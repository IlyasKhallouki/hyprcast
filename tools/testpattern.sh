#!/bin/sh
# Fullscreen test pattern for judging a cast by eye.
#
# Run this on the laptop, cast in mirror mode, then look at the TV. Each part of
# the picture answers a different question:
#
#   SMPTE HD bars      colour accuracy. If red and blue are swapped they are
#                      obvious here, and the 75% bars show clipping.
#   sweeping white bar judder and tearing. It crosses the screen once a second
#                      at a constant rate, so any stutter reads as a hitch and
#                      any tearing shows as a horizontal split in the bar.
#   grey ramp          banding. Drop the bitrate with `hyprcast ctl bitrate 2M`
#                      and watch the steps appear.
#   spinning counter   latency. Photograph the laptop and the TV in one shot;
#                      the difference between the two numbers, times the frame
#                      time, is the glass to glass delay.
#   moving noise block the worst case for an encoder. If the bitrate graph is
#                      going to spike anywhere, it spikes here.
#
# No audio is produced or played. It is a video-only lavfi graph.
#
# Usage:
#   tools/testpattern.sh              1920x1080 at 60 fps, fullscreen
#   tools/testpattern.sh 1280x720 30  match the negotiated wire mode instead
#
# Quit with q or Escape.

set -eu

SIZE=${1:-1920x1080}
FPS=${2:-60}

W=${SIZE%x*}
H=${SIZE#*x}

case "$W$H" in
    *[!0-9]*|"") echo "testpattern: bad size '$SIZE', expected WIDTHxHEIGHT" >&2; exit 2 ;;
esac

command -v ffplay >/dev/null 2>&1 || {
    echo "testpattern: ffplay not found" >&2; exit 1
}

FONT=$(fc-match -f "%{file}" "monospace:bold" 2>/dev/null || true)
[ -n "$FONT" ] && [ -r "$FONT" ] || FONT=/usr/share/fonts/noto/NotoSansMono-Bold.ttf

# Layout, top to bottom: bars 55%, ramp 10%, noise strip 15%, black 20% for text.
BARS_H=$(( H * 55 / 100 ))
RAMP_H=$(( H * 10 / 100 ))
NOIS_H=$(( H * 15 / 100 ))
REST_H=$(( H - BARS_H - RAMP_H - NOIS_H ))
BAR_W=$(( W / 24 ))                     # sweeping bar width
NOIS_W=$(( W / 4 ))

# The sweep completes one pass per second, so its position doubles as a clock.
GRAPH="
smptehdbars=size=${W}x${BARS_H}:rate=${FPS}[bars];
gradients=size=${W}x${RAMP_H}:rate=${FPS}:c0=black:c1=white:x0=0:y0=0:x1=${W}:y1=0[ramp];
color=c=black:size=${W}x${NOIS_H}:rate=${FPS}[nbg];
noise=alls=100:allf=t+u,format=yuv420p[ns];
color=c=black:size=${W}x${REST_H}:rate=${FPS}[txt];
[bars][ramp]vstack=inputs=2[top];
[top][nbg]vstack=inputs=2[mid];
[mid][txt]vstack=inputs=2[base];
color=c=white:size=${BAR_W}x${H}:rate=${FPS}[sweep];
[base][sweep]overlay=x='mod(t\\,1)*(W+w)-w':y=0[swept];
[swept]drawtext=fontfile=${FONT}:text='%{n}':x=(w-tw)/2:y=h-th-${REST_H}/6:fontsize=${REST_H}/3:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=12[out]
"

# -an: never open an audio device. -fflags nobuffer + -flags low_delay so the
# window itself does not add latency to the thing being measured.
exec ffplay \
    -hide_banner -loglevel error \
    -an -sn \
    -fflags nobuffer -flags low_delay \
    -window_title "hyprcast test pattern ${SIZE}@${FPS}" \
    -fs \
    -f lavfi -i "$(printf '%s' "$GRAPH" | tr -d '\n')" \
    -vf "null" \
    -autoexit
