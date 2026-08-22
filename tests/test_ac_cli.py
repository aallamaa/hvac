import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import ac_cli
from mit import AirDirection, DriveState, HVACUnit, MNetHTTPError


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

    def test_save_config_backs_up_previous_contents_before_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            backup_path = config_path + ".backup"
            first = {"host": "192.0.2.1", "port": 80, "zones": {}}
            second = {"host": "192.0.2.2", "port": 81, "zones": {}}

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                ac_cli.save_config(first)
                self.assertFalse(os.path.exists(backup_path))

                ac_cli.save_config(second)
                self.assertTrue(os.path.exists(backup_path))
                with open(backup_path) as f:
                    self.assertEqual(json.load(f)["host"], "192.0.2.1")
                with open(config_path) as f:
                    self.assertEqual(json.load(f)["host"], "192.0.2.2")
                self.assertEqual(stat.S_IMODE(os.stat(backup_path).st_mode), 0o600)

    def test_save_config_backup_failure_does_not_block_save(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open(config_path, "w") as f:
                f.write("original\n")

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path), \
                 mock.patch("ac_cli.shutil.copy2", side_effect=OSError("disk full")):
                saved = ac_cli.save_config({"host": "192.0.2.2", "port": 80, "zones": {}})

            with open(config_path) as f:
                self.assertEqual(json.load(f), saved)

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
            # A backup of the pre-existing file is expected; only the
            # failed-rename's temp file must be absent.
            self.assertEqual(sorted(os.listdir(directory)), ["config.json", "config.json.backup"])
            with open(config_path + ".backup") as f:
                self.assertEqual(f.read(), "original\n")

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


class TestLoadConfig(unittest.TestCase):
    def test_explicit_zone_subset_does_not_reintroduce_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open(config_path, "w") as f:
                json.dump({"zones": {"2": "Only zone"}}, f)

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                config = ac_cli.load_config()

        self.assertEqual(list(config["zones"]), ["2"])
        self.assertEqual(ac_cli.groups_from_config(config), [2])

    def test_explicit_empty_zone_set_remains_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open(config_path, "w") as f:
                json.dump({"zones": {}}, f)

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                config = ac_cli.load_config()

        self.assertEqual(config["zones"], {})
        self.assertEqual(ac_cli.groups_from_config(config), [])

    def test_missing_config_marks_zones_for_discovery_without_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "missing.json")

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                config = ac_cli.load_config()

        self.assertEqual(ac_cli.groups_from_config(config), [])
        self.assertTrue(config["_discover_zones"])

    def test_malformed_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open(config_path, "w") as f:
                f.write("{broken")

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                with self.assertRaisesRegex(ValueError, "Cannot load AC config"):
                    ac_cli.load_config()

    def test_invalid_zone_group_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open(config_path, "w") as f:
                json.dump({"zones": {"51": "Unsafe"}}, f)

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                with self.assertRaisesRegex(ValueError, "integer from 1 to 50"):
                    ac_cli.load_config()

    def test_malformed_falsey_config_fields_are_rejected(self):
        invalid_configs = [
            ({"host": ""}, "Invalid M-NET gateway host"),
            ({"host": None}, "Invalid M-NET gateway host"),
            ({"host": False}, "Invalid M-NET gateway host"),
            ({"host": "host with spaces"}, "Invalid M-NET gateway host"),
            ({"port": 0}, "port must be between 1 and 65535"),
            ({"port": False}, "port must be between 1 and 65535"),
            ({"port": None}, "port must be between 1 and 65535"),
            ({"port": 70000}, "port must be between 1 and 65535"),
            ({"icon_temp": ""}, "icon_temp must be one of"),
            ({"icon_temp": None}, "icon_temp must be one of"),
            ({"icon_temp": False}, "icon_temp must be one of"),
            ({"icon_temp": "invalid"}, "icon_temp must be one of"),
            ({"zones": None}, "zones must be a JSON object"),
            ({"zones": []}, "zones must be a JSON object"),
            ({"zones": ""}, "zones must be a JSON object"),
            ({"zones": {"1": None}}, "must be a name or object"),
            ({"zones": {"1": {"name": None}}}, "invalid name"),
            ({"zones": {"1": {"modes": "COOL"}}}, "invalid modes"),
            ({"zones": {"1": {"modes": [False]}}}, "invalid modes"),
            ({"zones": {"1": {"fan_speeds": "HIGH"}}}, "invalid fan speeds"),
            ({"zones": {"1": {"air_directions": "HORIZONTAL"}}}, "invalid air directions"),
        ]
        for cfg, err_match in invalid_configs:
            with self.subTest(cfg=cfg):
                with tempfile.TemporaryDirectory() as directory:
                    config_path = os.path.join(directory, "config.json")
                    with open(config_path, "w") as f:
                        json.dump(cfg, f)
                    with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                        with self.assertRaises(ValueError) as ctx:
                            ac_cli.load_config()
                        self.assertIn(err_match, str(ctx.exception))

    def test_explicit_empty_arrays_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            entry = {
                "name": "Custom",
                "modes": [],
                "fan_speeds": [],
                "air_directions": [],
            }
            with open(config_path, "w") as f:
                json.dump({"zones": {"1": entry}}, f)

            with mock.patch.object(ac_cli, "CONFIG_FILE", config_path):
                config = ac_cli.load_config()
                self.assertEqual(config["zones"]["1"]["modes"], [])
                self.assertEqual(config["zones"]["1"]["fan_speeds"], [])
                self.assertEqual(config["zones"]["1"]["air_directions"], [])

                saved = ac_cli.save_config(config)
                self.assertEqual(saved["zones"]["1"]["modes"], [])
                self.assertEqual(saved["zones"]["1"]["fan_speeds"], [])
                self.assertEqual(saved["zones"]["1"]["air_directions"], [])


