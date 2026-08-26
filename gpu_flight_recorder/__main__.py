from __future__ import annotations

import sys
from datetime import datetime, timedelta

from .marker import add_marker_to_active


def _verify_events(argv: list[str]) -> None:
    days = 30
    for index, arg in enumerate(argv):
        if arg == "--days" and index + 1 < len(argv):
            days = int(argv[index + 1])
    from .collectors import collect_events_since
    from .config import load_config

    since = (datetime.now().astimezone() - timedelta(days=max(0, days))).isoformat(timespec="milliseconds")
    record = collect_events_since(load_config(), since, set(), capture_reason="verify_events")
    print(f"Event verification window: last {days} day(s)")
    print(f"Captured relevant events: {len(record.get('events', []))}")
    print(f"Captured power events: {len(record.get('power_events', []))}")
    print("")
    print("Critical queries:")
    for item in record.get("query_health", []):
        severity = str(item.get("Severity") or "critical").lower()
        if severity != "critical":
            continue
        status = "ok" if item.get("Success") else f"error: {item.get('Message')}"
        print(
            "- {name}: raw={raw}, captured={captured}, status={status}".format(
                name=item.get("QueryName"),
                raw=item.get("EventCount", 0),
                captured=item.get("CapturedEventCount", 0),
                status=status,
            )
        )
    optional_errors = [
        item
        for item in record.get("query_health", [])
        if str(item.get("Severity") or "").lower() == "optional" and not item.get("Success")
    ]
    if optional_errors:
        print("")
        print("Optional channel notes:")
        for item in optional_errors:
            print(f"- {item.get('QueryName')}: {item.get('Message')}")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "marker":
        note = " ".join(sys.argv[2:]).strip()
        if not note:
            raise SystemExit('Usage: python -m gpu_flight_recorder marker "display froze / observed"')
        path = add_marker_to_active(note)
        print(f"Marker written to {path}")
        return

    if len(sys.argv) >= 2 and sys.argv[1].lower() == "verify-events":
        _verify_events(sys.argv[2:])
        return

    from .tk_bootstrap import ensure_tcl_tk_paths

    ensure_tcl_tk_paths()

    from .gui import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
