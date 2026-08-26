from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .io_utils import parse_iso, read_jsonl, safe_float, short_text


def _redact_path(path: Path) -> str:
    try:
        home = str(Path.home())
        text = str(path)
        if text.lower().startswith(home.lower()):
            return "%USERPROFILE%" + text[len(home) :]
        return text
    except Exception:
        return str(path)


def _record_time(record: dict[str, Any]) -> datetime | None:
    return parse_iso(record.get("timestamp") or record.get("TimeCreated"))


def _latest_timestamp(records: list[dict[str, Any]]) -> datetime | None:
    dates = [_record_time(record) for record in records]
    dates = [date for date in dates if date is not None]
    return max(dates) if dates else None


def _session_end(session_info: dict[str, Any], *groups: list[dict[str, Any]]) -> datetime:
    end = parse_iso(session_info.get("end_time")) or parse_iso(session_info.get("last_heartbeat"))
    latest = [date for date in (_latest_timestamp(group) for group in groups) if date is not None]
    if latest:
        newest = max(latest)
        if end is None or newest > end:
            end = newest
    return end or datetime.now().astimezone()


def _near(records: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    result = []
    for record in records:
        timestamp = _record_time(record)
        if timestamp and start <= timestamp <= end:
            result.append(record)
    return result


def _near_end(records: list[dict[str, Any]], end: datetime, minutes: int) -> list[dict[str, Any]]:
    return _near(records, end - timedelta(minutes=minutes), end)


STREAM_KEYS = [
    "heartbeat",
    "gpu_samples",
    "gpu_probe_failures",
    "keyboard_state",
    "display_events",
    "diagnostics",
    "live_kernel_reports",
    "events",
    "power_events",
    "processes",
    "markers",
]


def _stream_counts_from_data(data: dict[str, Any]) -> dict[str, int]:
    return {key: len(data.get(key) or []) for key in STREAM_KEYS}


def _latest_diagnostic(data: dict[str, Any], diagnostic_type: str) -> dict[str, Any] | None:
    return next((item for item in reversed(data["diagnostics"]) if item.get("type") == diagnostic_type), None)


def _best_stream_counts(data: dict[str, Any]) -> tuple[dict[str, int], list[str], str]:
    latest_summary = _latest_diagnostic(data, "session_stream_summary")
    latest_health = _latest_diagnostic(data, "collector_health")
    source = "computed_jsonl"
    warnings: list[str] = []
    counts: dict[str, int] = {}

    if latest_summary and latest_summary.get("stream_counts"):
        counts = dict(latest_summary.get("stream_counts") or {})
        warnings.extend(latest_summary.get("warnings") or [])
        source = "session_stream_summary"
    elif latest_health and latest_health.get("stream_counts"):
        counts = dict(latest_health.get("stream_counts") or {})
        source = "collector_health"

    actual = _stream_counts_from_data(data)
    if not counts:
        return actual, warnings, source

    stale = any(actual.get(key, 0) > int(counts.get(key, 0) or 0) for key in actual if key != "diagnostics")
    if stale:
        warnings.append("Collector health snapshot was stale; stream counts were computed directly from JSONL files.")
        return actual, warnings, "computed_jsonl"
    return counts, warnings, source


def _gpu_payload(sample: dict[str, Any]) -> dict[str, Any]:
    if "primary_gpu" in sample or "gpus" in sample:
        return sample
    return sample.get("nvidia", {}) or {}


def _gpu_value(sample: dict[str, Any], field: str) -> Any:
    payload = _gpu_payload(sample)
    gpu = payload.get("primary_gpu") or {}
    return gpu.get(field)


def _legacy_gpu_samples(legacy_samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for sample in legacy_samples:
        nvidia = dict(sample.get("nvidia") or {})
        nvidia.setdefault("timestamp", sample.get("timestamp"))
        converted.append(nvidia)
    return converted


def load_session(session_dir: Path) -> dict[str, Any]:
    info_path = session_dir / "session_info.json"
    session_info: dict[str, Any] = {}
    if info_path.exists():
        with info_path.open("r", encoding="utf-8") as handle:
            session_info = json.load(handle)
    legacy_samples = read_jsonl(session_dir / "samples.jsonl")
    gpu_samples = read_jsonl(session_dir / "gpu_samples.jsonl")
    if not gpu_samples and legacy_samples:
        gpu_samples = [sample for sample in _legacy_gpu_samples(legacy_samples) if sample.get("responded")]
    gpu_failures = read_jsonl(session_dir / "gpu_probe_failures.jsonl")
    if not gpu_failures and legacy_samples:
        gpu_failures = [sample for sample in _legacy_gpu_samples(legacy_samples) if not sample.get("responded", False)]
    return {
        "session_info": session_info,
        "legacy_samples": legacy_samples,
        "heartbeat": read_jsonl(session_dir / "heartbeat.jsonl"),
        "gpu_samples": gpu_samples,
        "gpu_probe_failures": gpu_failures,
        "keyboard_state": read_jsonl(session_dir / "keyboard_state.jsonl"),
        "display_events": read_jsonl(session_dir / "display_events.jsonl"),
        "diagnostics": read_jsonl(session_dir / "diagnostics.jsonl"),
        "live_kernel_reports": read_jsonl(session_dir / "live_kernel_reports.jsonl"),
        "events": read_jsonl(session_dir / "events.jsonl"),
        "power_events": read_jsonl(session_dir / "power_events.jsonl"),
        "processes": read_jsonl(session_dir / "processes.jsonl"),
        "markers": read_jsonl(session_dir / "markers.jsonl"),
    }


def _analysis_window(data: dict[str, Any], end: datetime) -> tuple[datetime, datetime, str]:
    markers = data["markers"]
    freeze_markers = [
        marker for marker in markers if any(token in str(marker.get("note", "")).lower() for token in ("freeze", "froze"))
    ]
    if not freeze_markers:
        return end - timedelta(minutes=5), end, "final 5 minutes"
    freeze_time = _record_time(freeze_markers[-1])
    if freeze_time is None:
        return end - timedelta(minutes=5), end, "final 5 minutes"
    recovery_time = None
    for marker in markers:
        note = str(marker.get("note", "")).lower()
        marker_time = _record_time(marker)
        if marker_time and marker_time > freeze_time and any(token in note for token in ("came back", "recovered", "responded")):
            recovery_time = marker_time
            break
    return freeze_time - timedelta(seconds=15), recovery_time or freeze_time + timedelta(minutes=5), "display-freeze marker"


def _classifier_setting(info: dict[str, Any], override: Config | None, name: str) -> Any:
    if override is not None:
        return getattr(override, name)
    config = info.get("config") or {}
    return config.get(name, getattr(Config(), name))


def _failure_elapsed_ms(record: dict[str, Any]) -> float:
    elapsed = safe_float(record.get("elapsed_ms"))
    if elapsed is not None:
        return elapsed
    timeout_sec = safe_float(record.get("timeout_sec"))
    if timeout_sec is not None:
        return timeout_sec * 1000.0
    return 0.0


def _score_gpu_failures(
    failures: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    info: dict[str, Any],
    basis: str,
    config: Config | None,
) -> dict[str, Any]:
    session_start = parse_iso(info.get("start_time"))
    warmup_seconds = float(_classifier_setting(info, config, "freeze_classifier_warmup_seconds"))
    strong_ms = float(_classifier_setting(info, config, "freeze_classifier_strong_failure_ms"))
    minor_ms = float(_classifier_setting(info, config, "freeze_classifier_minor_failure_ms"))
    cluster_window_sec = float(_classifier_setting(info, config, "freeze_classifier_cluster_window_sec"))
    cluster_min_failures = int(_classifier_setting(info, config, "freeze_classifier_cluster_min_failures"))
    cluster_total_ms = float(_classifier_setting(info, config, "freeze_classifier_cluster_total_ms"))

    automatic_window = basis == "final 5 minutes"
    considered: list[dict[str, Any]] = []
    ignored_startup: list[dict[str, Any]] = []
    minor: list[dict[str, Any]] = []

    for failure in failures:
        failure_time = _record_time(failure)
        elapsed_ms = _failure_elapsed_ms(failure)
        if automatic_window and session_start and failure_time:
            age = (failure_time - session_start).total_seconds()
            if age < warmup_seconds:
                ignored_startup.append(failure)
                continue
        if elapsed_ms < minor_ms:
            minor.append(failure)
        considered.append(failure)

    strong_failures = [failure for failure in considered if _failure_elapsed_ms(failure) >= strong_ms]

    cluster_failures: list[dict[str, Any]] = []
    ordered = [failure for failure in considered if _record_time(failure)]
    ordered.sort(key=lambda item: _record_time(item) or datetime.min)
    for index, failure in enumerate(ordered):
        start_time = _record_time(failure)
        if start_time is None:
            continue
        cluster = [
            item
            for item in ordered[index:]
            if _record_time(item) and 0 <= ((_record_time(item) or start_time) - start_time).total_seconds() <= cluster_window_sec
        ]
        total_ms = sum(_failure_elapsed_ms(item) for item in cluster)
        if len(cluster) >= cluster_min_failures and total_ms >= cluster_total_ms:
            cluster_failures = cluster
            break

    stall_diags = []
    for diagnostic in diagnostics:
        if diagnostic.get("type") != "gpu_probe_stalled":
            continue
        gap = safe_float(diagnostic.get("gap_seconds")) or 0.0
        if gap * 1000.0 >= strong_ms:
            stall_diags.append(diagnostic)

    significant = bool(strong_failures or cluster_failures or stall_diags)
    return {
        "significant": significant,
        "strong_failures": strong_failures,
        "cluster_failures": cluster_failures,
        "stall_diagnostics": stall_diags,
        "ignored_startup": ignored_startup,
        "minor_failures": minor,
        "considered_failures": considered,
        "settings": {
            "warmup_seconds": warmup_seconds,
            "strong_failure_ms": strong_ms,
            "minor_failure_ms": minor_ms,
            "cluster_window_sec": cluster_window_sec,
            "cluster_min_failures": cluster_min_failures,
            "cluster_total_ms": cluster_total_ms,
        },
    }


def freeze_classification(session_dir: Path, config: Config | None = None) -> list[str]:
    data = load_session(session_dir)
    info = data["session_info"]
    end = _session_end(
        info,
        data["heartbeat"],
        data["gpu_samples"],
        data["gpu_probe_failures"],
        data["events"],
        data["power_events"],
        data["markers"],
        data["diagnostics"],
    )
    start, stop, basis = _analysis_window(data, end)
    heartbeats = _near(data["heartbeat"], start, stop)
    gpu_samples = _near(data["gpu_samples"], start, stop)
    gpu_failures = _near(data["gpu_probe_failures"], start, stop)
    keyboard = _near(data["keyboard_state"], start, stop)
    diagnostics = _near(data["diagnostics"], start, stop)
    markers = _near(data["markers"], start, stop)

    diagnostic_types = {record.get("type") for record in diagnostics}
    marker_text = " ".join(str(marker.get("note", "")).lower() for marker in markers)
    classifications: list[str] = [f"Analysis window uses {basis}: {start.isoformat(timespec='seconds')} to {stop.isoformat(timespec='seconds')}."]
    failure_score = _score_gpu_failures(gpu_failures, diagnostics, info, basis, config)

    if not heartbeats:
        classifications.append("Evidence insufficient: no heartbeat records exist in the analysis window.")
    else:
        classifications.append("Heartbeat evidence present: logger/Windows proof-of-life records continued in the analysis window.")

    if failure_score["ignored_startup"]:
        settings = failure_score["settings"]
        classifications.append(
            f"Ignored {len(failure_score['ignored_startup'])} startup GPU probe blip(s) inside the first {settings['warmup_seconds']:.0f} seconds."
        )
    if failure_score["considered_failures"] and not failure_score["significant"]:
        classifications.append("GPU probe failures were minor or isolated and did not meet the sustained-stall threshold.")

    if heartbeats and failure_score["significant"]:
        keyboard_signal = bool(keyboard) or "caps lock responded" in marker_text or "audio continued" in marker_text
        if keyboard_signal:
            classifications.append("Display-path stall likely: heartbeat continued, input/audio evidence exists, and GPU probing failed or stalled.")
        else:
            classifications.append("NVIDIA management path wedge: heartbeat continued while nvidia-smi timed out, failed, or stopped producing successful samples.")

    if info.get("status") != "closed" and diagnostic_types and "system_or_process_stall_suspected" in diagnostic_types:
        classifications.append("Full system stall possible: previous session ended uncleanly and heartbeat/GPU samples stopped together.")
    elif not heartbeats and (info.get("status") != "closed"):
        classifications.append("Full system stall possible: no heartbeat evidence survived near the end of an unclean session.")

    temps = [safe_float(_gpu_value(sample, "temperature_gpu")) for sample in gpu_samples]
    powers = [safe_float(_gpu_value(sample, "power_draw")) or safe_float(_gpu_value(sample, "power_draw_average")) for sample in gpu_samples]
    mems = [safe_float(_gpu_value(sample, "memory_used")) for sample in gpu_samples]
    max_temp = max((value for value in temps if value is not None), default=None)
    max_power = max((value for value in powers if value is not None), default=None)
    max_mem = max((value for value in mems if value is not None), default=None)
    if gpu_samples and (max_temp is None or max_temp < 82) and (max_power is None or max_power < 300) and (max_mem is None or max_mem < 9000):
        classifications.append("Thermal/power pressure unlikely: temperature, power, and VRAM were not elevated in the analysis window.")

    if not gpu_failures and "gpu_probe_stalled" not in diagnostic_types and not gpu_samples:
        classifications.append("Evidence insufficient: no GPU samples or GPU probe failures exist in the analysis window.")
    return classifications


def summarize_session(session_dir: Path, support_minutes: int = 10) -> dict[str, Any]:
    data = load_session(session_dir)
    info = data["session_info"]
    end = _session_end(
        info,
        data["heartbeat"],
        data["gpu_samples"],
        data["gpu_probe_failures"],
        data["events"],
        data["power_events"],
        data["processes"],
        data["markers"],
        data["diagnostics"],
    )
    recent_gpu = _near_end(data["gpu_samples"], end, support_minutes)
    recent_events = _near_end(data["events"], end, support_minutes)
    recent_power_events = _near_end(data["power_events"], end, support_minutes)
    recent_processes = _near_end(data["processes"], end, support_minutes)
    recent_heartbeat = _near_end(data["heartbeat"], end, support_minutes)
    recent_failures = _near_end(data["gpu_probe_failures"], end, support_minutes)
    recent_markers = _near_end(data["markers"], end, support_minutes)

    temps = [safe_float(_gpu_value(sample, "temperature_gpu")) for sample in data["gpu_samples"]]
    powers = [safe_float(_gpu_value(sample, "power_draw")) or safe_float(_gpu_value(sample, "power_draw_average")) for sample in data["gpu_samples"]]
    memory_used = [safe_float(_gpu_value(sample, "memory_used")) for sample in data["gpu_samples"]]
    gpu_util = [safe_float(_gpu_value(sample, "utilization_gpu")) for sample in data["gpu_samples"]]
    last_process_record = recent_processes[-1] if recent_processes else (data["processes"][-1] if data["processes"] else {})
    event_counts = Counter(str(event.get("ProviderName") or "Unknown") for event in recent_events)
    stream_counts, stream_warnings, stream_count_source = _best_stream_counts(data)

    return {
        "session_dir": _redact_path(session_dir),
        "status": info.get("status", "unknown"),
        "started_at": info.get("start_time"),
        "ended_at": info.get("end_time") or info.get("last_heartbeat") or end.isoformat(timespec="milliseconds"),
        "clean_exit": info.get("status") == "closed",
        "heartbeat_count": len(data["heartbeat"]),
        "recent_heartbeat_count": len(recent_heartbeat),
        "gpu_sample_count": len(data["gpu_samples"]),
        "recent_gpu_sample_count": len(recent_gpu),
        "gpu_probe_failure_count": len(data["gpu_probe_failures"]),
        "recent_gpu_probe_failure_count": len(recent_failures),
        "event_count": len(data["events"]),
        "recent_event_count": len(recent_events),
        "power_event_count": len(data["power_events"]),
        "recent_power_event_count": len(recent_power_events),
        "marker_count": len(data["markers"]),
        "recent_marker_count": len(recent_markers),
        "keyboard_state_count": len(data["keyboard_state"]),
        "display_event_count": len(data["display_events"]),
        "diagnostic_count": len(data["diagnostics"]),
        "live_kernel_scan_count": len(data["live_kernel_reports"]),
        "max_gpu_temp_c": max((value for value in temps if value is not None), default=None),
        "max_gpu_power_w": max((value for value in powers if value is not None), default=None),
        "max_gpu_memory_used_mib": max((value for value in memory_used if value is not None), default=None),
        "max_gpu_util_percent": max((value for value in gpu_util if value is not None), default=None),
        "nvidia_smi_failures": len(data["gpu_probe_failures"]),
        "running_target_processes_near_end": last_process_record.get("processes", []),
        "recent_event_providers": dict(event_counts),
        "stream_counts": stream_counts,
        "stream_count_source": stream_count_source,
        "stream_warnings": stream_warnings,
    }


def suspicion_hints(session_dir: Path) -> list[str]:
    data = load_session(session_dir)
    classifications = freeze_classification(session_dir)
    hints = [item for item in classifications if not item.startswith("Analysis window")]
    if data["live_kernel_reports"]:
        latest_scan = data["live_kernel_reports"][-1]
        if latest_scan.get("files"):
            hints.append("Recent LiveKernelReports or minidumps were found. Share filenames/timestamps with anyone helping debug.")
        if latest_scan.get("errors"):
            hints.append("LiveKernelReports scan had access-denied or missing-folder entries. Run as admin only if you need dump filename visibility.")
    if data["display_events"]:
        topology = (data["display_events"][-1].get("topology") or {})
        if topology.get("below_native_refresh"):
            native = topology.get("native_mode") or {}
            hints.append(
                f"Display is running below EDID native refresh: current mode is lower than {native.get('refresh_hz')} Hz."
            )
        output = topology.get("current_output_technology")
        if output:
            hints.append(f"Display output technology detected: {output}.")
    if data["diagnostics"]:
        latest_summary = next((item for item in reversed(data["diagnostics"]) if item.get("type") == "session_stream_summary"), None)
        for warning in (latest_summary or {}).get("warnings") or []:
            hints.append(f"Recorder stream warning: {warning}.")
    if not hints:
        hints.append("No single strong signal was detected. Compare heartbeat, markers, GPU failures, and Windows events with the exact freeze time.")
    return hints


def generate_report(session_dir: Path) -> Path:
    data = load_session(session_dir)
    info = data["session_info"]
    end = _session_end(
        info,
        data["heartbeat"],
        data["gpu_samples"],
        data["gpu_probe_failures"],
        data["events"],
        data["power_events"],
        data["processes"],
        data["markers"],
        data["diagnostics"],
    )
    recent_gpu = _near_end(data["gpu_samples"], end, 5)
    recent_failures = _near_end(data["gpu_probe_failures"], end, 10)
    recent_events = _near_end(data["events"], end, 10)
    recent_power_events = _near_end(data["power_events"], end, 10)
    recent_processes = _near_end(data["processes"], end, 5)
    recent_diagnostics = _near_end(data["diagnostics"], end, 10)
    summary = summarize_session(session_dir)
    hints = suspicion_hints(session_dir)
    classifications = freeze_classification(session_dir)

    lines = [
        "# GPU Flight Recorder Report",
        "",
        f"- Session folder: `{_redact_path(session_dir)}`",
        f"- Started: {summary.get('started_at') or 'unknown'}",
        f"- Ended/last heartbeat: {summary.get('ended_at') or 'unknown'}",
        f"- Clean exit: {'yes' if summary.get('clean_exit') else 'no'}",
        f"- Heartbeats: {summary['heartbeat_count']} total, {summary['recent_heartbeat_count']} in final 10 minutes",
        f"- GPU samples: {summary['gpu_sample_count']} total, {summary['recent_gpu_sample_count']} in final 10 minutes",
        f"- GPU probe failures: {summary['gpu_probe_failure_count']} total, {summary['recent_gpu_probe_failure_count']} in final 10 minutes",
        f"- Events: {summary['event_count']} total, {summary['recent_event_count']} in final 10 minutes",
        f"- Power events: {summary['power_event_count']} total, {summary['recent_power_event_count']} in final 10 minutes",
        f"- Markers: {summary['marker_count']}",
        "",
        "## Freeze Classification",
        "",
    ]
    lines.extend(f"- {item}" for item in classifications)

    lines.extend(
        [
            "",
            "## Final Five Minutes",
            "",
            f"- Max GPU temperature: {summary.get('max_gpu_temp_c')} C",
            f"- Max GPU power: {summary.get('max_gpu_power_w')} W",
            f"- Max GPU memory used: {summary.get('max_gpu_memory_used_mib')} MiB",
            f"- Max GPU utilization: {summary.get('max_gpu_util_percent')}%",
            f"- nvidia-smi failures/timeouts: {summary.get('nvidia_smi_failures')}",
            "",
            "## Suspicion Hints",
            "",
        ]
    )
    lines.extend(f"- {hint}" for hint in hints)

    lines.extend(["", "## Display Topology", ""])
    if data["display_events"]:
        latest_display = data["display_events"][-1]
        mode = latest_display.get("active_mode") or {}
        topology = latest_display.get("topology") or {}
        native = topology.get("native_mode") or {}
        lines.append(f"- Current mode: {mode.get('width')}x{mode.get('height')}@{mode.get('refresh_hz')} Hz, {mode.get('bits_per_pixel')} bpp")
        lines.append(f"- Output technology: {topology.get('current_output_technology') or 'unknown'}")
        lines.append(f"- Monitor: {topology.get('current_monitor_friendly_name') or 'unknown'}")
        if native:
            lines.append(f"- EDID native mode: {native.get('width')}x{native.get('height')}@{native.get('refresh_hz')} Hz")
            lines.append(f"- Below native refresh: {topology.get('below_native_refresh')}")
        else:
            lines.append("- EDID native mode: unavailable")
        for error in (topology.get("errors") or [])[:5]:
            lines.append(f"- Topology note: {short_text(error, 160)}")
    else:
        lines.append("No display topology records were captured.")

    lines.extend(["", "## Power Events", ""])
    if recent_power_events:
        lines.append("| Time | Transition | Source | Message |")
        lines.append("| --- | --- | --- | --- |")
        for event in recent_power_events[-20:]:
            source = event.get("source_event") or {}
            lines.append(
                "| {time} | {transition} | {source} {event_id} | {message} |".format(
                    time=event.get("timestamp"),
                    transition=event.get("transition"),
                    source=source.get("provider_name"),
                    event_id=source.get("event_id"),
                    message=short_text(event.get("message"), 180).replace("|", "\\|"),
                )
            )
    else:
        lines.append("No sleep/resume/wake events were captured near the end.")

    lines.extend(["", "## Recent Diagnostics", ""])
    if recent_diagnostics:
        for record in recent_diagnostics[-20:]:
            lines.append(f"- {record.get('timestamp')}: {record.get('type')} - {record.get('message')}")
    else:
        lines.append("No internal diagnostics were captured near the end.")

    lines.extend(["", "## Collector Health", ""])
    stream_counts = summary.get("stream_counts") or {}
    stream_warnings = summary.get("stream_warnings") or []
    if stream_counts:
        lines.append(f"Count source: {summary.get('stream_count_source')}")
        lines.append("")
        lines.append("| Stream | Records |")
        lines.append("| --- | ---: |")
        for name, count in sorted(stream_counts.items()):
            lines.append(f"| {name} | {count} |")
        if stream_warnings:
            lines.append("")
            lines.append("Warnings:")
            for warning in stream_warnings:
                lines.append(f"- {warning}")
    else:
        lines.append("No collector health summary was captured.")

    lines.extend(["", "## Event Query Health", ""])
    latest_event_health = _latest_diagnostic(data, "event_query_health")
    if latest_event_health:
        lines.append(f"Health source: {latest_event_health.get('health_source') or 'unknown'}")
        lines.append("")
        critical = latest_event_health.get("critical_queries") or []
        optional_errors = latest_event_health.get("optional_errors") or []
        if critical:
            lines.append("| Query | Log | Provider | Events | Captured | Status |")
            lines.append("| --- | --- | --- | ---: | ---: | --- |")
            for item in critical:
                status = "ok" if item.get("success") else f"error: {short_text(item.get('message'), 80)}"
                lines.append(
                    f"| {item.get('query_name')} | {item.get('log_name')} | {item.get('provider_name') or ''} | {item.get('event_count')} | {item.get('captured_event_count')} | {status} |"
                )
        if optional_errors:
            lines.append("")
            lines.append("Optional channel notes:")
            for item in optional_errors[:10]:
                lines.append(f"- {item.get('query_name')}: {short_text(item.get('message'), 160)}")
    else:
        lines.append("No event query health diagnostic was captured.")

    lines.extend(["", "## GPU Probe Failures", ""])
    if recent_failures:
        lines.append("| Time | Type | Timeout | Elapsed ms | Error |")
        lines.append("| --- | --- | ---: | ---: | --- |")
        for failure in recent_failures[-20:]:
            error_text = short_text(failure.get("stderr"), 160).replace("|", "\\|")
            lines.append(
                f"| {failure.get('timestamp')} | {failure.get('error_type')} | {failure.get('timeout_sec')} | {failure.get('elapsed_ms')} | {error_text} |"
            )
    else:
        lines.append("No GPU probe failures were captured near the end.")

    lines.extend(["", "## Target Processes Near End", ""])
    last_processes = recent_processes[-1].get("processes", []) if recent_processes else []
    if last_processes:
        lines.append("| Process | PID | CPU % | RAM MB | Responding |")
        lines.append("| --- | ---: | ---: | ---: | --- |")
        for process in last_processes:
            ram = process.get("working_set_bytes")
            ram_mb = round(float(ram) / 1024 / 1024, 1) if ram else ""
            lines.append(
                f"| {process.get('name')} | {process.get('pid')} | {process.get('cpu_percent') or ''} | {ram_mb} | {process.get('responding')} |"
            )
    else:
        lines.append("No target process snapshot was captured near the end.")

    lines.extend(["", "## Recent Windows Events", ""])
    if recent_events:
        lines.append("| Time | Log | Provider | ID | Reason | Message |")
        lines.append("| --- | --- | --- | ---: | --- | --- |")
        for event in recent_events[-25:]:
            lines.append(
                "| {time} | {log} | {provider} | {event_id} | {reason} | {message} |".format(
                    time=event.get("TimeCreated") or event.get("timestamp"),
                    log=event.get("LogName"),
                    provider=event.get("ProviderName"),
                    event_id=event.get("Id"),
                    reason=event.get("capture_reason", ""),
                    message=short_text(event.get("Message"), 180).replace("|", "\\|"),
                )
            )
    else:
        lines.append("No relevant warning/error/critical events were captured near the end.")

    lines.extend(["", "## LiveKernelReports / Minidump Scan", ""])
    if data["live_kernel_reports"]:
        latest_scan = data["live_kernel_reports"][-1]
        files = latest_scan.get("files") or []
        errors = latest_scan.get("errors") or []
        if files:
            lines.append("| Folder | Source | Filename | Modified | Size |")
            lines.append("| --- | --- | --- | --- | ---: |")
            for item in files[:30]:
                lines.append(
                    f"| {item.get('folder')} | {item.get('path_source', '')} | {item.get('filename')} | {item.get('modified_time')} | {item.get('size_bytes')} |"
                )
        if errors:
            lines.append("")
            lines.append("Scan notes:")
            for error in errors[:20]:
                source = f" ({error.get('path_source')})" if error.get("path_source") else ""
                lines.append(f"- {error.get('folder')}{source}: {error.get('error_type')} - {short_text(error.get('message'), 160)}")
        if not files and not errors:
            lines.append("No recent dump filenames or scan errors were recorded.")
    else:
        lines.append("No LiveKernelReports scan was recorded.")

    lines.extend(["", "## Markers", ""])
    if data["markers"]:
        for marker in data["markers"]:
            lines.append(f"- {marker.get('timestamp')}: {marker.get('note')} ({marker.get('source', 'unknown')})")
    else:
        lines.append("No manual markers were added.")

    lines.extend(["", "## Last GPU Samples", ""])
    if recent_gpu:
        lines.append("| Time | GPU % | Temp C | Power W | VRAM MiB | Display Active | nvidia-smi |")
        lines.append("| --- | ---: | ---: | ---: | ---: | --- | --- |")
        for sample in recent_gpu[-20:]:
            gpu = _gpu_payload(sample).get("primary_gpu") or {}
            lines.append(
                f"| {sample.get('timestamp')} | {gpu.get('utilization_gpu', '')} | {gpu.get('temperature_gpu', '')} | {gpu.get('power_draw') or gpu.get('power_draw_average') or ''} | {gpu.get('memory_used', '')} | {gpu.get('display_active', '')} | {_gpu_payload(sample).get('responded')} |"
            )
    else:
        lines.append("No successful GPU samples were captured.")

    report_path = session_dir / "final_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
