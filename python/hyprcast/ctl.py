"""
hyprcast control IPC: JSON-lines over a unix socket.

Wire format is one JSON object per line, both directions.

    -> {"cmd":"fps","value":30}
    <- {"ok":true,"state":{...}}
    <- {"ok":false,"error":"engine is not running"}

`subscribe` is the exception: the request is answered normally and then the
connection stays open, receiving {"ev":"state","state":{...}} on every change
plus a heartbeat. That is what makes `hyprcast waybar` a streaming module
instead of a polling one.

Commands: status start stop fps bitrate volume mute monitor mode
          list-outputs list-sinks subscribe quit
"""

from __future__ import annotations

import errno
import json
import os
import socket
import socketserver
import stat
import threading

__all__ = ["socket_path", "Server", "Client", "CtlError", "NotRunning", "COMMANDS"]

COMMANDS = (
    "status", "start", "stop", "fps", "bitrate", "volume", "mute",
    "monitor", "mode", "list-outputs", "list-sinks", "subscribe", "quit",
)

HEARTBEAT_S = 1.0
_CLIENT_TIMEOUT = 5.0


class CtlError(RuntimeError):
    """The server answered with an error, or the exchange failed."""


class NotRunning(CtlError):
    """No hyprcast session is listening on the control socket."""


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "hyprcast.sock")


def _send_line(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "Server" = self.server  # type: ignore[assignment]
        subscribed = False
        try:
            for raw in self.rfile:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("not an object")
                except ValueError:
                    self._reply({"ok": False, "error": "malformed JSON line"})
                    continue

                cmd = str(msg.get("cmd", ""))
                if cmd == "subscribe":
                    self._reply({"ok": True, "state": server.snapshot()})
                    subscribed = True
                    server.add_subscriber(self)
                    self._pump()
                    return
                try:
                    reply = server.dispatch(cmd, msg)
                except Exception as exc:  # a broken handler must not kill the server
                    reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self._reply(reply)
                if cmd == "quit" and reply.get("ok"):
                    server.request_shutdown()
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if subscribed:
                server.drop_subscriber(self)

    def _reply(self, obj: dict) -> None:
        try:
            _send_line(self.connection, obj)
        except OSError:
            raise BrokenPipeError from None

    def _pump(self) -> None:
        """Block until the subscriber goes away; pushes arrive via push()."""
        server: "Server" = self.server  # type: ignore[assignment]
        self.connection.settimeout(HEARTBEAT_S)
        while not server.stopping:
            try:
                if not self.connection.recv(256):
                    return          # peer closed
            except socket.timeout:
                try:
                    _send_line(self.connection, {"ev": "tick"})
                except OSError:
                    return
            except OSError:
                return

    def push(self, obj: dict) -> None:
        try:
            _send_line(self.connection, obj)
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    """
    Threaded JSON-lines server. `dispatcher(cmd, msg) -> dict` does the work and
    must be thread-safe; `snapshot() -> dict` returns the current session state.
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, dispatcher, snapshot, path: str | None = None, on_quit=None):
        self.path = path or socket_path()
        self._dispatch = dispatcher
        self._snapshot = snapshot
        self._on_quit = on_quit
        self._subs: set[_Handler] = set()
        self._subs_lock = threading.Lock()
        self.stopping = False
        _clear_stale_socket(self.path)
        super().__init__(self.path, _Handler)
        os.chmod(self.path, 0o600)

    # ---- plumbing the handler calls into
    def dispatch(self, cmd: str, msg: dict) -> dict:
        return self._dispatch(cmd, msg)

    def snapshot(self) -> dict:
        return self._snapshot()

    def add_subscriber(self, handler: _Handler) -> None:
        with self._subs_lock:
            self._subs.add(handler)

    def drop_subscriber(self, handler: _Handler) -> None:
        with self._subs_lock:
            self._subs.discard(handler)

    def broadcast(self, state: dict) -> None:
        with self._subs_lock:
            subs = list(self._subs)
        for handler in subs:
            handler.push({"ev": "state", "state": state})

    def request_shutdown(self) -> None:
        """
        Stop accepting and tell the owner to exit.

        `shutdown()` alone only stops serve_forever's accept loop -- the
        listening socket stays bound, so a later client connects into an
        unserviced backlog and hangs until its own timeout. The owner MUST be
        woken so it can reach server_close(); that is what on_quit is for.
        """
        if self.stopping:
            return
        self.stopping = True
        threading.Thread(target=self.shutdown, daemon=True).start()
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception:
                pass

    def serve_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="hyprcast-ctl", daemon=True)
        thread.start()
        return thread

    def server_close(self) -> None:
        self.stopping = True
        super().server_close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _clear_stale_socket(path: str) -> None:
    """
    Unlink a socket left behind by a crashed session, but refuse to steal one a
    live session is still serving.
    """
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CtlError(f"control socket {path} is unusable: {exc}") from None
    if not stat.S_ISSOCK(mode):
        # Something that is not a socket is squatting on our path. It cannot be
        # a live session, so it goes.
        try:
            os.unlink(path)
        except OSError as exc:
            raise CtlError(f"cannot replace {path}: {exc}") from None
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(path)
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        raise CtlError(f"control socket {path} is unusable: {exc}") from None
    else:
        raise CtlError(f"hyprcast is already running (control socket {path} is live)")
    finally:
        probe.close()


class Client:
    """One-shot request/response, or a streaming subscription."""

    def __init__(self, path: str | None = None, timeout: float = _CLIENT_TIMEOUT):
        self.path = path or socket_path()
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise NotRunning(f"no hyprcast session at {self.path} ({exc.strerror})") from None
        return sock

    def call(self, cmd: str, **fields) -> dict:
        sock = self._connect()
        try:
            _send_line(sock, dict(cmd=cmd, **fields))
            line = sock.makefile("rb").readline()
        except OSError as exc:
            raise CtlError(f"control exchange failed: {exc}") from None
        finally:
            sock.close()
        if not line:
            raise CtlError("server closed the connection without replying")
        try:
            reply = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            raise CtlError("server sent a malformed reply") from None
        if not reply.get("ok", False):
            raise CtlError(str(reply.get("error", "unknown error")))
        return reply

    def subscribe(self):
        """Yield the initial state and then every subsequent state object."""
        sock = self._connect()
        sock.settimeout(None)
        try:
            _send_line(sock, {"cmd": "subscribe"})
            stream = sock.makefile("rb")
            for raw in stream:
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if "state" in msg:
                    yield msg["state"]
                elif msg.get("ev") == "tick":
                    yield None
        finally:
            sock.close()

    def alive(self) -> bool:
        try:
            self.call("status")
            return True
        except CtlError:
            return False
