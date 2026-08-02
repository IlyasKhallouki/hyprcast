/*
 * hyprcast engine -- MPEG-TS over RTP, on a writer thread of its own.
 *
 * The wire format is libavformat's rtp_mpegts muxer configured exactly the way
 * fluxcast configured ffmpeg, because that combination is field-proven against
 * the Xiaomi/Google TV sink (src/wfd.py:872-891 and :709-721):
 *
 *   muxdelay 0, muxpreload 0, flush_packets 1
 *   PMT PID 0x1000, video PID 0x1011, audio PID 0x1100, PCR on the video PID
 *   mpegts_flags resend_headers+pat_pmt_at_frames, pat_period 0.1, pcr_period 20
 *   rtp://IP:PORT?local_rtpport=SRC&local_rtcpport=SRC+1&pkt_size=1328
 *
 * pkt_size is the whole UDP payload: 12 bytes of RTP header + 7 * 188 bytes of
 * MPEG-TS = 1328, which is what a WFD receiver expects per datagram.
 *
 * The PIDs are set through AVStream.id (what "-streamid 0:4113" does), not
 * through mpegts_start_pid: mpegts.c uses st->id verbatim for any id >= 16, and
 * rtp_mpegts copies st->id onto the streams of the mpegts muxer it chains.
 * The mpegts-level options travel in the "mpegts_muxer_options" dictionary,
 * which is the only channel rtp_mpegts exposes to its inner muxer.
 *
 * THREADING -- the point of this file. libavformat's UDP writer can block
 * (ENOBUFS on a saturated Wi-Fi Direct link, a stalled ARP resolution, a
 * routing change), and the encode thread must never wait on that. So
 * hc_mux_video/hc_mux_audio only copy the access unit into a bounded ring and
 * signal; a private thread does every avformat call. When the ring is full we
 * drop WHOLE access units -- never a partial frame, which would desynchronise
 * the sink's PES parser -- and an incoming IDR evicts older droppable frames
 * rather than being dropped itself.
 *
 * Return convention, following hc_enc_receive(): < 0 is an error, 0 is
 * "queued", and 1 means "the ring was full and this whole frame was dropped".
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/channel_layout.h>
#include <libavutil/dict.h>
#include <libavutil/mathematics.h>
#include <libavutil/opt.h>

/* hc.h prototypes hc_pool_create() with an opaque Wayland type we never use
 * here; declaring it first keeps -Wpedantic from flagging the prototype. */
struct zwp_linux_dmabuf_v1;

#include "hc.h"

#define HC_LOG(...)  do {                     \
        fprintf(stderr, "[mux] ");            \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)
#define HC_ERR(...)  do {                     \
        fprintf(stderr, "[mux] ERROR: ");     \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)

/* fluxcast's field-proven layout. */
#define HC_PMT_PID        4096      /* 0x1000 */
#define HC_VIDEO_PID      4113      /* 0x1011 */
#define HC_AUDIO_PID      4352      /* 0x1100 */
#define HC_RTP_PKT_SIZE   1328      /* 12 + 7 * 188 */

/*
 * Ring depth in whole access units. 48 is 0.8 s of 60 fps video: deep enough to
 * ride out a scheduling hiccup or a burst of ENOBUFS, shallow enough that a
 * genuinely stuck link is noticed as drops rather than as latency.
 */
#define HC_MUX_RING       48

/*
 * How far ahead of the other stream libavformat may buffer one stream while
 * interleaving, in microseconds.
 *
 * This is a floor, not a target, and getting it wrong is subtle. rtp_mpegts
 * chains an "rtp" muxer that carries BOTH of our streams on one timeline, and
 * that muxer rejects a dts which is not monotonic -- for the whole session, not
 * per stream. av_interleaved_write_frame is what guarantees the global order,
 * and it stops guaranteeing it the moment one stream runs dry for longer than
 * max_interleave_delta: it flushes the other stream early, the late packet then
 * arrives with an older dts, and the write fails.
 *
 * MEASURED: at 250 ms this fired ~5 s into every run on an idle desktop, where
 * ext-image-copy-capture is damage driven and video gaps of several hundred ms
 * are normal ("Application provided invalid, non monotonically increasing dts
 * to muxer in stream 0: 490632 >= 488427"). So the value must exceed the
 * longest expected gap in either stream. 2 s does, and still bounds how long a
 * dead audio thread can hold video back -- libavformat's own default is 10 s.
 */
