"""Extract per-device test_data (the JSONB payload testkit consumes).

Usage::

    python3 scripts/extract_device_test_data.py
    python3 scripts/extract_device_test_data.py --json --output devices.json
    python3 scripts/extract_device_test_data.py --only-missing

Environment:

    GRIDFLEET_API_URL              Backend base URL incl. /api (default http://localhost:8000/api)
    GRIDFLEET_TESTKIT_USERNAME     Basic auth username (optional)
    GRIDFLEET_TESTKIT_PASSWORD     Basic auth password (optional)

Devices whose ``test_data`` is empty (``{}``) are flagged in the table output and
collected under ``missing`` in the JSON output so they can be triaged.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000/api"
PAGE_LIMIT = 200
TIMEOUT_SEC = 15.0

RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _auth_header() -> dict[str, str]:
    user = os.getenv("GRIDFLEET_TESTKIT_USERNAME")
    pw = os.getenv("GRIDFLEET_TESTKIT_PASSWORD")
    if not user or not pw:
        return {}
    token = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _get_json(base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{base_url}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json", **_auth_header()})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} fetching {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach {url}: {exc.reason}") from exc


def fetch_devices(base_url: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = _get_json(base_url, "/devices", {"limit": PAGE_LIMIT, "offset": offset})
        if isinstance(payload, list):
            devices.extend(payload)
            break
        items = payload.get("items", [])
        devices.extend(items)
        total = payload.get("total")
        offset += len(items)
        if not items or (total is not None and offset >= total):
            break
    return devices


def fetch_test_data(base_url: str, device_id: str) -> dict[str, Any]:
    payload = _get_json(base_url, f"/devices/{device_id}/test_data")
    if not isinstance(payload, dict):
        return {}
    return payload


def _label(device: dict[str, Any]) -> str:
    return (
        device.get("name")
        or device.get("connection_target")
        or device.get("identity_value")
        or device.get("id", "?")
    )


def _is_missing(test_data: dict[str, Any]) -> bool:
    return not test_data


def render_table(rows: list[dict[str, Any]], *, only_missing: bool, use_color: bool) -> str:
    headers = ("device_id", "name", "pack", "host", "test_data")
    visible = [row for row in rows if not only_missing or row["missing"]]

    def colorize(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if use_color else text

    widths = {h: len(h) for h in headers}
    for row in visible:
        for header in headers:
            widths[header] = max(widths[header], len(str(row[header])))

    lines = [
        "  ".join(colorize(h.ljust(widths[h]), BOLD) for h in headers),
        "  ".join("-" * widths[h] for h in headers),
    ]
    for row in visible:
        cells = []
        for header in headers:
            cell = str(row[header]).ljust(widths[header])
            if header == "test_data" and row["missing"]:
                cell = colorize(cell, RED + BOLD)
            cells.append(cell)
        lines.append("  ".join(cells))

    total = len(rows)
    missing = sum(1 for row in rows if row["missing"])
    summary = (
        f"\n{colorize(str(total), BOLD)} devices, "
        f"{colorize(str(missing), RED + BOLD)} without test_data, "
        f"{colorize(str(total - missing), GREEN)} populated."
    )
    lines.append(summary)
    return "\n".join(lines)


def build_rows(base_url: str, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in devices:
        device_id = device.get("id")
        if not device_id:
            continue
        test_data = fetch_test_data(base_url, device_id)
        rows.append(
            {
                "device_id": device_id,
                "name": _label(device),
                "pack": device.get("pack_id") or "-",
                "host": device.get("host_id") or "-",
                "platform": device.get("platform_label") or device.get("platform_id") or "-",
                "operational_state": device.get("operational_state") or "-",
                "test_data": test_data if test_data else "{} (empty)",
                "test_data_raw": test_data,
                "missing": _is_missing(test_data),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url",
        default=os.getenv("GRIDFLEET_API_URL", DEFAULT_BASE_URL).rstrip("/"),
        help="Backend API base URL including /api (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    parser.add_argument("--only-missing", action="store_true", help="Show only devices without test_data.")
    parser.add_argument("--output", type=str, default=None, help="Write output to this path instead of stdout.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color in table output.")
    args = parser.parse_args(argv)

    devices = fetch_devices(args.base_url)
    rows = build_rows(args.base_url, devices)

    if args.json:
        payload = {
            "base_url": args.base_url,
            "total": len(rows),
            "missing": [
                {"device_id": row["device_id"], "name": row["name"], "pack": row["pack"], "host": row["host"]}
                for row in rows
                if row["missing"]
            ],
            "devices": [
                {
                    "device_id": row["device_id"],
                    "name": row["name"],
                    "pack": row["pack"],
                    "host": row["host"],
                    "platform": row["platform"],
                    "operational_state": row["operational_state"],
                    "test_data": row["test_data_raw"],
                    "missing": row["missing"],
                }
                for row in rows
                if not args.only_missing or row["missing"]
            ],
        }
        output = json.dumps(payload, indent=2, sort_keys=True, default=str)
    else:
        use_color = not args.no_color and (args.output is None and sys.stdout.isatty())
        output = render_table(rows, only_missing=args.only_missing, use_color=use_color)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fp:
            fp.write(output)
            if not output.endswith("\n"):
                fp.write("\n")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
