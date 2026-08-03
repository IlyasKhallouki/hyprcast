/*
 * hyprcast engine -- control channel.
 *
 * Newline-delimited JSON on an fd inherited from the Python control plane
 * (fd 3 by default). One object per line, in both directions:
 *
 *   -> {"cmd":"start","dst_ip":"192.168.168.42",...}
 *   <- {"ev":"ready"}
 *
 * The reader is hand-rolled on purpose: the engine links libav, libva, gbm and
 * wayland and that is already more dependency surface than a 12-field control
 * protocol deserves. It handles exactly what the protocol uses -- a flat object
 * whose values are strings, numbers, booleans or null -- and rejects everything
 * else rather than guessing.
 *
 * Robustness rules, all enforced below:
 *   - A read may split a line anywhere, including inside a string or an escape.
 *     Bytes accumulate until a '\n' arrives; nothing is parsed before that.
 *   - An unterminated string, a bare control character inside a string, a
 *     trailing comma, a nested object or trailing garbage after '}' all make
 *     the LINE bad. A bad line is dropped and parsing continues with the next
 *     one: one malformed message must not desynchronise the channel.
 *   - A string value that does not fit its fixed field in struct hc_ctl_msg is
 *     an error, never a truncation. Silently shortening an IP address or an
 *     output name would produce a session that connects to the wrong place.
 *   - A line longer than the buffer cannot be parsed, so it is skipped up to
 *     and including its newline instead of overflowing or blocking forever.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <unistd.h>

/* hc.h names struct zwp_linux_dmabuf_v1 only inside a prototype's parameter
 * list; declaring the tag at file scope first keeps -Wpedantic quiet. */
struct zwp_linux_dmabuf_v1;

#include "hc.h"

#define HC_CTL_BUF        8192   /* max control line, generously above need */
#define HC_CTL_KEY        32     /* longest key we can match */
#define HC_CTL_NUM        64     /* longest number literal we accept */
#define HC_CTL_WRITE_MS   1000   /* total budget for one event write */

#define HC_LOG(...)  do {                     \
        fprintf(stderr, "[ctl] ");            \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)
#define HC_ERR(...)  do {                     \
        fprintf(stderr, "[ctl] ERROR: ");     \
        fprintf(stderr, __VA_ARGS__);         \
        fputc('\n', stderr);                  \
    } while (0)

struct hc_ctl {
    int      fd;
    char     buf[HC_CTL_BUF];
    size_t   len;          /* valid bytes in buf */
    bool     eof;          /* peer closed; drain what is buffered, then -1 */
    bool     skipping;     /* discarding an over-long line up to its newline */
    uint64_t bad_lines;
};

/* ------------------------------------------------------------------ open */

struct hc_ctl *hc_ctl_open(int fd)
{
    struct hc_ctl *c;
    int fl;

    if (fd < 0) {
        HC_ERR("hc_ctl_open: bad fd %d", fd);
        return NULL;
    }

    fl = fcntl(fd, F_GETFL);
    if (fl < 0) {
        HC_ERR("hc_ctl_open: fd %d is not open: %s", fd, strerror(errno));
        return NULL;
    }
    /* Non-blocking is the whole contract of hc_ctl_poll(): the media loop
     * cannot afford to stall on a control plane that went quiet. */
    if (!(fl & O_NONBLOCK) && fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0) {
        HC_ERR("hc_ctl_open: O_NONBLOCK on fd %d: %s", fd, strerror(errno));
        return NULL;
    }

    c = calloc(1, sizeof *c);
    if (!c) {
        HC_ERR("hc_ctl_open: out of memory");
        return NULL;
    }
    c->fd = fd;
    return c;
}

/* ------------------------------------------------------------ json input */

struct jp {
    const char *p;
    const char *end;
};

static void jws(struct jp *j)
{
    while (j->p < j->end &&
           (*j->p == ' ' || *j->p == '\t' || *j->p == '\r' || *j->p == '\n'))
        j->p++;
}

static bool jpeek(struct jp *j, char ch)
{
    return j->p < j->end && *j->p == ch;
}

static int jhex4(struct jp *j, unsigned *out)
{
    unsigned v = 0;
    if (j->end - j->p < 4)
        return -1;
    for (int i = 0; i < 4; i++) {
        char ch = j->p[i];
        unsigned d;
        if (ch >= '0' && ch <= '9')      d = (unsigned)(ch - '0');
        else if (ch >= 'a' && ch <= 'f') d = (unsigned)(ch - 'a') + 10u;
        else if (ch >= 'A' && ch <= 'F') d = (unsigned)(ch - 'A') + 10u;
        else return -1;
        v = v * 16u + d;
    }
    j->p += 4;
    *out = v;
    return 0;
}