#define HC_MUX_INTERLEAVE_DELTA_US 2000000

static const AVRational HC_NS_TB = { 1, 1000000000 };

static void log_av(const char *what, int err)
{
    char msg[AV_ERROR_MAX_STRING_SIZE];
    if (av_strerror(err, msg, sizeof msg) < 0)
        snprintf(msg, sizeof msg, "unknown error %d", err);
    HC_ERR("%s: %s", what, msg);
}

struct hc_mux {
    struct hc_mux_cfg cfg;

    AVFormatContext *fmt;
    AVStream        *vs, *as;
    bool             own_pb;        /* we opened fmt->pb and may avio_tell it */

    pthread_mutex_t  lock;
    pthread_cond_t   cv;
    pthread_t        thread;
    bool             thread_live;

    AVPacket        *ring[HC_MUX_RING];
    int              head, tail, count;

    bool             stop;

    bool             have_base;
    uint64_t         base_ns;       /* the first frame defines t = 0 */
    int64_t          last_ts[2];    /* [0] video, [1] audio, in stream units */
    bool             have_last[2];

    _Atomic uint_least64_t bytes;   /* wire bytes, published for stats */
    uint64_t         drops, queued, written, write_errors;
    uint64_t         last_drop_log_ns;
    uint64_t         last_werr_log_ns;
};

/* ------------------------------------------------------------------ ring */

/* Caller holds the lock. */
static void ring_push(struct hc_mux *m, AVPacket *pkt)
{
    m->ring[m->tail] = pkt;
    m->tail = (m->tail + 1) % HC_MUX_RING;
    m->count++;
}

/* Caller holds the lock. Returns NULL when the ring is empty. */
static AVPacket *ring_pop(struct hc_mux *m)
{
    AVPacket *pkt;

    if (m->count == 0)
        return NULL;
    pkt = m->ring[m->head];
    m->ring[m->head] = NULL;
    m->head = (m->head + 1) % HC_MUX_RING;
    m->count--;
    return pkt;
}

/*
 * Make room for an arriving IDR by throwing away the oldest access unit that
 * is not itself a keyframe. Caller holds the lock. Returns true if a slot was
 * freed. Compacts the ring in place; the queue is 48 entries, so the memmove
 * is not worth avoiding.
 */
static bool ring_evict_droppable(struct hc_mux *m)
{
    for (int i = 0; i < m->count; i++) {
        int idx = (m->head + i) % HC_MUX_RING;
        AVPacket *p = m->ring[idx];

        if (p->flags & AV_PKT_FLAG_KEY)
            continue;

        av_packet_free(&m->ring[idx]);
        /* Close the hole by shifting the newer entries down one slot. */
        for (int j = i; j + 1 < m->count; j++) {
            int a = (m->head + j) % HC_MUX_RING;
            int b = (m->head + j + 1) % HC_MUX_RING;
            m->ring[a] = m->ring[b];
        }
        m->tail = (m->tail + HC_MUX_RING - 1) % HC_MUX_RING;
        m->ring[m->tail] = NULL;
        m->count--;
        return true;
    }
    return false;
}

/* ---------------------------------------------------------------- writer */

