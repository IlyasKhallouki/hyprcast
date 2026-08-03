"""
hyprcast-engine supervisor -- the seam between the Python control plane and the
native media path.

Python owns P2P, RTSP and the CEA negotiation; the engine owns every pixel.
They talk newline-delimited JSON over one inherited socketpair, exactly as
engine/src/hc.h specifies:

    -> {"cmd":"start","dst_ip":"192.168.168.42","dst_port":19900,...}
    -> {"cmd":"retune","fps":30}   {"cmd":"idr"}   {"cmd":"volume","gain":0.5}
    -> {"cmd":"output","name":"HEADLESS-1"}   {"cmd":"stop"}   {"cmd":"quit"}
    <- {"ev":"ready"}  {"ev":"stats",...}  {"ev":"error","msg":"..."}

The child end is dup2'd onto fd 3 by os.posix_spawn, which does the dup after
fork and before exec without running any Python in the child. subprocess's
pass_fds cannot renumber an fd, and preexec_fn is not safe here: wfd.py runs
the RTSP server on several threads, and forking from a thread with a Python
callback in the child is the classic deadlock.

Nothing in here blocks forever. Every wait takes a deadline, the reader thread
turns a dead engine into an {"ev":"exited"} event, and every send on a dead
control channel raises EngineError instead of hanging.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import struct
import threading
import time
from typing import Any, Callable, Iterator, Optional

# The engine reads its control channel from this fd. hc.h: "an inherited fd
# (3 by default)". Keep the two in sync.
CTL_FD = 3

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
DEFAULT_BINARY = os.path.join(_REPO_ROOT, "engine", "build", "hyprcast-engine")


class EngineError(RuntimeError):
    """The engine could not be started, refused a command, or died."""


def engine_binary(path: Optional[str] = None) -> str:
    """Resolve the engine binary: explicit path, then $HYPRCAST_ENGINE, then the
    in-tree meson build directory."""
    return path or os.environ.get("HYPRCAST_ENGINE") or DEFAULT_BINARY


class Engine:
    """Owns one hyprcast-engine child process and its fd-3 control socket."""

    def __init__(
        self,
        binary: Optional[str] = None,
        argv: tuple[str, ...] = (),
        log: Optional[Callable[[str], None]] = None,
        ready_timeout: float = 15.0,
        quit_timeout: float = 5.0,
    ) -> None:
        self.binary = engine_binary(binary)
        self.argv = tuple(argv)
        self.ready_timeout = ready_timeout
        self.quit_timeout = quit_timeout
        self._log = log or (lambda msg: print(f"[hyprcast engine] {msg}", flush=True))

        self.pid: Optional[int] = None
        self.returncode: Optional[int] = None

        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._ready = False
        self._error: Optional[str] = None
        self._stats: dict[str, Any] = {}
        self._stats_seq = 0
        self._ctl_closed = False

    # ---------------------------------------------------------------- lifecycle

    def spawn(self) -> None:
        """Fork/exec the engine with the control socket on fd 3. Idempotent."""
        if self.pid is not None:
            return
        if not os.path.exists(self.binary):
            raise EngineError(
                f"engine binary not found: {self.binary} — build it with "
                "`ninja -C engine/build`, or point $HYPRCAST_ENGINE at it"
            )
        if not os.access(self.binary, os.X_OK):
            raise EngineError(f"engine binary is not executable: {self.binary}")

        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            pid = os.posix_spawn(
                self.binary,
                [self.binary, *self.argv],
                dict(os.environ),
                file_actions=[(os.POSIX_SPAWN_DUP2, child.fileno(), CTL_FD)],
            )
        except OSError as exc:
            parent.close()
            raise EngineError(f"could not exec {self.binary}: {exc}") from exc
        finally:
            child.close()

        # A stalled engine must not wedge the RTSP thread inside sendall().
        try:
            parent.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("@ll", 2, 0)
            )
        except OSError:
            pass

        self.pid = pid
        self.returncode = None
        self._ctl_closed = False
        self._sock = parent
        self._reader = threading.Thread(
            target=self._read_loop, name="hyprcast-engine-rx", daemon=True
        )
        self._reader.start()
        self._log(f"started {self.binary} (pid {pid}, control on fd {CTL_FD})")

    def is_alive(self) -> bool:
        """True while the child is running and its control channel is open."""
        if self.pid is None or self._ctl_closed:
            return False
        return self._poll() is None

    def _poll(self) -> Optional[int]:
        """Reap without blocking. Returns the exit code, or None if still up."""
        if self.pid is None or self.returncode is not None:
            return self.returncode
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = -1
            return self.returncode
        except OSError:
            return None
        if pid == 0:
            return None
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def _wait(self, timeout: float) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while True:
            code = self._poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def _kill(self, sig: int) -> None:
        if self.pid is None:
            return
        try:
            os.kill(self.pid, sig)
        except OSError:
            pass

    def quit(self, timeout: Optional[float] = None) -> Optional[int]:
        """Ask the engine to exit, then insist. Never raises, always reaps."""
        if self.pid is None:
            return self.returncode
        grace = self.quit_timeout if timeout is None else timeout
        try:
            self._send({"cmd": "quit"})
        except EngineError:
            pass

        code = self._wait(grace)
        if code is None:
            self._log("engine ignored quit; sending SIGTERM")
            self._kill(signal.SIGTERM)
            code = self._wait(2.0)
        if code is None:
            self._log("engine ignored SIGTERM; sending SIGKILL")
            self._kill(signal.SIGKILL)
            code = self._wait(2.0)

        self._close_socket()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)
        self._reader = None
        self.pid = None
        self.returncode = code if code is not None else self.returncode
        return self.returncode

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ----------------------------------------------------------------- commands

    def _send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._send_lock:
            sock = self._sock
            if sock is None or self._ctl_closed:
                raise EngineError(
                    f"engine is not running (exit code {self.returncode})"
                )
            try:
                sock.sendall(line)
            except OSError as exc:
                self._ctl_closed = True
                raise EngineError(f"engine control channel is dead: {exc}") from exc

    def start(self, **params: Any) -> None:
        """Spawn if needed, send `start`, and block until `ready` or failure."""
        self.spawn()
        with self._cond:
            self._ready = False
            self._error = None
        self._send({"cmd": "start", **params})
        self.wait_ready(self.ready_timeout)

    def retune(self, **kw: Any) -> None:
        """fps / bitrate / qp, live. No RTSP renegotiation, no encoder restart."""
        if not kw:
            raise ValueError("retune needs at least one of fps, bitrate, qp")
        self._send({"cmd": "retune", **kw})

    def idr(self) -> None:
        """Force the next frame to be an IDR (the sink asked; it has corruption)."""
        self._send({"cmd": "idr"})

    def volume(self, gain: Optional[float] = None, muted: Optional[bool] = None) -> None:
        """Gain and mute, applied to the PCM before encoding.

        gain 1.0 is UNITY -- the sink hears the monitor source untouched. The
        engine clamps at 4.0 and hard-clips the samples above unity. Whichever
        key is omitted is left exactly as it was; that is contractual, because
        `mute` and `unmute` send nothing else.
        """
        msg: dict[str, Any] = {"cmd": "volume"}
        if gain is not None:
            msg["gain"] = float(gain)
        if muted is not None:
            msg["muted"] = bool(muted)
        if len(msg) == 1:
            raise ValueError("volume needs gain or muted")
        self._send(msg)

    def set_audio_device(self, device: str) -> None:
        """Reopen ONLY the audio leg against another pulse source.

        Video, the encoder, the muxer and the RTP sequence space keep running,
        and the live gain/mute carry over. A session started without audio
        cannot grow one: the sink was told in M4 what streams to expect.
        """
        if not device:
            raise ValueError("set_audio_device needs a source name")
        self._send({"cmd": "audio", "audio": device})

    def set_output(self, name: str) -> None:
        """Rebuild capture against another wl_output; the wire size is frozen."""
        self._send({"cmd": "output", "name": name})

    def stop(self) -> None:
        """Stop the media leg but keep the engine warm for the next start."""
        self._send({"cmd": "stop"})

    def sync(self, timeout: float = 4.0, ticks: int = 2) -> bool:
        """Block until the engine has demonstrably consumed what we just sent.

        hc.h defines no per-command ack -- only ready / stats / error come back
        -- but the engine drains its whole control queue and emits stats from
        the same loop, once a second, control first. So a stats event that
        lands after our send proves the send was consumed, unless that event
        was already in flight when we called; two of them cannot both be.

        This is what makes `output` safe to wait on. Returns False on timeout
        instead of raising: the only caller is about to destroy a wl_output,
        and "could not confirm" must leave that output alone rather than kill
        the session (or, per the crash we hit, the compositor).
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            target = self._stats_seq + max(1, int(ticks))
            while self._stats_seq < target:
                if self._ctl_closed:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    # ------------------------------------------------------------------- events

    def wait_ready(self, timeout: Optional[float] = None) -> None:
        timeout = self.ready_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        with self._cond:
            while not (self._ready or self._error or self._ctl_closed):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineError(
                        f"engine did not report ready within {timeout:g}s"
                    )
                self._cond.wait(remaining)
            if self._error:
                raise EngineError(self._error)
            if not self._ready:
                raise EngineError(
                    f"engine exited before it was ready (exit code {self.returncode})"
                )

    def events(self, timeout: Optional[float] = None) -> Iterator[dict[str, Any]]:
        """Yield parsed engine events until it exits.

        The final event is always {"ev": "exited", "code": N}, so a consumer
        loop terminates on engine death rather than blocking forever. With
        `timeout` set, the iterator also ends after that many idle seconds.
        """
        while True:
            try:
                event = self._events.get(timeout=timeout)
            except queue.Empty:
                return
            yield event
            if event.get("ev") == "exited":
                return

    @property
    def stats(self) -> dict[str, Any]:
        """The most recent stats event, or {} if none has arrived."""
        with self._cond:
            return dict(self._stats)

    @property
    def last_error(self) -> Optional[str]:
        with self._cond:
            return self._error

    def _read_loop(self) -> None:
        sock = self._sock
        buffer = b""
        try:
            while True:
                try:
                    chunk = sock.recv(65536) if sock is not None else b""
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    self._dispatch(line)
            if buffer.strip():
                self._dispatch(buffer)
        finally:
            self._on_ctl_closed()

    def _dispatch(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            self._log(f"unparsable event: {text!r}")
            self._put({"ev": "unparsable", "raw": text})
            return
        if not isinstance(event, dict):
            self._put({"ev": "unparsable", "raw": text})
            return

        kind = event.get("ev")
        with self._cond:
            if kind == "ready":
                self._ready = True
            elif kind == "error":
                self._error = str(event.get("msg", "engine reported an error"))
            elif kind == "stats":
                self._stats = event
                self._stats_seq += 1
            self._cond.notify_all()
        if kind == "error":
            self._log(f"ERROR: {event.get('msg')}")
        self._put(event)

    def _on_ctl_closed(self) -> None:
        code = self._wait(1.0)
        with self._cond:
            self._ctl_closed = True
            self._cond.notify_all()
        self._put({"ev": "exited", "code": code})

    def _put(self, event: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass

    # ------------------------------------------------------------------ context

    def __enter__(self) -> "Engine":
        self.spawn()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.quit()