/* Append one byte, tracking overflow without ever writing past `cap`. */
static void put(char *out, size_t cap, size_t *n, bool *over, char ch)
{
    if (!out) {
        (*n)++;
        return;
    }
    if (*n + 1 >= cap) {          /* +1 leaves room for the NUL */
        *over = true;
        return;
    }
    out[(*n)++] = ch;
}

static void put_utf8(char *out, size_t cap, size_t *n, bool *over, unsigned cp)
{
    if (cp < 0x80u) {
        put(out, cap, n, over, (char)cp);
    } else if (cp < 0x800u) {
        put(out, cap, n, over, (char)(0xC0u | (cp >> 6)));
        put(out, cap, n, over, (char)(0x80u | (cp & 0x3Fu)));
    } else if (cp < 0x10000u) {
        put(out, cap, n, over, (char)(0xE0u | (cp >> 12)));
        put(out, cap, n, over, (char)(0x80u | ((cp >> 6) & 0x3Fu)));
        put(out, cap, n, over, (char)(0x80u | (cp & 0x3Fu)));
    } else {
        put(out, cap, n, over, (char)(0xF0u | (cp >> 18)));
        put(out, cap, n, over, (char)(0x80u | ((cp >> 12) & 0x3Fu)));
        put(out, cap, n, over, (char)(0x80u | ((cp >> 6) & 0x3Fu)));
        put(out, cap, n, over, (char)(0x80u | (cp & 0x3Fu)));
    }
}

/*
 * Parse one JSON string.
 *
 *   out == NULL          validate and discard (unknown key's value)
 *   returns  0           parsed, NUL-terminated in out
 *           -1           malformed -- unterminated, bad escape, raw control
 *           -2           well-formed but longer than cap-1; fully consumed, so
 *                        the caller may keep parsing the rest of the line
 */
static int jstring(struct jp *j, char *out, size_t cap)
{
    size_t n = 0;
    bool over = false;

    if (!jpeek(j, '"'))
        return -1;
    j->p++;

    for (;;) {
        if (j->p >= j->end)
            return -1;                       /* unterminated */
        unsigned char ch = (unsigned char)*j->p++;

        if (ch == '"')
            break;
        if (ch < 0x20)
            return -1;                       /* raw control character */
        if (ch != '\\') {
            put(out, cap, &n, &over, (char)ch);
            continue;
        }

        if (j->p >= j->end)
            return -1;                       /* escape split by end of line */
        char esc = *j->p++;
        switch (esc) {
        case '"':  put(out, cap, &n, &over, '"');  break;
        case '\\': put(out, cap, &n, &over, '\\'); break;
        case '/':  put(out, cap, &n, &over, '/');  break;
        case 'b':  put(out, cap, &n, &over, '\b'); break;
        case 'f':  put(out, cap, &n, &over, '\f'); break;
        case 'n':  put(out, cap, &n, &over, '\n'); break;
        case 'r':  put(out, cap, &n, &over, '\r'); break;
        case 't':  put(out, cap, &n, &over, '\t'); break;
        case 'u': {
            unsigned cp;
            if (jhex4(j, &cp) != 0)
                return -1;
            if (cp >= 0xD800u && cp <= 0xDBFFu) {
                /* High surrogate: pair it if the low half follows. */
                unsigned lo;
                if (j->end - j->p >= 6 && j->p[0] == '\\' && j->p[1] == 'u') {
                    struct jp save = *j;
                    j->p += 2;
                    if (jhex4(j, &lo) == 0 && lo >= 0xDC00u && lo <= 0xDFFFu)
                        cp = 0x10000u + ((cp - 0xD800u) << 10) + (lo - 0xDC00u);
                    else
                        *j = save, cp = 0xFFFDu;
                } else {
                    cp = 0xFFFDu;
                }
            } else if (cp >= 0xDC00u && cp <= 0xDFFFu) {
                cp = 0xFFFDu;                /* lone low surrogate */
            }
            put_utf8(out, cap, &n, &over, cp);
            break;
        }
        default:
            return -1;                       /* unknown escape */
        }
    }

    if (out && cap > 0)
        out[n < cap ? n : cap - 1] = '\0';
    return over ? -2 : 0;
}