static void *writer_thread(void *arg)
{
    struct hc_mux *m = arg;

    for (;;) {
        AVPacket *pkt;
        int size, err;

        pthread_mutex_lock(&m->lock);
        while (m->count == 0 && !m->stop)
            pthread_cond_wait(&m->cv, &m->lock);
        pkt = ring_pop(m);
        if (!pkt) {                       /* empty and stopping */
            pthread_mutex_unlock(&m->lock);
            break;
        }
        pthread_mutex_unlock(&m->lock);

        size = pkt->size;
        /* Consumes the packet's reference; the AVPacket comes back blank. */
        err = av_interleaved_write_frame(m->fmt, pkt);
        av_packet_free(&pkt);

        if (err < 0) {
            /*
             * One rejected access unit must not end the cast: the sink recovers
             * from a lost frame at the next IDR, but it cannot recover from the
             * source going silent. Count it, say so at most once a second, and
             * keep writing.
             */
            uint64_t now = hc_now_ns();

            pthread_mutex_lock(&m->lock);
            m->write_errors++;
            if (now - m->last_werr_log_ns > 1000000000ull) {
                m->last_werr_log_ns = now;
                pthread_mutex_unlock(&m->lock);
                log_av("av_interleaved_write_frame", err);
            } else {
                pthread_mutex_unlock(&m->lock);
            }
            continue;
        }

        m->written++;
        if (m->own_pb && m->fmt->pb) {
            int64_t pos = avio_tell(m->fmt->pb);
            if (pos > 0)
                atomic_store_explicit(&m->bytes, (uint_least64_t)pos,
                                      memory_order_relaxed);
        } else {
            atomic_fetch_add_explicit(&m->bytes, (uint_least64_t)size,
                                      memory_order_relaxed);
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------ open */

/*
 * AAC-LC 48 kHz stereo AudioSpecificConfig:
 *   audioObjectType 2 (LC), samplingFrequencyIndex 3 (48000),
 *   channelConfiguration 2, GASpecificConfig all zero.
 *   00010 0011 0010 000 -> 0x11 0x90
 * hc_audio emits ADTS, which the mpegts muxer passes straight through, but a
 * caller feeding raw AAC needs this for the muxer to build ADTS itself.
 */
static const uint8_t HC_AAC_ASC[2] = { 0x11, 0x90 };

static int set_extradata(AVCodecParameters *par, const uint8_t *src, int size)
{
    uint8_t *buf;

    if (!src || size <= 0)
        return 0;
    buf = av_mallocz((size_t)size + AV_INPUT_BUFFER_PADDING_SIZE);
    if (!buf)
        return AVERROR(ENOMEM);
    memcpy(buf, src, (size_t)size);
    av_freep(&par->extradata);
    par->extradata = buf;
    par->extradata_size = size;
    return 0;
}

struct hc_mux *hc_mux_open(const struct hc_mux_cfg *cfg,
                           const uint8_t *extradata, int extradata_size)
{
    struct hc_mux *m;
    char url[256];
    int err;
    bool locks = false;

    if (!cfg || !cfg->dst_ip || !*cfg->dst_ip) {
        HC_ERR("hc_mux_open: no destination");
        return NULL;
    }
    if (cfg->dst_port <= 0 || cfg->dst_port > 65535 ||
        cfg->src_port  <= 0 || cfg->src_port  >= 65535) {
        HC_ERR("hc_mux_open: bad ports dst=%d src=%d (src needs src+1 for RTCP)",
               cfg->dst_port, cfg->src_port);
        return NULL;
    }
    if (cfg->width == 0 || cfg->height == 0 || cfg->fps == 0) {
        HC_ERR("hc_mux_open: bad wire format %ux%u@%u",
               cfg->width, cfg->height, cfg->fps);
        return NULL;
    }
    if (extradata && extradata_size > 0 && extradata[0] == 1) {
        HC_ERR("hc_mux_open: extradata is AVCC (length-prefixed); this path "
               "muxes Annex-B only -- do not set AV_CODEC_FLAG_GLOBAL_HEADER");
        return NULL;
    }

    m = calloc(1, sizeof *m);
    if (!m) {
        HC_ERR("hc_mux_open: out of memory");
        return NULL;
    }
    m->cfg = *cfg;
    /* dst_ip belongs to the caller and is only needed while the URL is built;
     * drop it so nothing here can outlive the string. */
    m->cfg.dst_ip = NULL;
    atomic_init(&m->bytes, (uint_least64_t)0);

    if (pthread_mutex_init(&m->lock, NULL) != 0) {
        HC_ERR("pthread_mutex_init failed");
        free(m);
        return NULL;
    }
    if (pthread_cond_init(&m->cv, NULL) != 0) {
        HC_ERR("pthread_cond_init failed");
        pthread_mutex_destroy(&m->lock);
        free(m);
        return NULL;
    }
    locks = true;

    /* Network init is reference counted inside libavformat. */
    avformat_network_init();

    /*
     * local_rtpport / local_rtcpport bind the source ports the sink saw in the
     * RTSP SETUP reply and checks on arrival; the peer's subnet changes between
     * sessions, so we deliberately do NOT pin localaddr here.
     */
    if (snprintf(url, sizeof url,
                 "rtp://%s:%d?local_rtpport=%d&local_rtcpport=%d&pkt_size=%d",
                 cfg->dst_ip, cfg->dst_port, cfg->src_port, cfg->src_port + 1,
                 HC_RTP_PKT_SIZE) >= (int)sizeof url) {
        HC_ERR("hc_mux_open: destination address is too long");
        goto fail;
    }

    err = avformat_alloc_output_context2(&m->fmt, NULL, "rtp_mpegts", url);
    if (err < 0 || !m->fmt) {
        log_av("avformat_alloc_output_context2(rtp_mpegts)", err);
        goto fail;
    }

    /* -muxdelay 0 / -muxpreload 0 / -flush_packets 1. */
    m->fmt->max_delay      = 0;
    m->fmt->audio_preload  = 0;
    m->fmt->flags         |= AVFMT_FLAG_FLUSH_PACKETS;
    m->fmt->max_interleave_delta = HC_MUX_INTERLEAVE_DELTA_US;
    /* Our timestamps start at zero by construction; never let libavformat
     * shift them behind our back. */
    m->fmt->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;

    /* The only channel rtp_mpegts gives us to its inner mpegts muxer. */
    err = av_opt_set(m->fmt->priv_data, "mpegts_muxer_options",
                     "mpegts_pmt_start_pid=" AV_STRINGIFY(HC_PMT_PID)
                     ":mpegts_start_pid=" AV_STRINGIFY(HC_VIDEO_PID)
                     ":mpegts_flags=resend_headers+pat_pmt_at_frames"
                     ":pat_period=0.1"
                     ":pcr_period=20", 0);
    if (err < 0) {
        log_av("av_opt_set(mpegts_muxer_options)", err);
        goto fail;
    }

    /* --- video ---------------------------------------------------------- */
    m->vs = avformat_new_stream(m->fmt, NULL);
    if (!m->vs) {
        HC_ERR("avformat_new_stream(video) failed");
        goto fail;
    }
    m->vs->id             = HC_VIDEO_PID;
    m->vs->time_base      = (AVRational){ 1, 90000 };
    m->vs->avg_frame_rate = (AVRational){ (int)cfg->fps, 1 };
    m->vs->codecpar->codec_type          = AVMEDIA_TYPE_VIDEO;
    m->vs->codecpar->codec_id            = AV_CODEC_ID_H264;
    m->vs->codecpar->codec_tag           = 0;
    m->vs->codecpar->width               = (int)cfg->width;
    m->vs->codecpar->height              = (int)cfg->height;
    m->vs->codecpar->format              = AV_PIX_FMT_YUV420P;
    m->vs->codecpar->profile             = AV_PROFILE_H264_CONSTRAINED_BASELINE;
    m->vs->codecpar->level               = 32;
    m->vs->codecpar->sample_aspect_ratio = (AVRational){ 1, 1 };
    err = set_extradata(m->vs->codecpar, extradata, extradata_size);
    if (err < 0) {
        log_av("video extradata", err);
        goto fail;
    }

    /* --- audio ---------------------------------------------------------- */
    if (cfg->with_audio) {
        m->as = avformat_new_stream(m->fmt, NULL);
        if (!m->as) {
            HC_ERR("avformat_new_stream(audio) failed");
            goto fail;
        }
        m->as->id        = HC_AUDIO_PID;
        m->as->time_base = (AVRational){ 1, 90000 };
        m->as->codecpar->codec_type  = AVMEDIA_TYPE_AUDIO;
        m->as->codecpar->codec_id    = AV_CODEC_ID_AAC;
        m->as->codecpar->codec_tag   = 0;
        m->as->codecpar->profile     = AV_PROFILE_AAC_LOW;
        m->as->codecpar->sample_rate = 48000;
        m->as->codecpar->frame_size  = 1024;
        m->as->codecpar->format      = AV_SAMPLE_FMT_FLTP;
        av_channel_layout_default(&m->as->codecpar->ch_layout, 2);
        err = set_extradata(m->as->codecpar, HC_AAC_ASC, (int)sizeof HC_AAC_ASC);
        if (err < 0) {
            log_av("audio extradata", err);
            goto fail;
        }
    }

    /* --- output --------------------------------------------------------- */
    if (!(m->fmt->oformat->flags & AVFMT_NOFILE)) {
        err = avio_open2(&m->fmt->pb, url, AVIO_FLAG_WRITE, NULL, NULL);
        if (err < 0) {
            log_av("avio_open2(rtp)", err);
            /* rtpproto loses the real errno on a failed bind -- an in-use
             * source port surfaces as "Inappropriate ioctl for device". Say
             * what it almost always means. */
            HC_ERR("could not open %s -- is local port %d or %d already bound? "
                   "(the sink checks the source port, so it cannot be moved)",
                   url, cfg->src_port, cfg->src_port + 1);
            goto fail;
        }
        m->own_pb = true;
    }

    err = avformat_write_header(m->fmt, NULL);
    if (err < 0) {
        log_av("avformat_write_header(rtp_mpegts)", err);
        goto fail;
    }

    if (pthread_create(&m->thread, NULL, writer_thread, m) != 0) {
        HC_ERR("pthread_create(mux writer) failed");
        goto fail;
    }
    m->thread_live = true;

    HC_LOG("rtp_mpegts -> %s:%d from :%d, %ux%u@%u, PMT 0x%04x video 0x%04x%s, "
           "pkt_size %d", cfg->dst_ip, cfg->dst_port, cfg->src_port,
           cfg->width, cfg->height, cfg->fps,
           HC_PMT_PID, HC_VIDEO_PID,
           cfg->with_audio ? " audio 0x1100" : " (no audio)", HC_RTP_PKT_SIZE);
    return m;

fail:
    if (m->fmt) {
        if (m->own_pb && m->fmt->pb)
            avio_closep(&m->fmt->pb);
        avformat_free_context(m->fmt);
        m->fmt = NULL;
    }
    avformat_network_deinit();
    if (locks) {
        pthread_cond_destroy(&m->cv);
        pthread_mutex_destroy(&m->lock);
    }
    free(m);
    return NULL;
}

/* ------------------------------------------------------------------ push */

static int push(struct hc_mux *m, AVStream *st, int which,
                const uint8_t *data, int size,
                uint64_t pts_ns, uint64_t dur_ns, bool key)
{
    AVPacket *pkt;
    uint64_t rel_ns;
    int64_t ts;
    int err;

    if (!m || !data || size <= 0) {
        HC_ERR("hc_mux_%s: null or empty access unit", which ? "audio" : "video");
        return -1;
    }
    if (!st) {
        HC_ERR("hc_mux_audio: this mux was opened without audio");
        return -1;
    }

    /* Allocate and copy outside the lock: the producer is the encode thread. */
    pkt = av_packet_alloc();
    if (!pkt) {
        HC_ERR("av_packet_alloc failed");
        return AVERROR(ENOMEM);
    }
    err = av_new_packet(pkt, size);
    if (err < 0) {
        log_av("av_new_packet", err);
        av_packet_free(&pkt);
        return err;
    }
    memcpy(pkt->data, data, (size_t)size);
    pkt->stream_index = st->index;
    if (key)
        pkt->flags |= AV_PKT_FLAG_KEY;

    pthread_mutex_lock(&m->lock);

    if (m->stop) {
        pthread_mutex_unlock(&m->lock);
        av_packet_free(&pkt);
        return -1;
    }

    /*
     * The FIRST frame of either stream defines t = 0, and both streams are
     * rescaled from that one origin -- audio and video would drift apart
     * otherwise.
     */
    if (!m->have_base) {
        m->base_ns   = pts_ns;
        m->have_base = true;
    }
    if (which == 1 && pts_ns < m->base_ns) {
        /*
         * Audio captured before the stream began -- hc_audio's ring holds
         * whatever the sound server produced while capture and encode were
         * still starting up. Clamping it to zero would fire a fifth of a second
         * of audio at the sink in one burst; it belongs nowhere in this stream.
         */
        m->drops++;
        pthread_mutex_unlock(&m->lock);
        av_packet_free(&pkt);
        return 1;
    }
    if (m->count == HC_MUX_RING) {
        /* Full. An IDR is worth more than a queued inter frame: evict for it.
         * Anything else is dropped whole -- never truncated. Decided before the
         * timestamp is stamped so a dropped frame leaves no trace behind. */
        if (!(key && which == 0) || !ring_evict_droppable(m)) {
            uint64_t now = hc_now_ns();
            m->drops++;
            if (now - m->last_drop_log_ns > 1000000000ull) {
                m->last_drop_log_ns = now;
                HC_ERR("ring full: dropped %" PRIu64 " of %" PRIu64
                       " access units -- the network write is not keeping up",
                       m->drops, m->queued + m->drops);
            }
            pthread_mutex_unlock(&m->lock);
            av_packet_free(&pkt);
            return 1;
        }
    }

    rel_ns = pts_ns > m->base_ns ? pts_ns - m->base_ns : 0;

    ts = av_rescale_q((int64_t)rel_ns, HC_NS_TB, st->time_base);
    /* Two captures can share a presentation timestamp (the compositor did not
     * repaint); libavformat rejects a non-increasing dts outright. */
    if (m->have_last[which] && ts <= m->last_ts[which])
        ts = m->last_ts[which] + 1;
    m->last_ts[which] = ts;
    m->have_last[which] = true;

    pkt->pts      = ts;
    pkt->dts      = ts;               /* no B-frames, ever */
    pkt->duration = av_rescale_q((int64_t)dur_ns, HC_NS_TB, st->time_base);

    ring_push(m, pkt);
    m->queued++;
    pthread_cond_signal(&m->cv);
    pthread_mutex_unlock(&m->lock);
    return 0;
}

int hc_mux_video(struct hc_mux *m, const uint8_t *data, int size,
                 uint64_t pts_ns, bool is_idr)
{
    if (!m) {
        HC_ERR("hc_mux_video: null mux");
        return -1;
    }
    return push(m, m->vs, 0, data, size, pts_ns,
                1000000000ull / (m->cfg.fps ? m->cfg.fps : 60), is_idr);
}

int hc_mux_audio(struct hc_mux *m, const uint8_t *data, int size,
                 uint64_t pts_ns)
{
    if (!m) {
        HC_ERR("hc_mux_audio: null mux");
        return -1;
    }
    /* One AAC-LC access unit is 1024 samples at 48 kHz. */
    return push(m, m->as, 1, data, size, pts_ns,
                1024ull * 1000000000ull / 48000ull, true);
}

/* ----------------------------------------------------------------- stats */

uint64_t hc_mux_bytes_sent(struct hc_mux *m)
{
    if (!m)
        return 0;
    return (uint64_t)atomic_load_explicit(&m->bytes, memory_order_relaxed);
}

/* ----------------------------------------------------------------- close */

void hc_mux_close(struct hc_mux *m)
{
    if (!m)
        return;

    if (m->thread_live) {
        pthread_mutex_lock(&m->lock);
        m->stop = true;
        pthread_cond_broadcast(&m->cv);
        pthread_mutex_unlock(&m->lock);
        /* The writer drains whatever is still queued before it returns. */
        pthread_join(m->thread, NULL);
        m->thread_live = false;
    }

    /* Anything the writer could not take (it stops on the first hard error). */
    for (int i = 0; i < HC_MUX_RING; i++)
        if (m->ring[i])
            av_packet_free(&m->ring[i]);

    if (m->fmt) {
        if (m->fmt->pb) {
            int err = av_write_trailer(m->fmt);
            if (err < 0)
                log_av("av_write_trailer", err);
        }
        if (m->own_pb && m->fmt->pb)
            avio_closep(&m->fmt->pb);
        avformat_free_context(m->fmt);
        m->fmt = NULL;
    }

    HC_LOG("closed: %" PRIu64 " access units queued, %" PRIu64 " written, "
           "%" PRIu64 " dropped, %" PRIu64 " rejected by libavformat, "
           "%" PRIu64 " bytes on the wire",
           m->queued, m->written, m->drops, m->write_errors,
           hc_mux_bytes_sent(m));

    avformat_network_deinit();
    pthread_cond_destroy(&m->cv);
    pthread_mutex_destroy(&m->lock);
    free(m);
}