class TestZoneDiscovery(unittest.TestCase):
    def test_missing_zone_key_discovers_and_persists_only_found_groups(self):
        config = dict(ac_cli.DEFAULT_CONFIG)
        config["zones"] = {}
        config["_discover_zones"] = True

        info = {
            2: {"name": "Kitchen"},
            9: {"name": None},
        }
        with mock.patch("ac_cli.discover_zone_info", return_value=info) as discover, \
             mock.patch("ac_cli.save_config") as save:
            ac_cli.ensure_discovered_zones(config)

        discover.assert_called_once_with(config, 50)
        self.assertEqual(list(config["zones"]), ["2", "9"])
        self.assertNotIn("_discover_zones", config)
        save.assert_called_once_with(config)

    def test_explicit_empty_zones_do_not_trigger_discovery(self):
        config = {"zones": {}}
        with mock.patch("ac_cli.discover") as discover:
            ac_cli.ensure_discovered_zones(config)
        discover.assert_not_called()

    def test_no_discovered_groups_does_not_persist_empty_authoritative_set(self):
        config = {"zones": {}, "_discover_zones": True}
        with mock.patch("ac_cli.discover_zone_info", return_value={}), \
             mock.patch("ac_cli.save_config") as save:
            with self.assertRaisesRegex(RuntimeError, "No configured"):
                ac_cli.ensure_discovered_zones(config)
        save.assert_not_called()

    def test_inventory_discovery_excludes_non_ic_units(self):
        ctrl = mock.Mock()
        ctrl.get_group_inventory.return_value = {
            1: {"name": "Room 1", "model": "IC"},
            2: {"name": "Outdoor", "model": "OC"},
            3: {"name": "Lossnay", "model": "LC"},
            6: {"name": "Room 6", "model": "IC"},
        }
        config = {"host": "192.0.2.1", "port": 80, "zones": {}}
        with mock.patch("ac_cli.controller", return_value=ctrl):
            found = ac_cli.discover_zone_info(config, 50)

        self.assertEqual(sorted(found.keys()), [1, 6])
        self.assertEqual(found[1]["model"], "IC")
        self.assertEqual(found[6]["model"], "IC")

    def test_inventory_without_models_uses_group_models_to_exclude_non_ic(self):
        ctrl = mock.Mock()
        ctrl.get_group_inventory.return_value = {
            1: {"name": "Room 1", "model": None},
            2: {"name": "Outdoor", "model": None},
        }
        ctrl.get_group_models.return_value = {1: "IC", 2: "OC"}
        config = {"host": "192.0.2.1", "port": 80, "zones": {}}
        with mock.patch("ac_cli.controller", return_value=ctrl):
            found = ac_cli.discover_zone_info(config, 50)

        self.assertEqual(list(found.keys()), [1])
        ctrl.get_group_models.assert_called_once_with([1, 2])

    def test_mixed_inventory_resolves_only_missing_models(self):
        ctrl = mock.Mock()
        inventory = {
            1: {"name": "Known indoor", "model": "IC"},
            2: {"name": "Unknown indoor", "model": None},
            3: {"name": "Known outdoor", "model": "OC"},
        }
        ctrl.get_group_inventory.return_value = inventory
        ctrl.get_group_models.return_value = {2: "IC"}
        config = {"host": "192.0.2.1", "port": 80, "zones": {}}
        with mock.patch("ac_cli.controller", return_value=ctrl):
            found = ac_cli.discover_zone_info(config, 50)

        self.assertEqual(found, {1: inventory[1], 2: inventory[2]})
        ctrl.get_group_models.assert_called_once_with([2])

    def test_names_discovery_excludes_non_ic(self):
        ctrl = mock.Mock()
        ctrl.get_group_inventory.side_effect = RuntimeError("no inventory")
        ctrl.get_group_names.return_value = {
            1: {"name": "Room 1", "model": None},
            2: {"name": "Outdoor", "model": None},
        }
        ctrl.get_group_models.return_value = {1: "IC", 2: "OC"}
        config = {"host": "192.0.2.1", "port": 80, "zones": {}}
        with mock.patch("ac_cli.controller", return_value=ctrl):
            found = ac_cli.discover_zone_info(config, 50)

        self.assertEqual(list(found.keys()), [1])

    def test_probe_discovery_excludes_non_ic(self):
        ctrl = mock.Mock()
        ctrl.get_group_inventory.side_effect = RuntimeError("no inventory")
        ctrl.get_group_names.side_effect = RuntimeError("no names")
        ctrl.get_group_models.side_effect = lambda g: {g[0]: "IC"} if g == [1] else ({g[0]: "OC"} if g == [2] else {})
        config = {"host": "192.0.2.1", "port": 80, "zones": {}}
        with mock.patch("ac_cli.controller", return_value=ctrl):
            found = ac_cli.discover_zone_info(config, 3)

        self.assertEqual(list(found.keys()), [1])


