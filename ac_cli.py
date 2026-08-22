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
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mit import AirDirection, MNetController, MNetHTTPError, Mode, FanSpeed

DEFAULT_CONFIG_FILE = os.path.expanduser("~/.config/ac-plugin/config.json")
CONFIG_FILE = os.environ.get("AC_CONFIG_FILE", DEFAULT_CONFIG_FILE)

# Real, user-facing write targets. AUTOCOOL/AUTOHEAT are excluded on purpose:
# Mitsubishi integration documentation marks them "not applicable for setting" -
# read-only status the unit reports while running under AUTO, never
# something you write.
ALL_MODES = ["COOL", "HEAT", "DRY", "FAN", "AUTO"]
ALL_FAN_SPEEDS = ["AUTO", "LOW", "MID1", "MID2", "HIGH"]
ALL_AIR_DIRECTIONS = ["HORIZONTAL", "MID0", "MID1", "MID2", "VERTICAL", "SWING"]

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
    # What the bar icon shows next to the snowflake across live zone sensor
    # readings: "avg" (default), "min", "max", or "off" (icon only).
    "icon_temp": "avg",
    "zones": {},
}

ICON_TEMP_MODES = ("avg", "min", "max", "off")

# G-50-family gateways expose 50 possible group slots.
DISCOVER_MAX = 50

def discover_limit(value):
    limit = int(value)
    if not 1 <= limit <= DISCOVER_MAX:
        raise argparse.ArgumentTypeError("must be between 1 and {}".format(DISCOVER_MAX))
    return limit


def normalize_zone(entry, group):
    """Upgrade a zone config entry to the {name, modes, fan_speeds} shape.

    Older configs (and DEFAULT_CONFIG) store zones as a plain name string;
    this fills in the not-yet-discovered defaults for those.
    """
    if isinstance(entry, str):
        return {
            "name": entry, "modes": list(DEFAULT_MODES),
            "fan_speeds": list(DEFAULT_FAN_SPEEDS), "air_directions": [],
        }
    if not isinstance(entry, dict):
        raise ValueError("Zone {} must be a name or object".format(group))
    modes = entry["modes"] if "modes" in entry else list(DEFAULT_MODES)
    fan_speeds = entry["fan_speeds"] if "fan_speeds" in entry else list(DEFAULT_FAN_SPEEDS)
    air_directions = entry["air_directions"] if "air_directions" in entry else []
    if not isinstance(modes, list) or any(m not in ALL_MODES for m in modes):
        raise ValueError("Zone {} has invalid modes".format(group))
    if not isinstance(fan_speeds, list) or any(s not in ALL_FAN_SPEEDS for s in fan_speeds):
        raise ValueError("Zone {} has invalid fan speeds".format(group))
    if not isinstance(air_directions, list) or any(d not in ALL_AIR_DIRECTIONS for d in air_directions):
        raise ValueError("Zone {} has invalid air directions".format(group))
    name = entry.get("name", "Zone {}".format(group))
    if not isinstance(name, str):
        raise ValueError("Zone {} has an invalid name".format(group))
    return {
        "name": name or "Zone {}".format(group),
        "modes": modes,
        "fan_speeds": fan_speeds,
        "air_directions": air_directions,
    }


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError("Cannot load AC config {}: {}".format(CONFIG_FILE, e)) from e

    if not isinstance(data, dict):
        raise ValueError("AC config must contain a JSON object")

    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in data.items() if k != "zones"})
    # An explicit zone set is authoritative, including an empty one. If the
    # key is absent (old config or first run), commands report that explicit
    # `zones-init` setup is required; status remains offline and never writes.
    config["setup_required"] = "zones" not in data
    zones_raw = data.get("zones", {})
    if not isinstance(zones_raw, dict):
        raise ValueError("AC config zones must be a JSON object")
    zones = {}
    for raw_group, value in zones_raw.items():
        try:
            group = int(raw_group)
        except (TypeError, ValueError) as e:
            raise ValueError("Invalid zone group {!r}".format(raw_group)) from e
        if not 1 <= group <= DISCOVER_MAX or str(group) != str(raw_group):
            raise ValueError("Zone group must be an integer from 1 to {}".format(DISCOVER_MAX))
        zones[str(group)] = normalize_zone(value, group)
    config["zones"] = zones

    # Validate persisted host/port now, including commands that do not make a
    # request. Runtime environment overrides are applied only by controller(),
    # so an unrelated config save can never persist a one-off endpoint.
    MNetController(config["host"], config["port"])
    if config.get("icon_temp") not in ICON_TEMP_MODES:
        raise ValueError("icon_temp must be one of {}".format(", ".join(ICON_TEMP_MODES)))

    return config


