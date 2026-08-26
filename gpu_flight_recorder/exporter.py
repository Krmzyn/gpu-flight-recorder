from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .io_utils import parse_iso, read_jsonl, safe_float, write_jsonl_lines
from .report import generate_report, load_session, summarize_session


def _records_since(records: list[dict[str, Any]], cutoff) -> list[dict[str, Any]]:
    kept = []
    for record in records:
        timestamp = parse_iso(record.get("timestamp") or record.get("TimeCreated"))
        if timestamp and timestamp >= cutoff:
            kept.append(record)
    return kept


def _chart_csv(samples: list[dict[str, Any]]) -> str:
    handle = io.StringIO()
    writer = csv.writer(handle)
    writer.writerow(["timestamp", "gpu_util_percent", "gpu_temp_c", "gpu_power_w", "gpu_memory_used_mib"])
    for sample in samples:
        payload = sample.get("nvidia", sample)
        gpu = payload.get("primary_gpu") or {}
        writer.writerow(
            [
                sample.get("timestamp"),
                safe_float(gpu.get("utilization_gpu")),
                safe_float(gpu.get("temperature_gpu")),
                safe_float(gpu.get("power_draw")) or safe_float(gpu.get("power_draw_average")),
                safe_float(gpu.get("memory_used")),
            ]
        )
    return handle.getvalue()


def export_support_bundle(session_dir: Path, config: Config, include_raw: bool = False) -> Path:
    report_path = generate_report(session_dir)
    data = load_session(session_dir)
    summary = summarize_session(session_dir, support_minutes=config.support_bundle_minutes)
    end = parse_iso(summary.get("ended_at")) or parse_iso(summary.get("started_at"))
    if end is None:
        from datetime import datetime

        end = datetime.now().astimezone()
    cutoff = end - timedelta(minutes=config.support_bundle_minutes)

    heartbeat = _records_since(data["heartbeat"], cutoff)
    gpu_samples = _records_since(data["gpu_samples"], cutoff)
    gpu_failures = _records_since(data["gpu_probe_failures"], cutoff)
    keyboard = _records_since(data["keyboard_state"], cutoff)
    display = _records_since(data["display_events"], cutoff)
    diagnostics = _records_since(data["diagnostics"], cutoff)
    live_kernel = _records_since(data["live_kernel_reports"], cutoff)
    events = _records_since(data["events"], cutoff)
    power_events = _records_since(data["power_events"], cutoff)
    processes = _records_since(data["processes"], cutoff)
    markers = _records_since(data["markers"], cutoff)

    zip_path = session_dir / f"support_bundle_{session_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(report_path, "final_report.md")
        archive.writestr("summary.json", json.dumps(summary, indent=2, default=str) + "\n")
        archive.writestr("config.redacted.json", json.dumps(config.redacted_dict(), indent=2, default=str) + "\n")
        archive.writestr(f"heartbeat.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(heartbeat))
        archive.writestr(f"gpu_samples.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(gpu_samples))
        archive.writestr(f"gpu_probe_failures.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(gpu_failures))
        archive.writestr(f"keyboard_state.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(keyboard))
        archive.writestr(f"display_events.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(display))
        archive.writestr(f"diagnostics.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(diagnostics))
        archive.writestr(f"live_kernel_reports.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(live_kernel))
        archive.writestr(f"events.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(events))
        archive.writestr(f"power_events.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(power_events))
        archive.writestr(f"processes.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(processes))
        archive.writestr(f"markers.last_{config.support_bundle_minutes}m.jsonl", write_jsonl_lines(markers))
        archive.writestr("chart_data.csv", _chart_csv(gpu_samples))
        if include_raw:
            for filename in (
                "heartbeat.jsonl",
                "gpu_samples.jsonl",
                "gpu_probe_failures.jsonl",
                "keyboard_state.jsonl",
                "display_events.jsonl",
                "diagnostics.jsonl",
                "live_kernel_reports.jsonl",
                "events.jsonl",
                "power_events.jsonl",
                "processes.jsonl",
                "markers.jsonl",
                "session_info.json",
                "samples.jsonl",
            ):
                path = session_dir / filename
                if path.exists():
                    archive.write(path, f"raw/{filename}")
    return zip_path