class TestBulkPower(unittest.TestCase):
    def test_one_group_failure_does_not_skip_later_groups(self):
        ctrl = mock.Mock()
        ctrl.turn_off.side_effect = [True, RuntimeError("zone failed"), True]
        result = ac_cli.bulk_power(ctrl, [1, 2, 3], False)
        self.assertEqual(result[1], True)
        self.assertEqual(result[2], {"error": "zone failed"})
        self.assertEqual(result[3], True)
        self.assertEqual(ctrl.turn_off.call_count, 3)


class TestStatusFallback(unittest.TestCase):
    @staticmethod
    def gateway_500():
        return MNetHTTPError(500, "M-NET communication failed")

    def test_batch_http_500_falls_back_to_each_group(self):
        ctrl = mock.Mock()
        unit1 = HVACUnit(group=1, drive=DriveState.ON)
        unit3 = HVACUnit(group=3, drive=DriveState.OFF)
        ctrl.get_status.side_effect = [
            self.gateway_500(), [unit1], self.gateway_500(), [unit3]
        ]
        self.assertEqual(ac_cli.get_status_resilient(ctrl, [1, 2, 3]), [unit1, unit3])
        self.assertEqual(ctrl.get_status.call_args_list, [
            mock.call([1, 2, 3]), mock.call([1]), mock.call([2]), mock.call([3])
        ])

    def test_all_individual_http_500_status_fallbacks_raise(self):
        ctrl = mock.Mock()
        ctrl.get_status.side_effect = [
            self.gateway_500(), self.gateway_500(), self.gateway_500()
        ]
        with self.assertRaises(MNetHTTPError) as ctx:
            ac_cli.get_status_resilient(ctrl, [1, 2])
        self.assertEqual(ctx.exception.status_code, 500)

    def test_direct_control_requires_configured_group(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            ac_cli.require_configured_group({"zones": {}}, 1)
        ac_cli.require_configured_group({"zones": {"2": {}}}, 2)


class TestDiscoverFeatures(unittest.TestCase):
    def test_capabilities_are_derived_without_hardware_writes(self):
        ctrl = mock.Mock()
        ctrl.get_capabilities.return_value = {
            1: {
                "Model": "IC", "IcKind": "COOL", "DryModeSW": "ENABLE",
                "FanModeSW": "ENABLE", "AutoModeSW": "DISABLE",
                "FanSpeedSW": "3STAGES", "FanAutoSW": "ENABLE",
                "AirDirectionSW": "DISABLE", "SwingSW": "DISABLE",
                "FilterSign": "ON", "ErrorSign": "OFF",
            },
            6: {
                "Model": "IC", "IcKind": "NORMAL", "DryModeSW": "ENABLE",
                "FanModeSW": "ENABLE", "AutoModeSW": "ENABLE",
                "FanSpeedSW": "4STAGES", "FanAutoSW": "DISABLE",
                "AirDirectionSW": "ENABLE", "SwingSW": "ENABLE",
                "FilterSign": "OFF", "ErrorSign": "OFF",
            },
        }
        config = {"zones": {
            "1": ac_cli.normalize_zone("Cool only", 1),
            "6": ac_cli.normalize_zone("Heat pump", 6),
        }}
        with mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("ac_cli.save_config") as save:
            result = ac_cli.discover_features(config, [1, 6])

        self.assertEqual(result[1]["modes"], ["COOL", "DRY", "FAN"])
        self.assertEqual(result[1]["fan_speeds"], ["AUTO", "MID1", "MID2", "HIGH"])
        self.assertEqual(result[6]["modes"], ["COOL", "HEAT", "DRY", "FAN", "AUTO"])
        self.assertEqual(result[6]["fan_speeds"], ["LOW", "MID1", "MID2", "HIGH"])
        self.assertEqual(result[1]["air_directions"], [])
        self.assertEqual(
            result[6]["air_directions"],
            ["HORIZONTAL", "MID1", "MID2", "VERTICAL", "SWING"],
        )
        ctrl.get_capabilities.assert_called_once_with([1, 6])
        ctrl.turn_off.assert_not_called()
        ctrl.set_mode.assert_not_called()
        save.assert_called_once_with(config)

    def test_unknown_or_non_ic_capabilities_are_rejected(self):
        ctrl = mock.Mock()
        ctrl.get_capabilities.return_value = {
            1: {"Model": "OC", "IcKind": "COOL"},
        }
        config = {"zones": {"1": ac_cli.normalize_zone("Outdoor unit", 1)}}
        with mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("ac_cli.save_config") as save:
            with self.assertRaisesRegex(RuntimeError, "Unsupported M-NET unit type"):
                ac_cli.discover_features(config, [1])
            save.assert_not_called()

        ctrl.get_capabilities.return_value = {
            1: {"Model": "IC", "IcKind": "UNKNOWN"},
        }
        with mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("ac_cli.save_config") as save:
            with self.assertRaisesRegex(RuntimeError, "Unsupported M-NET unit type"):
                ac_cli.discover_features(config, [1])
            save.assert_not_called()

        ctrl.get_capabilities.return_value = {}
        with mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("ac_cli.save_config") as save:
            with self.assertRaisesRegex(RuntimeError, "returned no capabilities"):
                ac_cli.discover_features(config, [1])
            save.assert_not_called()


class TestCliCommands(unittest.TestCase):
    def test_set_air_dispatches_only_a_discovered_direction(self):
        config = {
            "host": "192.0.2.1", "port": 80, "icon_temp": "avg",
            "zones": {"1": {
                "name": "Room 1", "modes": ["COOL"],
                "fan_speeds": ["AUTO"], "air_directions": ["HORIZONTAL"],
            }},
        }
        ctrl = mock.Mock()
        ctrl.set_air_direction.return_value = True
        with mock.patch("sys.argv", ["ac_cli.py", "set-air", "1", "HORIZONTAL"]), \
             mock.patch("ac_cli.load_config", return_value=config), \
             mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("builtins.print"):
            ac_cli.main()

        ctrl.set_air_direction.assert_called_once_with(1, AirDirection.HORIZONTAL)

    def test_set_air_rejects_an_undiscovered_direction(self):
        config = {
            "host": "192.0.2.1", "port": 80, "icon_temp": "avg",
            "zones": {"1": {
                "name": "Room 1", "modes": ["COOL"],
                "fan_speeds": ["AUTO"], "air_directions": [],
            }},
        }
        ctrl = mock.Mock()
        with mock.patch("sys.argv", ["ac_cli.py", "set-air", "1", "HORIZONTAL"]), \
             mock.patch("ac_cli.load_config", return_value=config), \
             mock.patch("ac_cli.controller", return_value=ctrl), \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as ctx:
                ac_cli.main()

        self.assertEqual(ctx.exception.code, 1)
        ctrl.set_air_direction.assert_not_called()

    def test_status_serialization_includes_air_filter_and_fault_fields(self):
        unit = HVACUnit(
            group=1, air_direction=AirDirection.HORIZONTAL,
            filter_sign="ON", error_sign="OFF",
        )
        zone = {
            "name": "Room 1", "modes": [], "fan_speeds": [],
            "air_directions": ["HORIZONTAL"],
        }

        result = ac_cli.unit_to_dict(unit, zone)

        self.assertEqual(result["air_direction"], "HORIZONTAL")
        self.assertEqual(result["air_directions"], ["HORIZONTAL"])
        self.assertEqual(result["filter_sign"], "ON")
        self.assertEqual(result["error_sign"], "OFF")

    def test_invalid_set_host_cannot_mutate_config(self):
        config = dict(ac_cli.DEFAULT_CONFIG)
        with mock.patch("sys.argv", ["ac_cli.py", "set-host", "invalid host@name"]), \
             mock.patch("ac_cli.load_config", return_value=config), \
             mock.patch("ac_cli.save_config") as save, \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as ctx:
                ac_cli.main()
            self.assertEqual(ctx.exception.code, 1)
            save.assert_not_called()

    def test_rename_cannot_create_undiscovered_group(self):
        config = {"zones": {"1": ac_cli.normalize_zone("Room 1", 1)}}
        with mock.patch("sys.argv", ["ac_cli.py", "rename", "2", "New Room"]), \
             mock.patch("ac_cli.load_config", return_value=config), \
             mock.patch("ac_cli.save_config") as save, \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as ctx:
                ac_cli.main()
            self.assertEqual(ctx.exception.code, 1)
            save.assert_not_called()

    def test_empty_zones_init_is_rejected(self):
        config = {"host": "192.0.2.1", "port": 80, "zones": {}}
        with mock.patch("sys.argv", ["ac_cli.py", "zones-init"]), \
             mock.patch("ac_cli.load_config", return_value=config), \
             mock.patch("ac_cli.discover_zone_info", return_value={}), \
             mock.patch("ac_cli.save_config") as save, \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as ctx:
                ac_cli.main()
            self.assertEqual(ctx.exception.code, 1)
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