def save_config(config):
    config_dir = os.path.dirname(os.path.abspath(CONFIG_FILE))
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    if os.path.abspath(CONFIG_FILE) == os.path.abspath(DEFAULT_CONFIG_FILE):
        # Tighten directories created by older versions without changing an
        # arbitrary parent directory selected through AC_CONFIG_FILE.
        os.chmod(config_dir, 0o700)

    # Keep one generation of history. Use a private same-directory temporary
    # file and atomic replacement so an attacker-controlled backup symlink is
    # never followed and an interrupted copy cannot leave a partial backup.
    # Best-effort: a backup failure must never block the actual save.
    if os.path.exists(CONFIG_FILE):
        backup_path = CONFIG_FILE + ".backup"
        backup_fd = None
        backup_temp = None
        try:
            backup_fd, backup_temp = tempfile.mkstemp(
                prefix=".config.backup.", suffix=".tmp", dir=config_dir
            )
            os.fchmod(backup_fd, 0o600)
            with open(CONFIG_FILE, "rb") as source, os.fdopen(backup_fd, "wb") as target:
                backup_fd = None
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(backup_temp, backup_path)
            backup_temp = None
        except OSError:
            pass
        finally:
            if backup_fd is not None:
                os.close(backup_fd)
            if backup_temp is not None:
                try:
                    os.unlink(backup_temp)
                except FileNotFoundError:
                    pass

    zones_out = {
        g: {
            "name": z["name"], "modes": z["modes"],
            "fan_speeds": z["fan_speeds"],
            "air_directions": z.get("air_directions", []),
        }
        for g, z in config["zones"].items()
    }
    to_write = {
        "host": config["host"],
        "port": config["port"],
        "icon_temp": config.get("icon_temp", "avg"),
    }
    if not config.get("setup_required", False):
        to_write["zones"] = dict(sorted(zones_out.items(), key=lambda kv: int(kv[0])))
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
    host = os.environ.get("AC_HOST") or config["host"]
    port = config["port"]
    if os.environ.get("AC_PORT"):
        try:
            port = int(os.environ["AC_PORT"])
        except ValueError as e:
            raise ValueError("AC_PORT must be an integer") from e
    return MNetController(host, port)


def groups_from_config(config):
    return sorted(int(g) for g in config["zones"].keys())


def bulk_power(ctrl, groups, turn_on):
    """Attempt every configured group and report failures without aborting."""
    operation = ctrl.turn_on if turn_on else ctrl.turn_off
    results = {}
    for group in groups:
        try:
            results[group] = operation(group)
        except Exception as e:
            results[group] = {"error": str(e)}
    return results


def require_confirmed(confirmed, action):
    if not confirmed:
        raise RuntimeError("M-NET gateway did not confirm {}".format(action))
    return {"ok": True}


def require_configured_group(config, group):
    if str(group) not in config["zones"]:
        raise ValueError("M-NET group {} is not configured".format(group))


