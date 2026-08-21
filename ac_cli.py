#!/usr/bin/env python3
"""CLI wrapper around mit.py's MNetController, for the AC waybar/omarchy plugin.

Every subcommand prints one JSON value to stdout and exits 0 on success.
On failure it prints {"error": "..."} to stderr and exits 1.

Gateway host/port and zone (group -> name/capabilities) config live in a
single JSON file instead of being hardcoded, so the hardware setup can
change without touching this script or the QML. See CONFIG_FILE below.
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mit import MNetController, Mode, FanSpeed

DEFAULT_CONFIG_FILE = os.path.expanduser("~/.config/ac-plugin/config.json")
CONFIG_FILE = os.environ.get("AC_CONFIG_FILE", DEFAULT_CONFIG_FILE)

# Real, user-facing write targets. AUTOCOOL/AUTOHEAT are excluded on purpose:
# per Mitsubishi's own M-NET BACnet gateway spec they're "Not settable" -
# read-only status the unit reports while running under AUTO, never
# something you write.
ALL_MODES = ["COOL", "HEAT", "DRY", "FAN", "AUTO"]
ALL_FAN_SPEEDS = ["AUTO", "LOW", "MID1", "MID2", "HIGH"]

# Fallback for a zone that hasn't been through `discover-features` yet.
# Narrower than the full candidate lists above - COOL/DRY/FAN and
# AUTO/MID1/MID2/HIGH are confirmed working on this installation's units,
# whereas HEAT/AUTO mode and LOW fan speed are known to be silently
# rejected by at least one of them.
DEFAULT_MODES = ["COOL", "DRY", "FAN"]
DEFAULT_FAN_SPEEDS = ["AUTO", "MID1", "MID2", "HIGH"]

DEFAULT_CONFIG = {
    "host": "192.168.1.2",
    "port": 80,
    # What the bar icon shows next to the snowflake for the zones that are
    # currently on: "avg" (default), "min", "max", or "off" (icon only).
    "icon_temp": "avg",
    "zones": {
        "1": "Zone 1",
        "2": "Zone 2",
        "3": "Zone 3",
        "4": "Zone 4",
        "5": "Zone 5",
        "6": "Zone 6",
    },
}

ICON_TEMP_MODES = ("avg", "min", "max", "off")

DISCOVER_MAX = 16

# Gap between a set and the status read that checks it. The gateway has been
# observed to need a moment to reflect a write; without this, a supported
# value can read back as not-yet-applied and get misclassified as rejected.
FEATURE_TEST_DELAY = 0.4


def normalize_zone(entry, group):
    """Upgrade a zone config entry to the {name, modes, fan_speeds} shape.

    Older configs (and DEFAULT_CONFIG) store zones as a plain name string;
    this fills in the not-yet-discovered defaults for those.
    """
    if isinstance(entry, str):
        return {"name": entry, "modes": list(DEFAULT_MODES), "fan_speeds": list(DEFAULT_FAN_SPEEDS)}
    return {
        "name": entry.get("name") or "Zone {}".format(group),
        "modes": entry.get("modes") or list(DEFAULT_MODES),
        "fan_speeds": entry.get("fan_speeds") or list(DEFAULT_FAN_SPEEDS),
    }


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in data.items() if k != "zones"})
    zones_raw = dict(DEFAULT_CONFIG["zones"])
    zones_raw.update(data.get("zones") or {})
    config["zones"] = {g: normalize_zone(v, g) for g, v in zones_raw.items()}

    # Env vars win over the file, for one-off overrides without editing it.
    if os.environ.get("AC_HOST"):
        config["host"] = os.environ["AC_HOST"]
    if os.environ.get("AC_PORT"):
        config["port"] = int(os.environ["AC_PORT"])

    return config


def save_config(config):
    config_dir = os.path.dirname(os.path.abspath(CONFIG_FILE))
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    if os.path.abspath(CONFIG_FILE) == os.path.abspath(DEFAULT_CONFIG_FILE):
        # Tighten directories created by older versions without changing an
        # arbitrary parent directory selected through AC_CONFIG_FILE.
        os.chmod(config_dir, 0o700)
    zones_out = {
        g: {"name": z["name"], "modes": z["modes"], "fan_speeds": z["fan_speeds"]}
        for g, z in config["zones"].items()
    }
    to_write = {
        "host": config["host"],
        "port": config["port"],
        "icon_temp": config.get("icon_temp", "avg"),
        "zones": dict(sorted(zones_out.items(), key=lambda kv: int(kv[0]))),
    }
    fd, temp_path = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=config_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            fd = None
            json.dump(to_write, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_FILE)
        temp_path = None
        os.chmod(CONFIG_FILE, 0o600)
        directory_fd = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return to_write


def controller(config):
    return MNetController(config["host"], config["port"])


def groups_from_config(config):
    return sorted(int(g) for g in config["zones"].keys())


def unit_to_dict(u, zone_cfg):
    return {
        "group": u.group,
        "name": zone_cfg["name"],
        "modes": zone_cfg["modes"],
        "fan_speeds": zone_cfg["fan_speeds"],
        "drive": u.drive.value if u.drive else None,
        "mode": u.mode.value if u.mode else None,
        "set_temp": u.set_temp,
        "fan_speed": u.fan_speed.value if u.fan_speed else None,
        "inlet_temp": u.inlet_temp,
    }


def discover(config, max_group):
    """Probe groups 1..max_group one at a time and return the ones that respond.

    A single batched query fails outright (HTTP 500) if it includes even one
    nonexistent group, so discovery has to go one group per request.
    """
    ctrl = controller(config)
    found = []
    for g in range(1, max_group + 1):
        try:
            units = ctrl.get_status([g])
            if units:
                found.append(g)
        except Exception:
            continue
    return found


def discover_features(config, groups):
    """Probe which modes and fan speeds each group actually accepts.

    For each group: turn it off (a setting that doesn't stick is easiest to
    tell apart from one that does when the compressor isn't also changing
    the room), try every candidate value, and read back status to see
    whether it took. Restores the zone's original mode/fan speed/set temp/
    drive state when done, and saves each zone's results to the config as
    it finishes so an interruption doesn't lose earlier zones' results.
    """
    ctrl = controller(config)
    results = {}
    for g in groups:
        original = ctrl.get_unit_status(g)
        was_on = original.drive is not None and original.drive.value == "ON"
        supported_modes = []
        supported_fans = []
        probe_error = None
        restore_errors = []
        try:
            if was_on:
                ctrl.turn_off(g)
                time.sleep(FEATURE_TEST_DELAY)

            for m in ALL_MODES:
                ctrl.set_mode(g, Mode(m))
                time.sleep(FEATURE_TEST_DELAY)
                check = ctrl.get_unit_status(g)
                if check.mode is not None and check.mode.value == m:
                    supported_modes.append(m)

            for speed in ALL_FAN_SPEEDS:
                ctrl.set_fan_speed(g, FanSpeed(speed))
                time.sleep(FEATURE_TEST_DELAY)
                check = ctrl.get_unit_status(g)
                if check.fan_speed is not None and check.fan_speed.value == speed:
                    supported_fans.append(speed)
        except BaseException as e:
            probe_error = e
        finally:
            restore_steps = []
            if original.mode is not None:
                restore_steps.append(("mode", lambda: ctrl.set_mode(g, original.mode)))
            if original.fan_speed is not None:
                restore_steps.append(("fan speed", lambda: ctrl.set_fan_speed(g, original.fan_speed)))
            if original.set_temp is not None:
                restore_steps.append(("temperature", lambda: ctrl.set_temperature(g, original.set_temp)))
            if original.drive is not None:
                restore_power = ctrl.turn_on if was_on else ctrl.turn_off
                restore_steps.append(("power", lambda: restore_power(g)))

            for label, restore in restore_steps:
                try:
                    restore()
                except Exception as e:
                    restore_errors.append(f"{label}: {e}")

        if probe_error is not None:
            if restore_errors:
                restoration_note = "State restoration also failed: " + "; ".join(restore_errors)
                combined_message = f"{probe_error}. {restoration_note}"
                if isinstance(probe_error, Exception):
                    raise RuntimeError(combined_message) from probe_error
                else:
                    # Preserve KeyboardInterrupt/SystemExit semantics while
                    # still making restoration failures visible to the CLI.
                    probe_error.args = (combined_message,)
            raise probe_error
        if restore_errors:
            raise RuntimeError("Failed to restore zone {} state: {}".format(g, "; ".join(restore_errors)))

        key = str(g)
        zone_cfg = config["zones"].get(key) or normalize_zone("Zone {}".format(g), g)
        zone_cfg["modes"] = supported_modes
        zone_cfg["fan_speeds"] = supported_fans
        config["zones"][key] = zone_cfg
        results[g] = {"modes": supported_modes, "fan_speeds": supported_fans}
        save_config(config)

    return results


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("all-on")
    sub.add_parser("all-off")
    sub.add_parser("config")

    for name in ("on", "off"):
        p = sub.add_parser(name)
        p.add_argument("group", type=int)

    p = sub.add_parser("set-temp")
    p.add_argument("group", type=int)
    p.add_argument("temp", type=int)

    p = sub.add_parser("set-fan")
    p.add_argument("group", type=int)
    p.add_argument("speed")

    p = sub.add_parser("set-mode")
    p.add_argument("group", type=int)
    p.add_argument("mode")

    p = sub.add_parser("discover")
    p.add_argument("--max", type=int, default=DISCOVER_MAX)

    p = sub.add_parser("zones-init", help="Discover units and add any new ones to the config, keeping existing names")
    p.add_argument("--max", type=int, default=DISCOVER_MAX)

    p = sub.add_parser(
        "discover-features",
        help="Probe which modes/fan speeds each zone actually accepts and save them to the config",
    )
    p.add_argument("group", type=int, nargs="?", help="Limit to one group; omit to run for every configured zone")

    p = sub.add_parser("rename")
    p.add_argument("group", type=int)
    p.add_argument("name")

    p = sub.add_parser("set-host")
    p.add_argument("host")
    p.add_argument("port", type=int, nargs="?", default=80)

    p = sub.add_parser("set-icon-temp")
    p.add_argument("mode", choices=ICON_TEMP_MODES)

    args = parser.parse_args()
    config = load_config()

    try:
        if args.cmd == "status":
            groups = groups_from_config(config)
            units = controller(config).get_status(groups)
            by_group = {u.group: u for u in units}
            print(json.dumps([
                unit_to_dict(by_group[g], config["zones"][str(g)])
                for g in groups if g in by_group
            ]))
        elif args.cmd == "config":
            print(json.dumps(config))
        elif args.cmd == "all-on":
            ctrl = controller(config)
            print(json.dumps({g: ctrl.turn_on(g) for g in groups_from_config(config)}))
        elif args.cmd == "all-off":
            ctrl = controller(config)
            print(json.dumps({g: ctrl.turn_off(g) for g in groups_from_config(config)}))
        elif args.cmd == "on":
            print(json.dumps({"ok": controller(config).turn_on(args.group)}))
        elif args.cmd == "off":
            print(json.dumps({"ok": controller(config).turn_off(args.group)}))
        elif args.cmd == "set-temp":
            print(json.dumps({"ok": controller(config).set_temperature(args.group, args.temp)}))
        elif args.cmd == "set-fan":
            print(json.dumps({"ok": controller(config).set_fan_speed(args.group, FanSpeed(args.speed))}))
        elif args.cmd == "set-mode":
            print(json.dumps({"ok": controller(config).set_mode(args.group, Mode(args.mode))}))
        elif args.cmd == "discover":
            print(json.dumps({"found": discover(config, args.max)}))
        elif args.cmd == "zones-init":
            found = discover(config, args.max)
            for g in found:
                key = str(g)
                if key not in config["zones"]:
                    config["zones"][key] = normalize_zone("Zone {}".format(g), g)
            saved = save_config(config)
            print(json.dumps({"found": found, "zones": saved["zones"]}))
        elif args.cmd == "discover-features":
            groups = [args.group] if args.group else groups_from_config(config)
            results = discover_features(config, groups)
            print(json.dumps({str(g): r for g, r in results.items()}))
        elif args.cmd == "rename":
            key = str(args.group)
            zone_cfg = config["zones"].get(key) or normalize_zone("Zone {}".format(args.group), args.group)
            zone_cfg["name"] = args.name
            config["zones"][key] = zone_cfg
            saved = save_config(config)
            print(json.dumps(saved))
        elif args.cmd == "set-icon-temp":
            config["icon_temp"] = args.mode
            saved = save_config(config)
            print(json.dumps(saved))
        elif args.cmd == "set-host":
            config["host"] = args.host
            config["port"] = args.port
            saved = save_config(config)
            print(json.dumps(saved))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