/* JSON number grammar, then strtod. Returns 0 on success. */
static int jnumber(struct jp *j, double *out)
{
    const char *s = j->p;
    char tmp[HC_CTL_NUM];
    char *endp = NULL;
    size_t n;

    if (jpeek(j, '-'))
        j->p++;
    if (j->p >= j->end)
        return -1;
    if (*j->p == '0') {
        j->p++;
    } else if (*j->p >= '1' && *j->p <= '9') {
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9')
            j->p++;
    } else {
        return -1;
    }
    if (jpeek(j, '.')) {
        j->p++;
        if (!(j->p < j->end && *j->p >= '0' && *j->p <= '9'))
            return -1;
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9')
            j->p++;
    }
    if (jpeek(j, 'e') || jpeek(j, 'E')) {
        j->p++;
        if (jpeek(j, '+') || jpeek(j, '-'))
            j->p++;
        if (!(j->p < j->end && *j->p >= '0' && *j->p <= '9'))
            return -1;
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9')
            j->p++;
    }

    n = (size_t)(j->p - s);
    if (n == 0 || n >= sizeof tmp)
        return -1;
    memcpy(tmp, s, n);
    tmp[n] = '\0';

    errno = 0;
    *out = strtod(tmp, &endp);
    if (endp != tmp + n || !isfinite(*out))
        return -1;
    return 0;
}

static int jliteral(struct jp *j, const char *word)
{
    size_t n = strlen(word);
    if ((size_t)(j->end - j->p) < n || memcmp(j->p, word, n) != 0)
        return -1;
    j->p += n;
    return 0;
}

enum jval_kind { JV_STRING, JV_NUMBER, JV_BOOL, JV_NULL };

/*
 * Parse a value whose key we do not recognise, or whose type we still need.
 * Only the flat types are legal: '{' and '[' are rejected outright rather than
 * skipped, so a nested payload is a loud error instead of a silent half-parse.
 */
static int jvalue(struct jp *j, enum jval_kind *kind, double *num, bool *bl)
{
    jws(j);
    if (j->p >= j->end)
        return -1;

    switch (*j->p) {
    case '"':
        *kind = JV_STRING;
        return 0;                    /* caller re-parses into its own buffer */
    case 't':
        if (jliteral(j, "true") != 0) return -1;
        *kind = JV_BOOL; *bl = true; return 0;
    case 'f':
        if (jliteral(j, "false") != 0) return -1;
        *kind = JV_BOOL; *bl = false; return 0;
    case 'n':
        if (jliteral(j, "null") != 0) return -1;
        *kind = JV_NULL; return 0;
    case '{':
    case '[':
        HC_ERR("nested value: the control protocol is flat objects only");
        return -1;
    default:
        if (jnumber(j, num) != 0) return -1;
        *kind = JV_NUMBER; return 0;
    }
}

/* ------------------------------------------------------- field assignment */

static int want_u32(enum jval_kind k, double v, uint32_t *dst, const char *key)
{
    if (k != JV_NUMBER || v < 0.0 || v > 4294967295.0) {
        HC_ERR("'%s' must be a number in [0, 2^32)", key);
        return -1;
    }
    *dst = (uint32_t)v;
    return 0;
}

static int want_int(enum jval_kind k, double v, int *dst, const char *key)
{
    if (k != JV_NUMBER || v < -2147483648.0 || v > 2147483647.0) {
        HC_ERR("'%s' must be a number in int range", key);
        return -1;
    }
    *dst = (int)v;
    return 0;
}

static int want_bool(enum jval_kind k, double v, bool b, bool *dst,
                     bool *have, const char *key)
{
    if (k == JV_BOOL)
        *dst = b;
    else if (k == JV_NUMBER)
        *dst = v != 0.0;             /* 0/1 from a shell-built message */
    else {
        HC_ERR("'%s' must be a boolean", key);
        return -1;
    }
    *have = true;
    return 0;
}

/* Keys whose value must be a number or a boolean, never a string. */
static bool is_scalar_key(const char *key)
{
    static const char *const k[] = {
        "dst_port", "src_port", "width", "height", "fps",
        "bitrate", "gop", "qp", "gain", "muted", "low_power", "cursors",
    };
    for (size_t i = 0; i < sizeof k / sizeof *k; i++)
        if (!strcmp(key, k[i]))
            return true;
    return false;
}

/*
 * Parse one complete line.
 * Returns  1  line was blank -- skip it without counting it as bad
 *          0  *out is filled
 *         -1  malformed
 */