def unit_to_dict(u, zone_cfg):
    return {
        "group": u.group,
        "name": zone_cfg["name"],
        "modes": zone_cfg["modes"],
        "fan_speeds": zone_cfg["fan_speeds"],
        "air_directions": zone_cfg.get("air_directions", []),
        "drive": u.drive.value if u.drive else None,
        "mode": u.mode.value if u.mode else None,
        "set_temp": u.set_temp,
        "fan_speed": u.fan_speed.value if u.fan_speed else None,
        "air_direction": u.air_direction.value if u.air_direction else None,
        "inlet_temp": u.inlet_temp,
        "filter_sign": u.filter_sign,
        "error_sign": u.error_sign,
    }


def is_unused_group_http_error(error):
    return isinstance(error, MNetHTTPError) and error.status_code == 500


def get_status_resilient(ctrl, groups):
    """Use an efficient batch, falling back per group on gateway HTTP 500."""
    if not groups:
        return []
    requested = set(groups)
    try:
        batch_units = ctrl.get_status(groups)
    except Exception as batch_error:
        if not is_unused_group_http_error(batch_error):
            raise
        batch_units = []

    batch_groups = [unit.group for unit in batch_units]
    if len(batch_groups) != len(set(batch_groups)):
        raise RuntimeError("M-NET gateway returned duplicate groups in status response")
    unexpected = set(batch_groups) - requested
    if unexpected:
        raise RuntimeError("M-NET gateway returned unrequested group(s): {}".format(
            ", ".join(map(str, sorted(unexpected)))
        ))
    invalid = [unit.group for unit in batch_units if unit.drive is None]
    if invalid:
        raise RuntimeError("M-NET gateway returned incomplete status for configured group(s): {}".format(
            ", ".join(map(str, sorted(invalid)))
        ))

    by_group = {unit.group: unit for unit in batch_units}
    for group in groups:
        if group in by_group:
            continue
        try:
            individual = ctrl.get_status([group])
        except Exception as group_error:
            raise RuntimeError("No status available for configured group {}".format(group)) from group_error
        if (len(individual) != 1 or individual[0].group != group
                or individual[0].drive is None):
            raise RuntimeError("M-NET gateway returned invalid status for configured group {}".format(group))
        by_group[group] = individual[0]

    if not by_group:
        raise RuntimeError("M-NET gateway returned no status for configured groups")
    return [by_group[group] for group in groups]


def discover_zone_info(config, max_group):
    """Return group metadata, using the inventory API with probe fallback."""
    ctrl = controller(config)
    try:
        inventory = ctrl.get_group_inventory()
        missing_models = [g for g, info in inventory.items() if info.get("model") is None]
        models = ctrl.get_group_models(sorted(missing_models)) if missing_models else {}
        return {
            g: info for g, info in inventory.items()
            if g <= max_group and (info.get("model") or models.get(g)) == "IC"
        }
    except MNetHTTPError as e:
        if e.status_code != 500:
            raise
    except RuntimeError:
        pass

    # Some older controllers expose only the name list, which is still a
    # complete configured-group inventory for our purposes.
    try:
        names = ctrl.get_group_names()
        models = ctrl.get_group_models(sorted(names))
        return {
            g: info for g, info in names.items()
            if g <= max_group and models.get(g) == "IC"
        }
    except MNetHTTPError as e:
        if e.status_code != 500:
            raise
    except RuntimeError:
        pass

    found = {}
    for g in range(1, max_group + 1):
        try:
            models = ctrl.get_group_models([g])
            if models.get(g) == "IC":
                found[g] = {"name": None, "model": None, "submodel": None, "address": None}
        except Exception as e:
            # This gateway returns HTTP 500 for an unused group. Continue for
            # that expected probe rejection, but fail fast on DNS, connection,
            # timeout, redirect, authentication, or malformed-response errors.
            if is_unused_group_http_error(e):
                continue
            raise
    return found


def discover(config, max_group):
    return sorted(discover_zone_info(config, max_group))


def require_setup_complete(config):
    if config.get("setup_required", False):
        raise RuntimeError("AC setup is required; run `ac zones-init` first")


