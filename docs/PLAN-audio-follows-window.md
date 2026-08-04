# Plan: audio follows the window

Deferred. Recorded 2026-08-04 so the findings behind it are not lost.

## What it should do

- A window on the TV plays its sound through the TV.
- A window on the laptop plays its sound through the laptop.
- Stopping the cast returns those windows to the laptop screen and their audio
  with them.
- Reconnecting puts both back on the TV.

## The join works

Hyprland gives every window a `pid` and a `monitor`. PipeWire gives every
stream an `application.process.id`. They do not match directly for Chrome, but
walking `/proc` ancestry bridges them. Verified on this machine:

```
audio stream pid 1442814 (chrome)
  ancestor      1441468  chrome   <-- the window's pid
```

So: stream -> walk up the process tree -> find the owning window -> read its
monitor -> move the stream to the matching sink.

## The limitation to state before building

**All Chrome audio moves as a unit.** Three Chrome windows on workspaces 2, 4
and 6 all report `pid=1441468`; Chrome runs one audio service for every tab, so
a stream cannot be traced to a particular window. If one Chrome window is on the
TV and another is on the laptop, they cannot be split. Firefox is the same.

Everything with its own process (mpv, VLC, Spotify, games) routes correctly.

Proposed rule for the ambiguous case: route a multi-window process by its
FOCUSED window. Document that browsers move as a unit.

## Design

A router inside the cast session. Event driven, no polling.

Inputs:
- Hyprland `socket2`: openwindow, closewindow, movewindow, movewindowv2,
  workspace, focusedmon, monitoradded, monitorremoved
- `pactl subscribe`: sink-input new / change / remove

On any event:
1. Rebuild pid -> monitor from `hyprctl clients -j`
2. For each sink-input, resolve its owning window by ancestry walk
3. Target sink is `hyprcast` when that window is on the headless output,
   otherwise the previous default
4. `pactl move-sink-input` only where the target actually changed

Self-correcting: a stream that starts before its window is mapped is placed on
the next event.

## Teardown ordering, which is a prerequisite

The order below is not cosmetic. Getting it wrong is what stranded a playing
video on a removed output with no way to pause it:

1. Move every stream on the `hyprcast` sink back to the previous default
2. Move workspaces off the headless output back to a real one
3. THEN remove the output and unload the null sink

Must hold on every exit path: normal stop, SIGINT, SIGTERM, and the engine
dying unexpectedly.

## Reconnect

Persist which workspaces were on the TV. On reconnect, restore them; the router
then places their audio automatically, because routing is driven by window
position rather than remembered stream ids.

## Build order

1. Fix the teardown ordering first, and verify it by stopping a real cast with
   a video playing on the second screen.
2. Add the router on top.

Splitting it matters: the parts that break here are the ones only a real
session exercises, and loopback has passed several things the TV then rejected.