static int parse_line(const char *line, size_t len, struct hc_ctl_msg *out)
{
    struct jp j = { line, line + len };
    bool have_cmd = false;

    jws(&j);
    if (j.p >= j.end)
        return 1;

    memset(out, 0, sizeof *out);

    if (!jpeek(&j, '{')) {
        HC_ERR("line does not start with '{'");
        return -1;
    }
    j.p++;
    jws(&j);

    if (jpeek(&j, '}')) {
        j.p++;
        goto trailer;
    }

    for (;;) {
        char key[HC_CTL_KEY];
        enum jval_kind kind = JV_NULL;
        double num = 0.0;
        bool bl = false, known_key;
        int ks, rc;

        jws(&j);
        ks = jstring(&j, key, sizeof key);
        if (ks == -1) {
            HC_ERR("malformed key string");
            return -1;
        }
        if (ks == -2)
            key[0] = '\0';           /* too long to be one of ours */
        known_key = key[0] != '\0';

        jws(&j);
        if (!jpeek(&j, ':')) {
            HC_ERR("missing ':' after key '%s'", key);
            return -1;
        }
        j.p++;

        if (jvalue(&j, &kind, &num, &bl) != 0) {
            HC_ERR("malformed value for key '%s'", key);
            return -1;
        }

        rc = 0;
        if (kind == JV_STRING) {
            /* Route the string straight into its destination field so an
             * over-long value is caught by the field's own capacity. */
            char *dst = NULL;
            size_t cap = 0;

            if (!strcmp(key, "cmd"))          { dst = out->cmd;      cap = sizeof out->cmd; }
            else if (!strcmp(key, "dst_ip"))  { dst = out->s_dst_ip; cap = sizeof out->s_dst_ip; }
            else if (!strcmp(key, "output") ||
                     !strcmp(key, "name"))    { dst = out->s_output; cap = sizeof out->s_output; }
            else if (!strcmp(key, "audio"))   { dst = out->s_audio;  cap = sizeof out->s_audio; }
            else if (known_key && is_scalar_key(key)) {
                HC_ERR("'%s' takes a number or a boolean, not a string", key);
                return -1;
            }

            int sr = jstring(&j, dst, cap);
            if (sr == -1) {
                HC_ERR("malformed string value for key '%s'", key);
                return -1;
            }
            if (sr == -2) {
                HC_ERR("value for '%s' is longer than the %zu-byte field; "
                       "refusing to truncate it", key, cap ? cap - 1 : 0);
                return -1;
            }
            if (dst == out->cmd)
                have_cmd = true;
        } else if (!known_key || kind == JV_NULL) {
            /* nothing to store */
        } else if (!strcmp(key, "dst_port")) {
            rc = want_int(kind, num, &out->dst_port, key);
        } else if (!strcmp(key, "src_port")) {
            rc = want_int(kind, num, &out->src_port, key);
        } else if (!strcmp(key, "width")) {
            rc = want_u32(kind, num, &out->width, key);
        } else if (!strcmp(key, "height")) {
            rc = want_u32(kind, num, &out->height, key);
        } else if (!strcmp(key, "fps")) {
            rc = want_u32(kind, num, &out->fps, key);
        } else if (!strcmp(key, "bitrate")) {
            rc = want_u32(kind, num, &out->bitrate, key);
        } else if (!strcmp(key, "gop")) {
            rc = want_u32(kind, num, &out->gop, key);
        } else if (!strcmp(key, "qp")) {
            rc = want_u32(kind, num, &out->qp, key);
        } else if (!strcmp(key, "gain")) {
            if (kind != JV_NUMBER) {
                HC_ERR("'gain' must be a number");
                rc = -1;
            } else {
                out->gain     = (float)num;
                out->has_gain = true;
            }
        } else if (!strcmp(key, "muted")) {
            rc = want_bool(kind, num, bl, &out->muted, &out->has_muted, key);
        } else if (!strcmp(key, "low_power")) {
            rc = want_bool(kind, num, bl, &out->low_power,
                           &out->has_low_power, key);
        } else if (!strcmp(key, "cursors")) {
            rc = want_bool(kind, num, bl, &out->cursors,
                           &out->has_cursors, key);
        }
        if (rc != 0)
            return -1;

        jws(&j);
        if (jpeek(&j, ',')) {
            j.p++;
            jws(&j);
            if (jpeek(&j, '}')) {
                HC_ERR("trailing comma before '}'");
                return -1;
            }
            continue;
        }
        if (jpeek(&j, '}')) {
            j.p++;
            break;
        }
        HC_ERR("expected ',' or '}' after the value for '%s'", key);
        return -1;
    }

trailer:
    jws(&j);
    if (j.p != j.end) {
        HC_ERR("trailing garbage after the object");
        return -1;
    }
    if (!have_cmd || out->cmd[0] == '\0') {
        HC_ERR("message has no \"cmd\"");
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------------ poll */

static void consume(struct hc_ctl *c, size_t n)
{
    if (n >= c->len) {
        c->len = 0;
        return;
    }
    memmove(c->buf, c->buf + n, c->len - n);
    c->len -= n;
}

int hc_ctl_poll(struct hc_ctl *c, struct hc_ctl_msg *out)
{
    if (!c || !out) {
        HC_ERR("hc_ctl_poll: null argument");
        return -2;
    }

    for (;;) {
        char *nl = memchr(c->buf, '\n', c->len);

        if (nl) {
            size_t linelen = (size_t)(nl - c->buf);
            size_t total   = linelen + 1;

            if (c->skipping) {
                c->skipping = false;
                consume(c, total);
                continue;
            }
            int r = parse_line(c->buf, linelen, out);
            consume(c, total);
            if (r == 0)
                return 1;
            if (r < 0) {
                c->bad_lines++;
                HC_ERR("dropping malformed control line (%" PRIu64 " so far)",
                       c->bad_lines);
            }
            continue;                       /* blank or bad: try the next */
        }

        if (c->len == sizeof c->buf) {
            /* No newline in a full buffer: the line cannot ever be parsed. */
            HC_ERR("control line exceeds %zu bytes; skipping to the next "
                   "newline", sizeof c->buf);
            c->len = 0;
            c->skipping = true;
            c->bad_lines++;
            continue;
        }

        if (c->eof)
            return -1;

        ssize_t n = read(c->fd, c->buf + c->len, sizeof c->buf - c->len);
        if (n > 0) {
            c->len += (size_t)n;
            continue;
        }
        if (n == 0) {
            c->eof = true;
            if (c->len > 0) {
                HC_LOG("peer closed with %zu byte(s) of an unterminated line; "
                       "discarded", c->len);
                c->len = 0;
            }
            return -1;
        }
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            return 0;
        HC_ERR("read(fd %d): %s", c->fd, strerror(errno));
        return -2;
    }
}

/* ----------------------------------------------------------------- event */

int hc_ctl_event(struct hc_ctl *c, const char *json)
{
    struct iovec iov[2];
    static const char nl = '\n';
    size_t len, sent = 0, total;
    uint64_t deadline;

    if (!c || !json) {
        HC_ERR("hc_ctl_event: null argument");
        return -1;
    }

    len = strlen(json);
    while (len > 0 && (json[len - 1] == '\n' || json[len - 1] == '\r'))
        len--;
    if (len == 0)
        return 0;
    if (memchr(json, '\n', len)) {
        /* An embedded newline would split into two frames and desynchronise
         * the peer's reader. Refuse rather than corrupt the channel. */
        HC_ERR("hc_ctl_event: event contains an embedded newline");
        return -1;
    }
    total = len + 1;

    deadline = hc_now_ns() + (uint64_t)HC_CTL_WRITE_MS * 1000000ull;

    while (sent < total) {
        /* The payload and the framing newline, minus whatever a short write
         * already took. sent == len means only the newline is left. */
        int nio = 0;
        if (sent < len) {
            iov[nio].iov_base = (void *)(uintptr_t)(json + sent);
            iov[nio].iov_len  = len - sent;
            nio++;
        }
        iov[nio].iov_base = (void *)(uintptr_t)&nl;
        iov[nio].iov_len  = 1;
        nio++;

        ssize_t w = writev(c->fd, iov, nio);
        if (w > 0) {
            sent += (size_t)w;
            continue;
        }
        if (w < 0 && errno == EINTR)
            continue;
        if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            uint64_t now = hc_now_ns();
            if (now >= deadline) {
                HC_ERR("hc_ctl_event: peer has not drained the control fd in "
                       "%d ms; dropping the event", HC_CTL_WRITE_MS);
                return -1;
            }
            struct pollfd pfd = { c->fd, POLLOUT, 0 };
            int pr = poll(&pfd, 1, (int)((deadline - now) / 1000000ull) + 1);
            if (pr < 0 && errno != EINTR) {
                HC_ERR("hc_ctl_event: poll: %s", strerror(errno));
                return -1;
            }
            continue;
        }
        HC_ERR("hc_ctl_event: write: %s", strerror(errno));
        return -1;
    }
    return 0;
}

/* ----------------------------------------------------------------- close */

void hc_ctl_close(struct hc_ctl *c)
{
    if (!c)
        return;
    if (c->fd >= 0)
        close(c->fd);
    free(c);
}