def discover_features(config, groups):
    """Read mode and fan capabilities without changing physical HVAC state."""
    ctrl = controller(config)
    capabilities = ctrl.get_capabilities(groups)
    results = {}
    for g in groups:
        attrs = capabilities.get(g)
        if attrs is None:
            raise RuntimeError("M-NET gateway returned no capabilities for group {}".format(g))

        if attrs.get("Model") != "IC" or attrs.get("IcKind") not in ("COOL", "NORMAL"):
            raise RuntimeError(
                "Unsupported M-NET unit type for group {}: model={!r}, kind={!r}".format(
                    g, attrs.get("Model"), attrs.get("IcKind")
                )
            )
        supported_modes = ["COOL"]
        if attrs["IcKind"] == "NORMAL":
            supported_modes.append("HEAT")
        if attrs.get("DryModeSW") == "ENABLE":
            supported_modes.append("DRY")
        if attrs.get("FanModeSW") == "ENABLE":
            supported_modes.append("FAN")
        if attrs.get("AutoModeSW") == "ENABLE":
            supported_modes.append("AUTO")

        stages = attrs.get("FanSpeedSW")
        if stages == "4STAGES":
            supported_fans = ["LOW", "MID1", "MID2", "HIGH"]
        elif stages == "3STAGES":
            supported_fans = ["MID1", "MID2", "HIGH"]
        elif stages == "2STAGES":
            supported_fans = ["LOW", "HIGH"]
        else:
            supported_fans = []
        if attrs.get("FanAutoSW") == "ENABLE":
            supported_fans.insert(0, "AUTO")

        air_directions = []
        if attrs.get("AirDirectionSW") == "ENABLE":
            air_directions = ["HORIZONTAL"]
            if attrs.get("AirStageSW") == "5STAGES":
                air_directions.append("MID0")
            air_directions.extend(["MID1", "MID2", "VERTICAL"])
            if attrs.get("SwingSW") == "ENABLE":
                air_directions.append("SWING")

        key = str(g)
        zone_cfg = config["zones"].get(key) or normalize_zone("Zone {}".format(g), g)
        zone_cfg["modes"] = supported_modes
        zone_cfg["fan_speeds"] = supported_fans
        zone_cfg["air_directions"] = air_directions
        config["zones"][key] = zone_cfg
        results[g] = {
            "modes": supported_modes,
            "fan_speeds": supported_fans,
            "air_directions": air_directions,
            "swing": attrs.get("SwingSW") == "ENABLE",
            "filter_sign": attrs.get("FilterSign"),
            "error_sign": attrs.get("ErrorSign"),
        }

    save_config(config)
    return results


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("all-on")
    sub.add_parser("all-off")
    sub.add_parser("config")
    sub.add_parser("alarms")

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

    p = sub.add_parser("set-air")
    p.add_argument("group", type=int)
    p.add_argument("direction")

    p = sub.add_parser("discover")
    p.add_argument("--max", type=discover_limit, default=DISCOVER_MAX)

    p = sub.add_parser("zones-init", help="Discover units and add any new ones to the config, keeping existing names")
    p.add_argument("--max", type=discover_limit, default=DISCOVER_MAX)

    p = sub.add_parser(
        "discover-features",
        help="Read mode/fan/air capability flags and save them without changing HVAC state",
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

    try:
        args = parser.parse_args()
        config = load_config()
        if args.cmd in ("all-on", "all-off", "on", "off", "set-temp",
                        "set-fan", "set-mode", "set-air", "discover-features"):
            require_setup_complete(config)
        if args.cmd in ("on", "off", "set-temp", "set-fan", "set-mode", "set-air"):
            require_configured_group(config, args.group)
        if args.cmd == "discover-features" and args.group is not None:
            require_configured_group(config, args.group)
        if args.cmd == "status":
            if config.get("setup_required", False):
                print(json.dumps([]))
                return
            groups = groups_from_config(config)
            units = get_status_resilient(controller(config), groups)
            by_group = {u.group: u for u in units}
            print(json.dumps([
                unit_to_dict(by_group[g], config["zones"][str(g)])
                for g in groups if g in by_group
            ]))
        elif args.cmd == "config":
            print(json.dumps(config))
        elif args.cmd == "alarms":
            print(json.dumps(controller(config).get_alarm_status()))
        elif args.cmd == "all-on":
            ctrl = controller(config)
            results = bulk_power(ctrl, groups_from_config(config), True)
            failures = [g for g, result in results.items() if result is not True]
            if failures:
                raise RuntimeError("Failed to turn on group(s): {}".format(", ".join(map(str, failures))))
            print(json.dumps(results))
        elif args.cmd == "all-off":
            ctrl = controller(config)
            results = bulk_power(ctrl, groups_from_config(config), False)
            failures = [g for g, result in results.items() if result is not True]
            if failures:
                raise RuntimeError("Failed to turn off group(s): {}".format(", ".join(map(str, failures))))
            print(json.dumps(results))
        elif args.cmd == "on":
            print(json.dumps(require_confirmed(controller(config).turn_on(args.group), "power on")))
        elif args.cmd == "off":
            print(json.dumps(require_confirmed(controller(config).turn_off(args.group), "power off")))
        elif args.cmd == "set-temp":
            print(json.dumps(require_confirmed(
                controller(config).set_temperature(args.group, args.temp), "temperature"
            )))
        elif args.cmd == "set-fan":
            if args.speed not in config["zones"][str(args.group)]["fan_speeds"]:
                raise ValueError("Fan speed is not supported for this group")
            print(json.dumps(require_confirmed(
                controller(config).set_fan_speed(args.group, FanSpeed(args.speed)), "fan speed"
            )))
        elif args.cmd == "set-mode":
            if args.mode not in config["zones"][str(args.group)]["modes"]:
                raise ValueError("Mode is not supported for this group")
            print(json.dumps(require_confirmed(
                controller(config).set_mode(args.group, Mode(args.mode)), "mode"
            )))
        elif args.cmd == "set-air":
            allowed = config["zones"][str(args.group)].get("air_directions", [])
            if args.direction not in allowed:
                raise ValueError("Air direction is not supported for this group")
            print(json.dumps(require_confirmed(
                controller(config).set_air_direction(
                    args.group, AirDirection(args.direction)
                ),
                "air direction",
            )))
        elif args.cmd == "discover":
            print(json.dumps({"found": discover(config, args.max)}))
        elif args.cmd == "zones-init":
            zone_info = discover_zone_info(config, args.max)
            found = sorted(zone_info)
            if not found:
                raise RuntimeError("No configured indoor HVAC groups were discovered")
            for g in found:
                key = str(g)
                if key not in config["zones"]:
                    name = zone_info[g].get("name") or "Zone {}".format(g)
                    config["zones"][key] = normalize_zone(name, g)
            config["setup_required"] = False
            saved = save_config(config)
            print(json.dumps({"found": found, "zones": saved["zones"]}))
        elif args.cmd == "discover-features":
            groups = [args.group] if args.group else groups_from_config(config)
            results = discover_features(config, groups)
            print(json.dumps({str(g): r for g, r in results.items()}))
        elif args.cmd == "rename":
            require_configured_group(config, args.group)
            key = str(args.group)
            zone_cfg = config["zones"][key]
            zone_cfg["name"] = args.name
            config["zones"][key] = zone_cfg
            config["setup_required"] = False
            saved = save_config(config)
            print(json.dumps(saved))
        elif args.cmd == "set-icon-temp":
            config["icon_temp"] = args.mode
            saved = save_config(config)
            print(json.dumps(saved))
        elif args.cmd == "set-host":
            MNetController(args.host, args.port)
            config["host"] = args.host
            config["port"] = args.port
            saved = save_config(config)
            print(json.dumps(saved))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
