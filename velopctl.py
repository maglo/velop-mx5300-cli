#!/usr/bin/env python3
"""velopctl - administer a Linksys Velop mesh router over its local JNAP HTTP API.

Talks directly to http://<host>/JNAP/ so it works even when the Linksys app
and web GUI are broken (e.g. "Error 2123 / ErrorDeviceDBFailure").
"""

import argparse
import base64
import getpass
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

JNAP_PREFIX = "http://linksys.com/jnap/"
DEFAULT_HOST = "myrouter.local"
DEFAULT_USERNAME = "admin"
CONFIG_PATH = Path.home() / ".velopctl.toml"
DEFAULT_WINDOW = 500
MIN_WINDOW = 10


class JNAPError(Exception):
    def __init__(self, result, error_info=None):
        self.result = result
        self.error_info = error_info
        super().__init__(str(result))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _parse_simple_toml(text):
    """Fallback parser for flat key = "value" TOML files, used when tomllib
    is unavailable (Python < 3.11 and no tomli installed)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.lower() in ("true", "false"):
            value = value.lower() == "true"
        else:
            try:
                value = int(value)
            except ValueError:
                pass
        out[key] = value
    return out


def load_config(config_path=None):
    path = Path(config_path) if config_path else CONFIG_PATH
    data = {}
    if path.exists():
        if tomllib is not None:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        else:
            data = _parse_simple_toml(path.read_text())
    if os.environ.get("VELOP_PASSWORD"):
        data["password"] = os.environ["VELOP_PASSWORD"]
    if os.environ.get("VELOP_HOST"):
        data["host"] = os.environ["VELOP_HOST"]
    return data


def write_config(host, username, password, config_path=None):
    path = Path(config_path) if config_path else CONFIG_PATH
    content = (
        f'host = "{host}"\n'
        f'username = "{username}"\n'
        f'password = "{password}"\n'
    )
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------------
# JNAP transport
# --------------------------------------------------------------------------

class JNAPClient:
    def __init__(self, host, username, password, dry_run=False, timeout=15.0):
        self.host = host
        self.username = username
        self.password = password
        self.dry_run = dry_run
        self.timeout = timeout
        self.url = f"http://{host}/JNAP/"

    def _auth_header(self):
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"

    def call(self, action, body=None, check=True, destructive=False):
        body = body or {}
        full_action = action if action.startswith("http://") else JNAP_PREFIX + action

        if destructive and self.dry_run:
            print(f"[dry-run] POST {self.url}")
            print(f"[dry-run]   X-JNAP-Action: {full_action}")
            print(f"[dry-run]   body: {json.dumps(body)}")
            return {"result": "OK", "output": {}}

        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "X-JNAP-Action": full_action,
                "Content-Type": "application/json",
                "X-JNAP-Authorization": self._auth_header(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        except urllib.error.URLError as e:
            print(f"error: could not reach {self.url}: {e.reason}", file=sys.stderr)
            sys.exit(1)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(f"error: non-JSON response from router: {raw[:200]!r}", file=sys.stderr)
            sys.exit(1)

        if check and data.get("result") != "OK":
            raise JNAPError(data.get("result"), data.get("output", {}).get("ErrorInfo"))
        return data


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def dump_json(resp):
    print(json.dumps(resp, indent=2))


def pretty_print(obj, indent=0):
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            print(f"{pad}(empty)")
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                pretty_print(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                print(f"{pad}-")
                pretty_print(item, indent + 1)
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{obj}")


def handle_jnap_error(e):
    print(f"error: JNAP call failed: {e.result}", file=sys.stderr)
    if e.error_info:
        print(f"  ErrorInfo: {e.error_info}", file=sys.stderr)
    sys.exit(1)


def handle_device_db_error(e):
    print(f"error: JNAP call failed: {e.result}", file=sys.stderr)
    if e.error_info:
        print(f"  ErrorInfo: {e.error_info}", file=sys.stderr)
    if e.result == "ErrorDeviceDBFailure":
        print(
            "hint: the device database looks corrupted. Try 'velopctl devices clear' "
            "to drain it (optionally with a smaller --window).",
            file=sys.stderr,
        )
    sys.exit(1)


def confirm_or_abort(prompt, assume_yes):
    if assume_yes:
        return
    answer = input(f'{prompt} Type "CONFIRM" to proceed: ')
    if answer != "CONFIRM":
        print("Aborted.")
        sys.exit(1)


def set_dotted(d, path, value):
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


# --------------------------------------------------------------------------
# devices: sliding-window reader for the (possibly corrupted) device DB
# --------------------------------------------------------------------------

def get_current_revision(client):
    resp = client.call("devicelist/GetDevices", {"sinceRevision": 2**31 - 1}, check=False)
    if resp.get("result") != "OK":
        raise JNAPError(resp.get("result"), resp.get("output", {}).get("ErrorInfo"))
    return resp.get("output", {}).get("revision", 0)


def read_window(client, start, end, min_window=MIN_WINDOW):
    """Fetch devices changed in the revision range (start, end].

    devicelist/GetDevices with sinceRevision=N returns every device changed
    since N, so the response grows as N shrinks -- that's why sinceRevision=0
    can blow past the payload limit on a bloated DB. If a window still fails,
    it's recursively bisected until it succeeds or hits min_window, in which
    case that slice is reported back as an unreadable gap instead of raised.
    """
    resp = client.call("devicelist/GetDevices", {"sinceRevision": start}, check=False)
    if resp.get("result") == "OK":
        devices = resp.get("output", {}).get("devices", [])
        in_range = [d for d in devices if start < d.get("lastChangeRevision", 0) <= end]
        return in_range, []
    if resp.get("result") == "ErrorDeviceDBFailure" and (end - start) > min_window:
        mid = start + (end - start) // 2
        left_devices, left_gaps = read_window(client, start, mid, min_window)
        right_devices, right_gaps = read_window(client, mid, end, min_window)
        return left_devices + right_devices, left_gaps + right_gaps
    return [], [(start, end)]


def read_all_devices(client, window=DEFAULT_WINDOW, progress=None):
    revision = get_current_revision(client)
    devices = {}
    gaps = []
    end = revision
    while end > 0:
        start = max(0, end - window)
        found, window_gaps = read_window(client, start, end)
        for d in found:
            devices[d.get("deviceID")] = d
        gaps.extend(window_gaps)
        if progress:
            progress(start, end, len(devices))
        end = start
    return revision, devices, gaps


# --------------------------------------------------------------------------
# parental control: wanSchedule bitmask -> human-readable ranges
# --------------------------------------------------------------------------

def slot_to_time(slot):
    minutes = slot * 30
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def compress_ranges(bitstring):
    ranges = []
    start = None
    for i, ch in enumerate(bitstring + "0"):
        blocked = ch == "1"
        if blocked and start is None:
            start = i
        elif not blocked and start is not None:
            ranges.append((start, i))
            start = None
    return ranges


def format_wan_schedule(schedule, indent="      "):
    lines = []
    for day, bits in schedule.items():
        if isinstance(bits, list):
            bits = "".join("1" if b else "0" for b in bits)
        ranges = compress_ranges(bits)
        if not ranges:
            lines.append(f"{indent}{day}: (not blocked)")
            continue
        parts = [f"{slot_to_time(s)}-{slot_to_time(e)}" for s, e in ranges]
        lines.append(f"{indent}{day}: {', '.join(parts)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_login(args):
    host = input(f"Router host [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    username = input(f"Username [{DEFAULT_USERNAME}]: ").strip() or DEFAULT_USERNAME
    password = getpass.getpass("Password: ")
    if not password:
        print("error: password cannot be empty", file=sys.stderr)
        sys.exit(1)
    write_config(host, username, password, args.config)
    print(f"Saved config to {args.config or CONFIG_PATH}")


def cmd_info(client, args):
    resp = client.call("core/GetDeviceInfo")
    if args.json:
        dump_json(resp)
        return
    pretty_print(resp.get("output", {}))


def cmd_wan(client, args):
    resp = client.call("router/GetWANSettings")
    if args.json:
        dump_json(resp)
        return
    pretty_print(resp.get("output", {}))


def cmd_lan_get(client, args):
    resp = client.call("router/GetLANSettings")
    if args.json:
        dump_json(resp)
        return
    pretty_print(resp.get("output", {}))


def cmd_lan_set(client, args):
    current = client.call("router/GetLANSettings").get("output", {})
    merged = json.loads(json.dumps(current))  # deep copy

    for kv in args.set or []:
        if "=" not in kv:
            print(f"error: --set expects KEY=VALUE, got {kv!r}", file=sys.stderr)
            sys.exit(1)
        key, _, raw_val = kv.partition("=")
        try:
            val = json.loads(raw_val)
        except json.JSONDecodeError:
            val = raw_val
        set_dotted(merged, key.strip(), val)

    if args.json_body:
        merged = json.loads(args.json_body)

    print("About to apply the following LAN settings (this WILL drop connectivity):")
    pretty_print(merged, indent=1)

    if client.dry_run:
        client.call("router/SetLANSettings", merged, destructive=True)
        return

    confirm_or_abort(
        f"WARNING: changing LAN settings on {client.host} can disconnect this session and all clients.",
        args.yes,
    )
    client.call("router/SetLANSettings", merged, destructive=True)
    print("LAN settings updated. Clients may need to renew their DHCP lease.")


def cmd_wifi(client, args):
    for action in ("wirelessap/GetWirelessSettings", "wirelessap/GetRadioSettings"):
        resp = client.call(action, check=False)
        if resp.get("result") == "OK":
            if args.json:
                dump_json(resp)
            else:
                print(f"(via {action})")
                pretty_print(resp.get("output", {}))
            return
    print(
        "error: neither wirelessap/GetWirelessSettings nor wirelessap/GetRadioSettings "
        "returned OK on this firmware",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_devices(client, args):
    def progress(start, end, count):
        print(f"  window ({start}, {end}] -> {count} device(s) so far", file=sys.stderr)

    try:
        revision, devices, gaps = read_all_devices(
            client, window=args.window, progress=None if args.json else progress
        )
    except JNAPError as e:
        handle_device_db_error(e)

    if args.json:
        print(json.dumps(
            {"revision": revision, "devices": list(devices.values()), "unreadableRanges": gaps},
            indent=2,
        ))
        return

    print(f"DB revision: {revision}")
    print(f"Devices found: {len(devices)}")
    if gaps:
        print(f"Warning: {len(gaps)} revision range(s) could not be read (likely corrupted entries):")
        for s, e in gaps:
            print(f"  ({s}, {e}]")
    for d in devices.values():
        label = d.get("friendlyName") or d.get("name") or ""
        print(f"  {d.get('deviceID')}  {label}")
        if args.verbose:
            print(f"    {json.dumps(d)}")


def cmd_devices_clear(client, args):
    try:
        revision, devices, gaps = read_all_devices(client, window=args.window)
    except JNAPError as e:
        handle_device_db_error(e)

    device_ids = list(devices.keys())
    print(f"Found {len(device_ids)} device(s) to delete (DB revision {revision}).")
    if gaps:
        print(
            f"Note: {len(gaps)} revision range(s) were unreadable and may hide additional devices."
        )
    if not device_ids:
        print("Nothing to delete.")
        return

    if client.dry_run:
        for did in device_ids:
            client.call("devicelist/DeleteDevice", {"deviceID": did}, destructive=True)
        return

    confirm_or_abort(f"This will delete all {len(device_ids)} device entries from the DB.", args.yes)

    ok = 0
    failed = 0
    for i, did in enumerate(device_ids, 1):
        resp = client.call("devicelist/DeleteDevice", {"deviceID": did}, check=False, destructive=True)
        if resp.get("result") == "OK":
            ok += 1
        else:
            failed += 1
            print(f"  [{i}/{len(device_ids)}] failed to delete {did}: {resp.get('result')}", file=sys.stderr)
        if i % 10 == 0 or i == len(device_ids):
            print(f"  progress: {i}/{len(device_ids)} (ok: {ok}, failed: {failed})", file=sys.stderr)

    print(f"Done. Deleted {ok}, failed {failed}.")
    print("Verifying sinceRevision=0 now works...")
    verify = client.call("devicelist/GetDevices", {"sinceRevision": 0}, check=False)
    if verify.get("result") == "OK":
        count = len(verify.get("output", {}).get("devices", []))
        print(f"Verification OK: sinceRevision=0 now returns {count} device(s).")
    else:
        print(f"Verification FAILED: sinceRevision=0 still returns {verify.get('result')}.", file=sys.stderr)
        sys.exit(1)


def cmd_pc_get(client, args):
    resp = client.call("parentalcontrol/GetParentalControlSettings")
    if args.json:
        dump_json(resp)
        return
    o = resp.get("output", {})
    print(f"Parental control enabled: {o.get('isParentalControlEnabled')}")
    rules = o.get("rules", [])
    print(f"Rules ({len(rules)}):")
    for r in rules:
        macs = ", ".join(r.get("macAddresses", []))
        name = r.get("description") or r.get("ruleName") or "(unnamed)"
        print(f"  - {name}  MACs: {macs}")
        if r.get("blockedURLs"):
            print(f"      Blocked URLs: {', '.join(r['blockedURLs'])}")
        if r.get("wanSchedule"):
            print("      Schedule:")
            print(format_wan_schedule(r["wanSchedule"]))


def cmd_pc_disable(client, args):
    body = {"isParentalControlEnabled": False, "rules": []}
    if client.dry_run:
        client.call("parentalcontrol/SetParentalControlSettings", body, destructive=True)
        return
    confirm_or_abort("This will disable parental control and remove ALL rules.", args.yes)
    client.call("parentalcontrol/SetParentalControlSettings", body, destructive=True)
    print("Parental control disabled and rules cleared.")


def cmd_pc_rm(client, args):
    mac = args.mac.strip().lower()
    resp = client.call("parentalcontrol/GetParentalControlSettings")
    o = resp.get("output", {})
    rules = o.get("rules", [])
    kept = [r for r in rules if mac not in [m.lower() for m in r.get("macAddresses", [])]]
    if len(kept) == len(rules):
        print(f"No rule found containing MAC {args.mac}.", file=sys.stderr)
        sys.exit(1)

    body = {"isParentalControlEnabled": o.get("isParentalControlEnabled", True), "rules": kept}
    print(
        f"Removing {len(rules) - len(kept)} rule(s) matching MAC {args.mac}; "
        f"{len(kept)} rule(s) will remain."
    )

    if client.dry_run:
        client.call("parentalcontrol/SetParentalControlSettings", body, destructive=True)
        return

    confirm_or_abort(f"About to remove the parental control rule for {args.mac}.", args.yes)
    client.call("parentalcontrol/SetParentalControlSettings", body, destructive=True)
    print("Updated.")


def cmd_reboot(client, args):
    if client.dry_run:
        client.call("core/Reboot", {}, destructive=True)
        return
    confirm_or_abort(f"This will reboot the router at {client.host}.", args.yes)
    client.call("core/Reboot", {}, destructive=True)
    print("Reboot command sent.")


def cmd_raw(client, args):
    try:
        body = json.loads(args.body) if args.body else {}
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON body: {e}", file=sys.stderr)
        sys.exit(1)
    resp = client.call(args.action, body, check=False, destructive=True)
    dump_json(resp)


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="velopctl",
        description="Administer a Linksys Velop mesh router over its local JNAP HTTP API.",
    )
    p.add_argument("--host", help=f"router hostname/IP (default: {DEFAULT_HOST}, or config file)")
    p.add_argument("--username", help=f"JNAP username (default: {DEFAULT_USERNAME}, or config file)")
    p.add_argument("--config", help=f"path to config file (default: {CONFIG_PATH})")
    p.add_argument("--json", action="store_true", help="print raw JSON responses instead of human-readable output")
    p.add_argument(
        "--dry-run", action="store_true",
        help="print intended JNAP calls for destructive operations instead of sending them",
    )
    p.add_argument("--yes", "-y", action="store_true", help="skip interactive confirmation prompts")
    p.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds (default: 15)")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="prompt for and save router host + credentials to the config file")

    sub.add_parser("info", help="show device model, firmware, serial, and supported services")

    sub.add_parser("wan", help="show WAN type/configuration")

    lan = sub.add_parser("lan", help="LAN settings")
    lan_sub = lan.add_subparsers(dest="lan_command", required=True)
    lan_sub.add_parser("get", help="show current LAN settings")
    lan_set = lan_sub.add_parser(
        "set", help="change LAN settings (drops connectivity; asks for confirmation)"
    )
    lan_set.add_argument(
        "--set", action="append", metavar="KEY=VALUE",
        help="dotted-path field override merged onto the current settings, "
             "e.g. dhcpSettings.dhcpEnabled=false (repeatable)",
    )
    lan_set.add_argument(
        "--json-body",
        help="replace the entire SetLANSettings body with this raw JSON instead of merging --set overrides",
    )

    sub.add_parser("wifi", help="show wireless settings (probes GetWirelessSettings, then GetRadioSettings)")

    devices = sub.add_parser("devices", help="list devices via a paged reader that avoids the corrupted-DB crash")
    devices.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW,
        help=f"revision window size per page (default: {DEFAULT_WINDOW})",
    )
    devices.add_argument("--verbose", action="store_true", help="print full JSON for each device")
    devices_sub = devices.add_subparsers(dest="devices_command")
    devices_sub.add_parser(
        "clear",
        help="delete every device found via the paged reader, then verify sinceRevision=0 works",
    )

    pc = sub.add_parser("pc", help="parental control settings")
    pc_sub = pc.add_subparsers(dest="pc_command", required=True)
    pc_sub.add_parser("get", help="show parental control settings and rules (with human-readable schedules)")
    pc_sub.add_parser("disable", help="disable parental control and clear all rules")
    pc_rm = pc_sub.add_parser("rm", help="remove the rule containing a given MAC address")
    pc_rm.add_argument("mac", help="MAC address to remove, e.g. AA:BB:CC:DD:EE:FF")

    sub.add_parser("reboot", help="reboot the router (asks for confirmation)")

    raw = sub.add_parser("raw", help="send an arbitrary JNAP action - escape hatch for experimentation")
    raw.add_argument(
        "action",
        help='action suffix, e.g. "core/GetDeviceInfo" '
             "(http://linksys.com/jnap/ is prefixed automatically unless already a full URL)",
    )
    raw.add_argument("body", nargs="?", default=None, help="JSON request body (default: {})")

    return p


def dispatch(client, args):
    if args.command == "info":
        cmd_info(client, args)
    elif args.command == "wan":
        cmd_wan(client, args)
    elif args.command == "lan":
        if args.lan_command == "get":
            cmd_lan_get(client, args)
        else:
            cmd_lan_set(client, args)
    elif args.command == "wifi":
        cmd_wifi(client, args)
    elif args.command == "devices":
        if args.devices_command == "clear":
            cmd_devices_clear(client, args)
        else:
            cmd_devices(client, args)
    elif args.command == "pc":
        if args.pc_command == "get":
            cmd_pc_get(client, args)
        elif args.pc_command == "disable":
            cmd_pc_disable(client, args)
        else:
            cmd_pc_rm(client, args)
    elif args.command == "reboot":
        cmd_reboot(client, args)
    elif args.command == "raw":
        cmd_raw(client, args)


def main():
    args = build_parser().parse_args()

    if args.command == "login":
        cmd_login(args)
        return

    config = load_config(args.config)
    host = args.host or config.get("host") or DEFAULT_HOST
    username = args.username or config.get("username") or DEFAULT_USERNAME
    password = config.get("password") or os.environ.get("VELOP_PASSWORD")
    if not password:
        print(
            "error: no password configured. Run 'velopctl login' or set VELOP_PASSWORD.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = JNAPClient(host, username, password, dry_run=args.dry_run, timeout=args.timeout)

    try:
        dispatch(client, args)
    except JNAPError as e:
        handle_jnap_error(e)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
