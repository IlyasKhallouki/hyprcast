"""
The config file, so the same flags stop being retyped every session.

Looked for at $HYPRCAST_CONFIG, else $XDG_CONFIG_HOME/hyprcast/config.toml,
else ~/.config/hyprcast/config.toml. A missing file is NOT an error: every key
has a built-in default and hyprcast runs with no config at all.

Precedence, highest first:  explicit CLI flag  >  config file  >  built-in
default. argparse cannot tell "the user typed --fps 60" from "60 is the
default", so every config-backed argument in __main__ is declared with
default=None -- including the store_true ones, which then yield None when
absent and True when present -- and apply_flags() only overrides a key when the
flag is not None.

The built-in defaults are EXACTLY today's argparse defaults, so a machine with
no config file behaves identically to before this module existed. The starter
file written by `hyprcast config --init` carries this laptop's preferences,
which differ from the defaults for a few keys; those are commented at the point
of use.

Validation is strict about types and lenient about vocabulary: an unknown key
is a warning (the file still loads), a malformed value is fatal (silently
casting at 30 fps because someone typed fps = "sixty" is worse than refusing).
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib

__all__ = [
    "ConfigError", "Config", "SPEC", "config_path", "load", "apply_flags",
    "describe", "starter_text", "write_starter",
]


class ConfigError(RuntimeError):
    """A malformed config file, or a malformed flag. Always fatal."""


_TOML_TYPE = {str: "string", int: "integer", float: "float", bool: "boolean",
              list: "array", dict: "table"}


def _typename(value) -> str:
    return _TOML_TYPE.get(type(value), type(value).__name__)


class Field:
    """One key: its type, its built-in default, and what values are legal."""

    __slots__ = ("kind", "default", "choices", "lo", "hi", "note")

    def __init__(self, kind, default, choices=(), lo=None, hi=None, note=""):
        self.kind = kind
        self.default = default
        self.choices = tuple(choices)
        self.lo = lo
        self.hi = hi
        self.note = note

    def check(self, value):
        """Return the normalised value, or raise ValueError explaining why not."""
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"expected a boolean (true or false), "
                                 f"got {_typename(value)} {value!r}")
            return value

        if self.kind == "int":
            # bool is a subclass of int; `fps = true` must not become fps 1.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"expected an integer, got {_typename(value)} {value!r}")
            if self.lo is not None and not (self.lo <= value <= self.hi):
                raise ValueError(f"expected an integer in {self.lo}..{self.hi}, got {value}")
            return value

        if self.kind == "str":
            if not isinstance(value, str):
                raise ValueError(f"expected a string, got {_typename(value)} {value!r}")
            if self.choices and value not in self.choices:
                raise ValueError(f"expected one of {', '.join(map(repr, self.choices))}, "
                                 f"got {value!r}")
            return value

        if self.kind == "bitrate":
            # session.parse_bitrate is the one true parser; wfd's consumer wants
            # text ("8M"), so an integer number of bits is normalised to text.
            from .session import SessionError, parse_bitrate
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError(f"expected a bitrate string like \"8M\", "
                                 f"got {_typename(value)} {value!r}")
            text = str(value)
            try:
                bits = parse_bitrate(text)
            except SessionError:
                raise ValueError(f"expected a bitrate like \"8M\", \"6000k\" or "
                                 f"6000000, got {value!r}") from None
            if bits <= 0:
                raise ValueError(f"expected a bitrate above zero, got {value!r}")
            return text

        raise AssertionError(f"unknown field kind {self.kind!r}")


# The schema. Defaults here MUST match the argparse defaults that shipped
# before this module, so "no config file" is a no-op.
SPEC: dict[str, dict[str, Field]] = {
    "cast": {
        "fps":         Field("int", 60, lo=1, hi=240),
        "bitrate":     Field("bitrate", "8M"),
        "mode":        Field("str", "mirror", choices=("mirror", "extend")),
        "monitor":     Field("str", ""),
        "audio":       Field("str", "shared", choices=("shared", "tv-only", "device", "none")),
        # Cast volume only. 100 is unity -- what the sink hears equals what the
        # monitor source carries -- and it never touches the laptop's own sink.
        "volume":      Field("int", 100, lo=0, hi=100),
        "low_power":   Field("bool", False),
        "cursors":     Field("bool", True),
        "qp":          Field("int", None, lo=1, hi=51),
        "no_firewall": Field("bool", False),
        "width":       Field("int", 1280, lo=16, hi=7680),
        "height":      Field("int", 720, lo=16, hi=4320),
    },
    "wifi": {
        "interface": Field("str", "p2p-dev-wlan0"),
        "timeout":   Field("int", 60, lo=1, hi=3600),
        "sink":      Field("str", ""),
        "go_intent": Field("int", None, lo=0, hi=15),
    },
    "extend": {
        "name":      Field("str", "hyprcast"),
        "position":  Field("str", "auto",
                           choices=("auto", "left", "right", "above", "below")),
        "workspace": Field("str", ""),
    },
    "waybar": {
        "format":       Field("str", "{icon} {fps}"),
        "show_bitrate": Field("bool", True),
    },
}


# ------------------------------------------------------------------ location
def config_path(env=None) -> str:
    """Where the file is looked for. $HYPRCAST_CONFIG wins, for testing."""
    env = os.environ if env is None else env
    override = env.get("HYPRCAST_CONFIG")
    if override:
        return override
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(base, "hyprcast", "config.toml")


def _line_of(text: str, section: str, key: str | None) -> int:
    """The 1-based line of `key` inside [section], or of the header itself.

    tomllib does not hand back positions for values, and a bare `grep key`
    would point at the wrong table when two sections share a key name, so walk
    the file tracking the current header.
    """
    header = re.compile(r"\s*\[\s*([A-Za-z0-9_.-]+)\s*\]")
    current = ""
    for number, line in enumerate(text.splitlines(), 1):
        match = header.match(line)
        if match:
            current = match.group(1)
            if current == section and key is None:
                return number
            continue
        if current == section and key is not None:
            if re.match(rf"\s*(?:{re.escape(key)}|\"{re.escape(key)}\"|"
                        rf"'{re.escape(key)}')\s*=", line):
                return number
    return 0


def _at(path: str, line: int) -> str:
    return f"{path}:{line}" if line else path


# -------------------------------------------------------------------- config
class Config:
    """Effective values plus, for every one of them, where it came from."""

    def __init__(self, path: str, exists: bool):
        self.path = path
        self.exists = exists
        self.warnings: list[str] = []
        self._values: dict[tuple[str, str], object] = {}
        self._sources: dict[tuple[str, str], str] = {}
        for section, fields in SPEC.items():
            for key, field in fields.items():
                self._values[(section, key)] = field.default
                self._sources[(section, key)] = "default"

    def get(self, section: str, key: str):
        return self._values[(section, key)]

    def source(self, section: str, key: str) -> str:
        return self._sources[(section, key)]

    def set(self, section: str, key: str, value, source: str) -> None:
        self._values[(section, key)] = value
        self._sources[(section, key)] = source

    def rows(self):
        for section, fields in SPEC.items():
            for key in fields:
                yield section, key, self._values[(section, key)], \
                    self._sources[(section, key)]


def load(path: str | None = None, env=None) -> Config:
    """Read the file if it exists. Raises ConfigError on anything malformed."""
    path = path or config_path(env)
    cfg = Config(path, os.path.exists(path))
    if not cfg.exists:
        return cfg

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror}") from None

    text = raw.decode("utf-8", "replace")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # tomllib's message already carries "(at line N, column M)".
        raise ConfigError(f"{path}: not valid TOML: {exc}") from None

    for section, table in data.items():
        if section not in SPEC:
            near = difflib.get_close_matches(section, SPEC, 1)
            cfg.warnings.append(
                f"{_at(path, _line_of(text, section, None))}: unknown section "
                f"[{section}] -- ignored" + (f" (did you mean [{near[0]}]?)" if near else ""))
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [{section}] must be a table, "
                              f"got {_typename(table)}")
        for key, value in table.items():
            field = SPEC[section].get(key)
            if field is None:
                near = difflib.get_close_matches(key, SPEC[section], 1)
                cfg.warnings.append(
                    f"{_at(path, _line_of(text, section, key))}: unknown key "
                    f"{key!r} in [{section}] -- ignored"
                    + (f" (did you mean {near[0]!r}?)" if near else ""))
                continue
            try:
                cfg.set(section, key, field.check(value), "file")
            except ValueError as exc:
                raise ConfigError(f"{_at(path, _line_of(text, section, key))}: "
                                  f"[{section}] {key}: {exc}") from None
    return cfg


def apply_flags(cfg: Config, flags: dict[tuple[str, str], object]) -> None:
    """Overlay explicit CLI flags. A None value means "the flag was absent"."""
    for (section, key), value in flags.items():
        if value is None:
            continue
        try:
            cfg.set(section, key, SPEC[section][key].check(value), "flag")
        except ValueError as exc:
            raise ConfigError(f"--{key.replace('_', '-')}: {exc}") from None


# ------------------------------------------------------------------- display
def _show(value) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == "":
        return '""'
    return str(value)


def describe(cfg: Config) -> list[str]:
    """The merged config, one line per key, with where each value came from."""
    lines = [f"config file: {cfg.path}"
             + ("" if cfg.exists else "  (not found -- using built-in defaults)")]
    for section, fields in SPEC.items():
        lines.append("")
        lines.append(f"[{section}]")
        for key in fields:
            lines.append(f"  {key:<13} {_show(cfg.get(section, key)):<16} "
                         f"{cfg.source(section, key)}")
    return lines


# ------------------------------------------------------------------- starter
_STARTER = '''\
# hyprcast configuration.
#
# Every key below is optional: delete the file and hyprcast still runs. A value
# here is overridden by the matching command-line flag, so `hyprcast cast
# --fps 30` wins over fps = 60. `hyprcast config` prints the merged result and
# where each value came from.

[cast]
fps = 60
bitrate = "8M"
mode = "mirror"          # mirror | extend
monitor = ""             # "" = the first output
audio = "device"         # shared | tv-only | device | none
volume = 100             # cast volume; 100 is unity, and the laptop's own
                         # sink volume is never touched
low_power = false
cursors = true           # composite the mouse pointer into the cast
qp = 26                  # CQP quantiser; only read when low_power = true
no_firewall = true       # firewall-cmd times out on this box
width = 1280
height = 720

[wifi]
interface = "p2p-dev-wlan0"
timeout = 60
sink = ""                # "" = discover; otherwise a peer MAC
# Group-owner intent, 0-15. Leaving this OUT is the verified-working path on
# this laptop -- bare `hyprcast cast` negotiates 720p60 with the TV as GO.
# Comment it out again if group formation regresses.
go_intent = 15

[extend]
# only used when mode = "extend"
name = "hyprcast"        # headless output name
position = "auto"        # auto | left | right | above | below
workspace = ""           # "" = none; otherwise move this workspace onto it

[waybar]
format = "{icon} {fps}"
show_bitrate = true
'''


def starter_text() -> str:
    return _STARTER


def write_starter(path: str) -> None:
    """Write a commented starter file. Never clobbers an existing one."""
    if os.path.exists(path):
        raise ConfigError(f"{path} already exists -- refusing to overwrite it")
    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"{directory}: {exc.strerror}") from None
    try:
        # "x" so a file created between the check above and here still wins.
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(_STARTER)
    except FileExistsError:
        raise ConfigError(f"{path} already exists -- refusing to overwrite it") from None
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from None
