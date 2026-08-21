import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import ac_cli
from mit import DriveState, FanSpeed, HVACUnit, Mode


class TestSaveConfig(unittest.TestCase):
    def test_save_config_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            config = {
                "host": "192.0.2.1",
                "port": 80,
                "icon_temp": "avg",
                "zones": {
                    "1": {
                        "name": "Private room",
                        "modes": ["COOL"],
                        "fan_speeds": ["AUTO"],
                    }
                },
            }

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path), \
                 mock.patch("ac_cli.os.fsync", wraps=os.fsync) as mock_fsync:
                saved = ac_cli.save_config(config)

            with open(config_path) as f:
                self.assertEqual(json.load(f), saved)
            self.assertEqual(stat.S_IMODE(os.stat(config_path).st_mode), 0o600)
            self.assertEqual(os.listdir(directory), ["config.json"])
            # Once for the temporary file contents, once for the directory
            # entry created by os.replace().
            self.assertEqual(mock_fsync.call_count, 2)

    def test_failed_replace_preserves_existing_config_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open(config_path, "w") as f:
                f.write("original\n")

            config = {
                "host": "192.0.2.1",
                "port": 80,
                "zones": {},
            }
            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path), \
                 mock.patch("ac_cli.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    ac_cli.save_config(config)

            with open(config_path) as f:
                self.assertEqual(f.read(), "original\n")
            self.assertEqual(os.listdir(directory), ["config.json"])

    def test_default_config_directory_is_tightened_but_override_parent_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            default_dir = os.path.join(directory, "default")
            default_path = os.path.join(default_dir, "config.json")
            os.makedirs(default_dir, mode=0o755)
            os.chmod(default_dir, 0o755)
            config = {"host": "192.0.2.1", "port": 80, "zones": {}}

            with mock.patch.object(ac_cli, "DEFAULT_CONFIG_FILE", default_path), \
                 mock.patch.object(ac_cli, "CONFIG_FILE", default_path):
                ac_cli.save_config(config)
            self.assertEqual(stat.S_IMODE(os.stat(default_dir).st_mode), 0o700)

            override_dir = os.path.join(directory, "shared")
            override_path = os.path.join(override_dir, "config.json")
            os.makedirs(override_dir, mode=0o755)
            os.chmod(override_dir, 0o755)
            with mock.patch.object(ac_cli, "DEFAULT_CONFIG_FILE", default_path), \
                 mock.patch.object(ac_cli, "CONFIG_FILE", override_path):
                ac_cli.save_config(config)
            self.assertEqual(stat.S_IMODE(os.stat(override_dir).st_mode), 0o755)


class FakeController:
    def __init__(self, fail_restore=None):
        self.calls = []
        self.fail_restore = fail_restore
        self.status_calls = 0
        self.original = HVACUnit(
            group=1,
            drive=DriveState.ON,
            mode=Mode.COOL,
            set_temp=22,
            fan_speed=FanSpeed.AUTO,
        )

    def get_unit_status(self, group):
        self.status_calls += 1
        if self.status_calls == 1:
            return self.original
        raise RuntimeError("probe failed")

    def turn_off(self, group):
        self.calls.append(("off", group))

    def turn_on(self, group):
        self.calls.append(("on", group))
        if self.fail_restore == "power":
            raise RuntimeError("power restore failed")

    def set_mode(self, group, mode):
        self.calls.append(("mode", group, mode))
        if self.status_calls > 1 and mode == self.original.mode and self.fail_restore == "mode":
            raise RuntimeError("mode restore failed")

    def set_fan_speed(self, group, speed):
        self.calls.append(("fan", group, speed))

    def set_temperature(self, group, temperature):
        self.calls.append(("temp", group, temperature))


class TestDiscoverFeaturesRestoration(unittest.TestCase):
    def test_probe_failure_restores_every_original_setting(self):
        ctrl = FakeController()
        config = {"zones": {"1": ac_cli.normalize_zone("Zone 1", 1)}}

        with mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("ac_cli.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                ac_cli.discover_features(config, [1])

        self.assertIn(("mode", 1, Mode.COOL), ctrl.calls)
        self.assertIn(("fan", 1, FanSpeed.AUTO), ctrl.calls)
        self.assertIn(("temp", 1, 22), ctrl.calls)
        self.assertIn(("on", 1), ctrl.calls)

    def test_one_restore_failure_does_not_skip_later_steps(self):
        ctrl = FakeController(fail_restore="mode")
        config = {"zones": {"1": ac_cli.normalize_zone("Zone 1", 1)}}

        with mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("ac_cli.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "probe failed") as caught:
                ac_cli.discover_features(config, [1])

        self.assertIn("probe failed", str(caught.exception))
        self.assertIn("State restoration also failed", str(caught.exception))
        self.assertIn("mode restore failed", str(caught.exception))
        self.assertIn(("fan", 1, FanSpeed.AUTO), ctrl.calls)
        self.assertIn(("temp", 1, 22), ctrl.calls)
        self.assertIn(("on", 1), ctrl.calls)


if __name__ == "__main__":
    unittest.main()
