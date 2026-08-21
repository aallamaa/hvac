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

Gateway host/port and zone (group number → name) live in
`~/.config/ac-plugin/config.json`, not in the code:

```json
{
  "host": "192.168.1.2",
  "port": 80,
  "icon_temp": "avg",
  "zones": {
    "1": "Living room",
    "2": "Kitchen"
  }
}
```

`icon_temp` controls what number the bar icon shows next to the snowflake
for the zones that are currently on (icon-only when everything's off):
`avg` (default), `min`, `max`, or `off`. It's also editable straight from
the popup — the "Icon: AVG" button in the header cycles through the four
values.

If this file is missing, `ac_cli.py` falls back to built-in defaults
(`192.168.1.2`, zones 1–6 named "Zone N") so the plugin still works out of
the box. `AC_CONFIG_FILE` overrides the path; `AC_HOST` / `AC_PORT` env
vars override the host/port from the file for one-off use.

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
| `set-fan` | `<group> <speed>` | Set fan speed: `AUTO`, `MID1`, `MID2`, `HIGH`. |
| `set-mode` | `<group> <mode>` | Set mode: `COOL`, `DRY`, `FAN`. |
| `all-on` | | Turn on every configured zone. |
| `all-off` | | Turn off every configured zone. |
| `discover` | `[--max N]` | Probe groups `1..N` (default 16) one at a time and report which respond. Doesn't touch the config. |
| `zones-init` | `[--max N]` | Run `discover`, then add any newly-found groups to the config as "Zone N" — existing names are kept. |
| `rename` | `<group> <name>` | Set a zone's display name in the config. |
| `set-host` | `<host> [port]` | Set the gateway host/port in the config (default port 80). |
| `set-icon-temp` | `<mode>` | Set the bar icon's temperature display: `avg`, `min`, `max`, or `off`. |

`mit.py`'s `Mode`/`FanSpeed` enums also define `HEAT`, `AUTO` (mode), and
`LOW` (fan speed), but this hardware silently rejects all three — the
gateway acks the write and the setting never actually changes, confirmed
both with the zone on and off. This is a cooling-only unit with no heat
pump; the plugin's mode/fan cycle buttons only cycle through the values
that actually stick.

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
ac set-host 192.168.1.2
```
