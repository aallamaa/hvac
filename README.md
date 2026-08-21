# mvac.ac — Omarchy AC control plugin

Bar widget for the Omarchy shell (Quickshell) that controls a Mitsubishi
M-NET HVAC system: per-zone status, on/off, mode, temperature, and fan
speed, from a popup anchored to a bar icon.

If the gateway can't be reached (or the status call fails for any reason),
the bar icon switches to the theme's urgent/red color and drops the
temperature reading rather than showing a stale value.

## Files

- `mit.py` — M-NET client (XML over HTTP to the gateway). Not modified by
  this plugin; used as a library.
- `ac_cli.py` — CLI wrapper around `mit.py`. The QML shells out to this for
  every read and write; it's also usable standalone from a terminal.
- `Panel.qml` — the bar icon + popup (Quickshell/QML).
- `manifest.json` — Omarchy plugin manifest (`id: mvac.ac`).

## Deployment

Omarchy's plugin loader reads from `~/.config/omarchy/plugins/mvac.ac/`,
and its validator (`omarchy plugin validate`) rejects symlinks inside a
plugin folder — so that directory holds real copies, not a symlink back
here. After editing files in this repo, redeploy with:

```bash
cp mit.py ac_cli.py Panel.qml manifest.json ~/.config/omarchy/plugins/mvac.ac/
```

QML edits hot-reload automatically once copied; if changes don't seem to
apply, `omarchy restart shell` forces a clean reload.

## Configuration

Gateway host/port, zone names, and per-zone capabilities live in
`~/.config/ac-plugin/config.json`, not in the code:

```json
{
  "host": "192.168.1.2",
  "port": 80,
  "icon_temp": "avg",
  "zones": {
    "1": { "name": "Living room", "modes": ["COOL", "DRY", "FAN"], "fan_speeds": ["AUTO", "MID1", "MID2", "HIGH"] },
    "6": { "name": "Office", "modes": ["COOL", "HEAT", "DRY", "FAN"], "fan_speeds": ["LOW", "MID1", "MID2", "HIGH"] }
  }
}
```

`icon_temp` controls what number the bar icon shows next to the snowflake
for the zones that are currently on (icon-only when everything's off):
`avg` (default), `min`, `max`, or `off`. It's also editable straight from
the popup — the "Icon: AVG" button in the header cycles through the four
values.

`modes`/`fan_speeds` are what the popup's mode/fan cycle buttons step
through for that zone — see "Per-zone capability discovery" below for
where they come from. A zone can also be given as a plain name string
(`"1": "Living room"`); it's upgraded on load to the full shape above,
filled in with the not-yet-discovered defaults.

If this file is missing, `ac_cli.py` falls back to built-in defaults
(`192.168.1.2`, zones 1–6 named "Zone N") so the plugin still works out of
the box. `AC_CONFIG_FILE` overrides the path; `AC_HOST` / `AC_PORT` env
vars override the host/port from the file for one-off use.

### Per-zone capability discovery

Different indoor unit models on the same M-NET system can support
different modes and fan speeds — on this installation, one zone (a
heat-pump unit) supports `HEAT` and `LOW` fan speed while the rest
(cooling-only units) reject both. Rather than guess, `discover-features`
finds out empirically per zone: for each candidate mode/fan speed, it
writes the value and reads status back to see whether it actually took —
one that's silently rejected reads back unchanged.

```bash
ac discover-features        # every configured zone
ac discover-features 6      # just zone/group 6
```

For each zone this temporarily turns it off (a setting is easier to tell
apart from "didn't take" when the compressor isn't also reacting), steps
through `COOL`/`HEAT`/`DRY`/`FAN`/`AUTO` and `AUTO`/`LOW`/`MID1`/`MID2`/`HIGH`,
then restores its original mode, fan speed, set temperature, and on/off
state before moving to the next zone. `AUTOCOOL`/`AUTOHEAT` aren't tested —
per Mitsubishi's own M-NET gateway spec they're read-only status values a
unit reports while running under `AUTO`, never something you write. Each
zone's results are saved to the config as soon as that zone finishes, so
an interruption doesn't lose earlier zones' results. It takes a few
seconds per zone.

## Running commands directly

There's no `omarchy plugin <id> <command>` passthrough — `omarchy plugin`
only manages plugin lifecycle (list/enable/disable/validate). To run
`ac_cli.py` commands yourself, either call it directly:

```bash
python3 ~/.config/omarchy/plugins/mvac.ac/ac_cli.py status
```

or use the `ac` shortcut installed at `~/.local/bin/ac` (on `PATH`), which
always calls the deployed copy:

```bash
ac status
```

Every subcommand prints one JSON value to stdout and exits 0 on success;
on failure it prints `{"error": "..."}` to stderr and exits 1.

### Commands

| Command | Args | Description |
|---|---|---|
| `status` | | Status of every configured zone (drive, mode, set temp, fan speed, inlet temp). |
| `config` | | Print the resolved config (host, port, zones). |
| `on` | `<group>` | Turn a zone on. |
| `off` | `<group>` | Turn a zone off. |
| `set-temp` | `<group> <temp>` | Set target temperature (16–30). |
| `set-fan` | `<group> <speed>` | Set fan speed: `AUTO`, `LOW`, `MID1`, `MID2`, or `HIGH`. Not every zone accepts every value — see below. |
| `set-mode` | `<group> <mode>` | Set mode: `COOL`, `HEAT`, `DRY`, `FAN`, or `AUTO`. Not every zone accepts every value — see below. |
| `all-on` | | Turn on every configured zone. |
| `all-off` | | Turn off every configured zone. |
| `discover` | `[--max N]` | Probe groups `1..N` (default 16) one at a time and report which respond. Doesn't touch the config. |
| `zones-init` | `[--max N]` | Run `discover`, then add any newly-found groups to the config as "Zone N" — existing names are kept. |
| `discover-features` | `[group]` | Probe which modes/fan speeds a zone (or every zone, if omitted) actually accepts and save them to the config. |
| `rename` | `<group> <name>` | Set a zone's display name in the config. |
| `set-host` | `<host> [port]` | Set the gateway host/port in the config (default port 80). |
| `set-icon-temp` | `<mode>` | Set the bar icon's temperature display: `avg`, `min`, `max`, or `off`. |

### Examples

```bash
ac status
ac on 3
ac set-temp 3 22
ac set-fan 3 HIGH
ac set-mode 3 COOL
ac rename 3 "Kids room"
ac discover --max 10
ac zones-init
ac discover-features
ac discover-features 6
ac set-host 192.168.1.2
```
