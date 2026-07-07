# velop-mx5300-cli

`velopctl` is a single-file Python CLI for administering a Linksys Velop
mesh router (tested against an MX5300 on firmware 1.1.12.210066) directly
over its local JNAP HTTP API. It bypasses the Linksys app and web GUI
entirely, which is useful when both are broken (e.g. "Error 2123 /
ErrorDeviceDBFailure" caused by a corrupted device database).

It talks to `POST http://<host>/JNAP/` with the standard JNAP headers
(`X-JNAP-Action`, `X-JNAP-Authorization: Basic ...`) and a JSON body,
the same protocol the router's own app/GUI use.

## Requirements

- Python 3.8+ (uses only the standard library: `urllib`, `json`, `base64`,
  `argparse`, `getpass`). On Python 3.11+ it uses `tomllib` for the config
  file; on older versions it falls back to a tiny built-in parser for
  simple `key = "value"` TOML files, so no extra dependency is required
  either way.
- Works on macOS's system Python out of the box.

## Install

Just copy the script somewhere on your `PATH`:

```sh
chmod +x velopctl.py
cp velopctl.py /usr/local/bin/velopctl
```

Or run it in place with `python3 velopctl.py ...`.

## Configuration

`velopctl` needs the router's admin password. It never hardcodes it, and
reads it from (in order of precedence):

1. `VELOP_PASSWORD` environment variable
2. `~/.velopctl.toml` (or a path given with `--config`)

Run the interactive setup once:

```sh
$ velopctl login
Router host [myrouter.local]: 192.168.1.1
Username [admin]: admin
Password:
Saved config to /Users/you/.velopctl.toml
```

This writes `~/.velopctl.toml` (mode `0600`):

```toml
host = "192.168.1.1"
username = "admin"
password = "your-admin-password"
```

Or set it ad hoc for one-off commands / CI:

```sh
export VELOP_PASSWORD='your-admin-password'
velopctl --host 192.168.1.1 info
```

`--host` / `--username` on the command line always override the config
file, which in turn is overridden by `VELOP_HOST` / `VELOP_PASSWORD` env
vars only for the host/password respectively.

## Global flags

```
--host HOST         router hostname/IP (default: myrouter.local, or config file)
--username USER     JNAP username (default: admin, or config file)
--config PATH       path to config file (default: ~/.velopctl.toml)
--json              print raw JSON responses instead of human-readable output
--dry-run           print intended JNAP calls for destructive ops instead of sending them
--yes, -y           skip interactive confirmation prompts
--timeout SECONDS   HTTP timeout (default: 15)
```

Global flags go **before** the subcommand, e.g. `velopctl --json info`,
not `velopctl info --json`.

## Commands

### `info`
Show model, firmware, serial, and the full supported-services list
(`core/GetDeviceInfo`).

```sh
velopctl info
velopctl --json info
```

### `wan`
Show WAN type/configuration (`router/GetWANSettings`).

```sh
velopctl wan
```

### `lan get` / `lan set`
Show or change LAN settings (`router/GetLANSettings` /
`router/SetLANSettings`). `lan set` fetches the current settings, merges
in your overrides, prints what it's about to send, and requires typing
`CONFIRM` (or `--yes`) since it can drop connectivity to the whole LAN:

```sh
velopctl lan get
velopctl lan set --set ipAddress=192.168.2.1 --set dhcpSettings.dhcpEnabled=false
velopctl --dry-run lan set --set ipAddress=192.168.2.1   # preview only, no request sent
velopctl lan set --json-body '{"ipAddress":"192.168.2.1", ...}'  # replace the whole body
```

`--set KEY=VALUE` supports dotted paths for nested fields and is
repeatable; values are parsed as JSON when possible (so `true`, `123`,
`"a string"` all work), otherwise treated as a literal string.

### `wifi`
Show wireless settings. Different firmware builds expose this under
different JNAP actions, so `velopctl` probes `wirelessap/GetWirelessSettings`
first and falls back to `wirelessap/GetRadioSettings`, using whichever
returns `OK`:

```sh
velopctl wifi
```

### `devices` / `devices clear`
List devices from the device DB, and optionally drain it.

**The problem this solves:** a plain `{"sinceRevision": 0}` call to
`devicelist/GetDevices` throws `ErrorDeviceDBFailure` ("Payload exceeded
maximum size") when the DB is bloated/corrupted — which is exactly the
state that also breaks the app and web GUI.

**How `devices` works around it:** it discovers the router's current DB
revision (a cheap call with a far-future `sinceRevision`, which returns
few or no devices), then walks *backward* from that revision in
configurable windows (default 500), reading only the devices whose
`lastChangeRevision` falls in each window and de-duping by `deviceID`. If
a window itself is too large to read, it's recursively bisected before
being given up on (reported as an "unreadable range" rather than
crashing the whole scan):

```sh
velopctl devices                  # human-readable list + revision + any unreadable ranges
velopctl devices --window 200     # smaller windows if the DB is especially bloated
velopctl devices --verbose        # print full JSON per device
velopctl --json devices           # raw JSON: {revision, devices, unreadableRanges}
```

`devices clear` reuses the same paged reader, then deletes every
`deviceID` it found one at a time via `devicelist/DeleteDevice`, showing
a running progress count, and finishes by re-trying
`{"sinceRevision": 0}` to verify the DB is actually healthy again:

```sh
velopctl devices clear              # asks for confirmation, then drains + verifies
velopctl --yes devices clear        # no prompt (e.g. scripted)
velopctl --dry-run devices clear    # print the deletes that would happen, don't send them
```

Any `ErrorDeviceDBFailure` encountered while reading devices prints a
hint to run `devices clear`.

### `pc get` / `pc disable` / `pc rm`
Parental control (`parentalcontrol/GetParentalControlSettings` /
`SetParentalControlSettings`):

```sh
velopctl pc get                     # rules, blocked URLs, and human-readable schedules
velopctl pc disable                 # turn off parental control and clear all rules
velopctl pc rm AA:BB:CC:DD:EE:FF    # remove just the rule containing this MAC
```

`pc get` renders each rule's 48-slot-per-day `wanSchedule` half-hour
bitmask (`1` = blocked) as human-readable time ranges, e.g. `Mon:
20:00-24:00` instead of a raw bitstring.

### `reboot`
Reboot the router (`core/Reboot`), with a confirmation prompt:

```sh
velopctl reboot
velopctl --yes reboot
```

### `raw` — escape hatch
Send any JNAP action with an arbitrary JSON body, for experimentation.
The `http://linksys.com/jnap/` prefix is added automatically unless the
action already looks like a full URL:

```sh
velopctl raw core/GetDeviceInfo
velopctl raw devicelist/GetDevices '{"sinceRevision": 2900}'
velopctl --dry-run raw router/SetLANSettings '{"ipAddress":"192.168.2.1"}'
```

## Error handling

Every response is checked for `"result" != "OK"`. On failure, `velopctl`
prints the error string and `output.ErrorInfo` (if present) and exits
non-zero. Device-DB reads specifically catch `ErrorDeviceDBFailure` and
suggest running `devices clear`.

## Optional: pyvelop backend

If [`pyvelop`](https://pypi.org/project/pyvelop/) happens to be installed,
it's *not* required and `velopctl` does not depend on it — all commands,
including parental controls, LAN settings, and the device-delete loop,
work purely via direct JNAP calls regardless of whether `pyvelop` is
present.
