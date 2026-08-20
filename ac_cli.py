#!/usr/bin/env python3
"""CLI wrapper around mit.py's MNetController, for the AC waybar/omarchy plugin.

Every subcommand prints one JSON value to stdout and exits 0 on success.
On failure it prints {"error": "..."} to stderr and exits 1.

Gateway host/port and zone (group -> name) config live in a single JSON
file instead of being hardcoded, so the hardware setup can change without
touching this script or the QML. See CONFIG_FILE below.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mit import MNetController, Mode, FanSpeed

CONFIG_FILE = os.environ.get("AC_CONFIG_FILE", os.path.expanduser("~/.config/ac-plugin/config.json"))

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


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in data.items() if k != "zones"})
    zones = dict(DEFAULT_CONFIG["zones"])
    zones.update(data.get("zones") or {})
    config["zones"] = zones

    # Env vars win over the file, for one-off overrides without editing it.
    if os.environ.get("AC_HOST"):
        config["host"] = os.environ["AC_HOST"]
    if os.environ.get("AC_PORT"):
        config["port"] = int(os.environ["AC_PORT"])

    return config


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    to_write = {
        "host": config["host"],
        "port": config["port"],
        "icon_temp": config.get("icon_temp", "avg"),
        "zones": dict(sorted(config["zones"].items(), key=lambda kv: int(kv[0]))),
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(to_write, f, indent=2)
        f.write("\n")
    return to_write


def controller(config):
    return MNetController(config["host"], config["port"])


def groups_from_config(config):
    return sorted(int(g) for g in config["zones"].keys())


def unit_to_dict(u, name):
    return {
        "group": u.group,
        "name": name,
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
                config["zones"].setdefault(str(g), "Zone {}".format(g))
            saved = save_config(config)
            print(json.dumps({"found": found, "zones": saved["zones"]}))
        elif args.cmd == "rename":
            config["zones"][str(args.group)] = args.name
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
