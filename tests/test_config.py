"""
Config precedence, validation, and the argparse trap.

The trap is the whole reason this file exists: argparse cannot distinguish
"the user typed --fps 60" from "60 is the default", so a naive merge would let
the default silently outrank a config file. FlagOverridesFileTest proves both
directions, including the case where the flag's value happens to equal the
built-in default.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from hyprcast import config, session  # noqa: E402
from hyprcast.__main__ import _merge_config, build_parser, cmd_config  # noqa: E402


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "config.toml")
        patch = mock.patch.dict(os.environ, {"HYPRCAST_CONFIG": self.path})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def cast(self, *argv):
        """Parse `hyprcast cast ...` and merge the config into it."""
        args = build_parser().parse_args(["cast", *argv])
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = _merge_config(args)
        return args, cfg, err.getvalue()


# --------------------------------------------------------------- precedence
class DefaultsTest(ConfigCase):
    def test_missing_file_is_not_an_error(self):
        cfg = config.load()
        self.assertFalse(cfg.exists)
        self.assertEqual(cfg.get("cast", "fps"), 60)
        self.assertEqual(cfg.source("cast", "fps"), "default")
        self.assertEqual(cfg.warnings, [])

    def test_defaults_match_the_pre_config_behaviour(self):
        # If these drift, "no config file" stops being a no-op.
        cfg = config.load()
        self.assertEqual((cfg.get("cast", "width"), cfg.get("cast", "height")),
                         session.DEFAULT_WIRE)
        self.assertEqual(cfg.get("cast", "fps"), session.DEFAULT_FPS)
        self.assertEqual(session.parse_bitrate(cfg.get("cast", "bitrate")),
                         session.DEFAULT_BITRATE)
        self.assertIsNone(cfg.get("cast", "qp"))
        self.assertIsNone(cfg.get("wifi", "go_intent"))
        self.assertFalse(cfg.get("cast", "no_firewall"))
        self.assertEqual(cfg.get("extend", "name"), session.HEADLESS_NAME)


class FileValueUsedTest(ConfigCase):
    def test_file_wins_over_default_when_the_flag_is_absent(self):
        self.write('[cast]\nfps = 30\nbitrate = "6M"\n\n[wifi]\ntimeout = 15\n')
        args, cfg, _ = self.cast()
        self.assertEqual(args.fps, 30)
        self.assertEqual(args.bitrate, "6M")
        self.assertEqual(args.timeout, 15)
        self.assertEqual(cfg.source("cast", "fps"), "file")
        self.assertEqual(cfg.source("cast", "width"), "default")


class FlagOverridesFileTest(ConfigCase):
    def test_flag_wins_over_file(self):
        self.write("[cast]\nfps = 30\n")
        args, cfg, _ = self.cast("--fps", "24")
        self.assertEqual(args.fps, 24)
        self.assertEqual(cfg.source("cast", "fps"), "flag")

    def test_flag_equal_to_the_builtin_default_still_wins(self):
        # The exact case argparse's own defaults cannot express: the file says
        # 30, the user explicitly typed the default, 60 must win.
        self.write("[cast]\nfps = 30\n")
        args, cfg, _ = self.cast("--fps", "60")
        self.assertEqual(args.fps, 60)
        self.assertEqual(cfg.source("cast", "fps"), "flag")

    def test_store_true_flag_is_distinguishable_from_absent(self):
        self.write("[cast]\nlow_power = true\nno_firewall = true\n")
        args, cfg, _ = self.cast()
        self.assertTrue(args.low_power)
        self.assertEqual(cfg.source("cast", "low_power"), "file")

        self.write("[cast]\nlow_power = false\n")
        args, cfg, _ = self.cast("--low-power")
        self.assertTrue(args.low_power)
        self.assertEqual(cfg.source("cast", "low_power"), "flag")

        args, cfg, _ = self.cast()
        self.assertFalse(args.low_power)
        self.assertEqual(cfg.source("cast", "low_power"), "file")


class ModeAndSinkTest(ConfigCase):
    def test_mode_extend_sets_second_screen(self):
        self.write('[cast]\nmode = "extend"\n')
        args, _, _ = self.cast()
        self.assertTrue(args.second_screen)

    def test_second_screen_flag_is_an_alias_for_mode_extend(self):
        args, cfg, _ = self.cast("--second-screen")
        self.assertEqual(cfg.get("cast", "mode"), "extend")
        self.assertEqual(cfg.source("cast", "mode"), "flag")

    def test_mode_flag_overrides_an_extend_file(self):
        self.write('[cast]\nmode = "extend"\n')
        args, _, _ = self.cast("--mode", "mirror")
        self.assertFalse(args.second_screen)

    def test_empty_sink_becomes_none_not_empty_string(self):
        # _scan_and_select() reads None as "discover"; "" would be treated as a
        # peer selector and fail three scans.
        self.write('[wifi]\nsink = ""\n')
        args, _, _ = self.cast()
        self.assertIsNone(args.sink)

    def test_sink_from_the_file_reaches_args(self):
        self.write('[wifi]\nsink = "aa:bb:cc:dd:ee:ff"\n')
        args, _, _ = self.cast()
        self.assertEqual(args.sink, "aa:bb:cc:dd:ee:ff")


# --------------------------------------------------------------- validation
class MalformedIsFatalTest(ConfigCase):
    def assertFatal(self, text, *needles):
        self.write(text)
        with self.assertRaises(config.ConfigError) as caught:
            config.load()
        message = str(caught.exception)
        for needle in needles:
            self.assertIn(needle, message)
        return message

    def test_string_where_an_integer_belongs(self):
        message = self.assertFatal('# hyprcast\n[cast]\nfps = "sixty"\n',
                                   "fps", "expected an integer", "string")
        self.assertIn(f"{self.path}:3", message)

    def test_boolean_is_not_an_integer(self):
        self.assertFatal("[cast]\nfps = true\n", "expected an integer", "boolean")

    def test_out_of_range_is_fatal(self):
        self.assertFatal("[wifi]\ngo_intent = 99\n", "0..15")

    def test_bad_choice_names_the_alternatives(self):
        self.assertFatal('[cast]\naudio = "loud"\n', "audio", "'shared'")

    def test_unparseable_bitrate_is_fatal(self):
        self.assertFatal('[cast]\nbitrate = "plenty"\n', "bitrate")

    def test_broken_toml_is_fatal(self):
        self.assertFatal("[cast\nfps = 60\n", "not valid TOML")

    def test_the_reported_line_is_the_right_table(self):
        # Same key name in two tables: the error must point at [wifi], line 6.
        message = self.assertFatal(
            "[cast]\n"
            "fps = 60\n"
            "monitor = \"eDP-1\"\n"
            "\n"
            "[wifi]\n"
            "timeout = \"soon\"\n",
            "[wifi] timeout")
        self.assertIn(f"{self.path}:6", message)


class ValidValuesTest(ConfigCase):
    def test_integer_bitrate_is_normalised_to_text(self):
        # wfd._bitrate_to_kbits() regex-matches a str; an int would raise.
        self.write("[cast]\nbitrate = 6000000\n")
        cfg = config.load()
        self.assertEqual(cfg.get("cast", "bitrate"), "6000000")
        self.assertEqual(session.parse_bitrate(cfg.get("cast", "bitrate")), 6_000_000)


class UnknownKeysWarnTest(ConfigCase):
    def test_unknown_key_warns_and_the_file_still_loads(self):
        self.write("[cast]\nfps = 30\nfpz = 99\n")
        cfg = config.load()
        self.assertEqual(cfg.get("cast", "fps"), 30)
        self.assertEqual(len(cfg.warnings), 1)
        self.assertIn("unknown key 'fpz'", cfg.warnings[0])
        self.assertIn("did you mean 'fps'?", cfg.warnings[0])
        self.assertIn(f"{self.path}:3", cfg.warnings[0])

    def test_unknown_section_warns(self):
        self.write("[casts]\nfps = 30\n")
        cfg = config.load()
        self.assertEqual(cfg.get("cast", "fps"), 60)
        self.assertIn("unknown section [casts]", cfg.warnings[0])

    def test_warnings_reach_stderr_but_do_not_stop_a_cast(self):
        self.write("[cast]\nfpz = 99\n")
        _, _, stderr = self.cast()
        self.assertIn("warning", stderr)
        self.assertIn("unknown key 'fpz'", stderr)


class BadFlagIsFatalTest(ConfigCase):
    def test_flag_out_of_range_is_rejected_like_a_file_value(self):
        args = build_parser().parse_args(["cast", "--fps", "0"])
        with self.assertRaises(config.ConfigError) as caught:
            _merge_config(args)
        self.assertIn("--fps", str(caught.exception))


# ------------------------------------------------------------ the subcommand
class ConfigCommandTest(ConfigCase):
    def run_cmd(self, *argv):
        args = build_parser().parse_args(["config", *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_config(args)
        return rc, out.getvalue(), err.getvalue()

    def test_path_prints_the_lookup_location(self):
        rc, out, _ = self.run_cmd("--path")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), self.path)

    def test_effective_config_shows_every_source(self):
        self.write("[cast]\nfps = 30\n")
        rc, out, _ = self.run_cmd("--bitrate", "6M")
        self.assertEqual(rc, 0)
        self.assertIn("fps           30", out)
        self.assertRegex(out, r"fps\s+30\s+file")
        self.assertRegex(out, r"bitrate\s+6M\s+flag")
        self.assertRegex(out, r"width\s+1280\s+default")

    def test_init_writes_then_refuses_to_clobber(self):
        rc, out, _ = self.run_cmd("--init")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.path))
        rc, _, err = self.run_cmd("--init")
        self.assertEqual(rc, 1)
        self.assertIn("refusing to overwrite", err)

    def test_the_starter_file_is_valid_and_warning_free(self):
        config.write_starter(self.path)
        cfg = config.load()
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.get("cast", "fps"), 60)
        self.assertEqual(cfg.get("cast", "mode"), "mirror")
        # Every key in the schema is present in the starter file.
        for section, key, _, source in cfg.rows():
            self.assertEqual(source, "file", f"[{section}] {key} missing from --init")


if __name__ == "__main__":
    unittest.main()
