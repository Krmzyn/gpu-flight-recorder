from __future__ import annotations

import json
import platform
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .collectors import (
    SCHEMA_VERSION,
    CpuTracker,
    ProcessCpuState,
    collect_display,
    collect_events_since,
    collect_heartbeat,
    collect_keyboard_state,
    collect_live_kernel_reports,
    collect_nvidia_gpu,
    collect_target_processes,
    is_admin,
)
from .config import Config
from .exporter import export_support_bundle
from .io_utils import JsonlWriter, atomic_write_json, now_iso, parse_iso, read_jsonl, short_text
from .marker import append_marker, clear_active_session, write_active_session
from .report import generate_report


HEARTBEAT_GAP_SECONDS = 3.0
GPU_STALL_SECONDS = 5.0
SYSTEM_STALL_ALIGNMENT_SECONDS = 5.0


class FlightRecorder:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session_dir: Path | None = None
        self._writers: dict[str, JsonlWriter] = {}
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._process_state = ProcessCpuState()
        self._event_seen: set[str] = set()
        self._optional_event_error_seen: set[str] = set()
        self._timeline: deque[dict[str, Any]] = deque(maxlen=300)
        self._status_message = "Idle"
        self._session_start_monotonic = time.monotonic()

        self._latest_heartbeat: dict[str, Any] | None = None
        self._latest_gpu_sample: dict[str, Any] | None = None
        self._latest_gpu_failure: dict[str, Any] | None = None
        self._latest_keyboard_state: dict[str, Any] | None = None
        self._latest_display_state: dict[str, Any] | None = None
        self._latest_processes: dict[str, Any] | None = None
        self._latest_events: list[dict[str, Any]] = []
        self._latest_power_events: list[dict[str, Any]] = []
        self._latest_event_query_health: list[dict[str, Any]] = []
        self._startup_event_query_health: list[dict[str, Any]] = []
        self._latest_live_kernel_scan: dict[str, Any] | None = None
        self._collector_health: dict[str, dict[str, Any]] = {}
        self._stream_counts: dict[str, int] = {}

        self._last_heartbeat_monotonic: float | None = None
        self._last_gpu_success_monotonic: float | None = None
        self._last_gpu_stall_diag_monotonic = 0.0
        self._last_session_info_update_monotonic = 0.0
        self._last_collector_health_write_monotonic = 0.0
        self._gpu_failure_count = 0
        self._keyboard_change_count = 0
        self._display_change_count = 0

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def recover_unclean_sessions(self) -> list[Path]:
        recovered: list[Path] = []
        log_dir = self.config.log_path
        if not log_dir.exists():
            return recovered
        for session_dir in sorted(log_dir.glob("session-*")):
            info_path = session_dir / "session_info.json"
            if not info_path.exists():
                continue
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if info.get("status") == "closed":
                continue
            self._append_recovery_diagnostics(session_dir)
            self._append_live_kernel_scan(session_dir, "recovery_startup")
            if info.get("status") != "recovered_unclean":
                info["status"] = "recovered_unclean"
                info["recovered_at"] = now_iso()
                info["unclean_reason"] = "Recorder found this session still marked running on startup."
                atomic_write_json(info_path, info)
            if self.config.generate_reports_automatically:
                generate_report(session_dir)
            recovered.append(session_dir)
        return recovered

    def start(self) -> Path:
        with self._lock:
            if self.running:
                if self.session_dir is None:
                    raise RuntimeError("Recorder is running without a session directory")
                return self.session_dir
            self.config.log_path.mkdir(parents=True, exist_ok=True)
            self.session_dir = self._new_session_dir()
            self.session_dir.mkdir(parents=True, exist_ok=False)
            self._reset_runtime_state()
            self._open_writers()
            self._write_session_info("running")
            write_active_session(self.config, self.session_dir)
            if not is_admin():
                self._write_diagnostic(
                    "not_elevated",
                    "Recorder is not running as administrator; LiveKernelReports, minidumps, Security, and some operational event channels may be unavailable.",
                    severity="warning",
                )
            self._append_live_kernel_scan(self.session_dir, "startup")
            self._start_workers()
            self._status_message = f"Recording to {self.session_dir}"
            return self.session_dir

    def stop(self) -> Path | None:
        self._stop_event.set()
        for thread in list(self._threads):
            thread.join(timeout=20.0)
        with self._lock:
            if self.session_dir is None:
                return None
            still_alive = [thread.name for thread in self._threads if thread.is_alive()]
            if still_alive:
                self._write_diagnostic(
                    "worker_stop_timeout",
                    "One or more workers did not stop before the timeout.",
                    workers=still_alive,
                )
            self._append_live_kernel_scan(self.session_dir, "stop")
            self._write_collector_health(force=True)
            self._write_session_stream_summary()
            self._write_event_query_health("stop")
            self._write_session_info("closed", end_time=now_iso())
            clear_active_session(self.config, self.session_dir)
            self._close_writers()
            report_path = generate_report(self.session_dir)
            self._status_message = f"Stopped. Report: {report_path.name}"
            self._threads = []
            return report_path

    def add_marker(self, note: str) -> bool:
        note = note.strip()
        if not note:
            return False
        with self._lock:
            if not self.session_dir or "markers" not in self._writers:
                self._status_message = "Start recording before adding markers"
                return False
            record = {"timestamp": now_iso(), "note": note, "source": "gui"}
            self._write("markers", record)
            self._timeline.append({"timestamp": record["timestamp"], "type": "marker", "detail": note})
            self._status_message = f"Marker added: {note}"
            return True

    def generate_report(self) -> Path | None:
        with self._lock:
            if self.session_dir is None:
                return None
            self._append_live_kernel_scan(self.session_dir, "report_generation")
            self._write_collector_health(force=True)
            self._write_session_stream_summary()
            self._write_event_query_health("report_generation")
            self._write_session_info("running" if self.running else "closed")
            report_path = generate_report(self.session_dir)
            self._status_message = f"Report generated: {report_path}"
            return report_path

    def export_support_bundle(self, include_raw: bool = False) -> Path | None:
        with self._lock:
            if self.session_dir is None:
                return None
            self._append_live_kernel_scan(self.session_dir, "export")
            self._write_collector_health(force=True)
            self._write_session_stream_summary()
            self._write_event_query_health("export")
            bundle = export_support_bundle(self.session_dir, self.config, include_raw=include_raw)
            self._status_message = f"Support bundle exported: {bundle.name}"
            return bundle

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latest_sample = None
            if self._latest_heartbeat or self._latest_gpu_sample or self._latest_gpu_failure:
                latest_sample = {
                    "timestamp": (self._latest_gpu_sample or self._latest_gpu_failure or self._latest_heartbeat or {}).get("timestamp"),
                    "system": {
                        "foreground_process": (self._latest_heartbeat or {}).get("foreground_process"),
                        "display": self._latest_display_state,
                    },
                    "nvidia": self._latest_gpu_sample or self._latest_gpu_failure,
                }
            return {
                "running": self.running,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "latest_sample": latest_sample,
                "latest_heartbeat": self._latest_heartbeat,
                "latest_gpu_sample": self._latest_gpu_sample,
                "latest_gpu_failure": self._latest_gpu_failure,
                "latest_keyboard_state": self._latest_keyboard_state,
                "latest_display_state": self._latest_display_state,
                "latest_processes": self._latest_processes,
                "latest_events": list(self._latest_events[-10:]),
                "latest_power_events": list(self._latest_power_events[-10:]),
                "latest_event_query_health": list(self._latest_event_query_health),
                "latest_live_kernel_scan": self._latest_live_kernel_scan,
                "collector_health": self._collector_health.copy(),
                "stream_counts": self._stream_counts.copy(),
                "gpu_failure_count": self._gpu_failure_count,
                "keyboard_change_count": self._keyboard_change_count,
                "display_change_count": self._display_change_count,
                "timeline": list(self._timeline),
                "status_message": self._status_message,
            }

    def _reset_runtime_state(self) -> None:
        self._stop_event.clear()
        self._event_seen.clear()
        self._optional_event_error_seen.clear()
        self._threads = []
        self._timeline.clear()
        self._status_message = "Starting"
        self._session_start_monotonic = time.monotonic()
        self._latest_heartbeat = None
        self._latest_gpu_sample = None
        self._latest_gpu_failure = None
        self._latest_keyboard_state = None
        self._latest_display_state = None
        self._latest_processes = None
        self._latest_events = []
        self._latest_power_events = []
        self._latest_event_query_health = []
        self._startup_event_query_health = []
        self._latest_live_kernel_scan = None
        self._collector_health = {}
        self._stream_counts = {}
        self._last_heartbeat_monotonic = None
        self._last_gpu_success_monotonic = None
        self._last_gpu_stall_diag_monotonic = 0.0
        self._last_session_info_update_monotonic = 0.0
        self._last_collector_health_write_monotonic = 0.0
        self._gpu_failure_count = 0
        self._keyboard_change_count = 0
        self._display_change_count = 0
        self._process_state = ProcessCpuState()

    def _new_session_dir(self) -> Path:
        base_name = "session-" + time.strftime("%Y%m%d-%H%M%S")
        candidate = self.config.log_path / base_name
        index = 1
        while candidate.exists():
            candidate = self.config.log_path / f"{base_name}-{index}"
            index += 1
        return candidate

    def _open_writers(self) -> None:
        if self.session_dir is None:
            raise RuntimeError("No session directory")
        self._writers = {
            "heartbeat": JsonlWriter(self.session_dir / "heartbeat.jsonl", self.config.fsync_each_write),
            "gpu_samples": JsonlWriter(self.session_dir / "gpu_samples.jsonl", self.config.fsync_each_write),
            "gpu_probe_failures": JsonlWriter(self.session_dir / "gpu_probe_failures.jsonl", self.config.fsync_each_write),
            "keyboard_state": JsonlWriter(self.session_dir / "keyboard_state.jsonl", self.config.fsync_each_write),
            "display_events": JsonlWriter(self.session_dir / "display_events.jsonl", self.config.fsync_each_write),
            "diagnostics": JsonlWriter(self.session_dir / "diagnostics.jsonl", self.config.fsync_each_write),
            "live_kernel_reports": JsonlWriter(self.session_dir / "live_kernel_reports.jsonl", self.config.fsync_each_write),
            "events": JsonlWriter(self.session_dir / "events.jsonl", self.config.fsync_each_write),
            "power_events": JsonlWriter(self.session_dir / "power_events.jsonl", self.config.fsync_each_write),
            "processes": JsonlWriter(self.session_dir / "processes.jsonl", self.config.fsync_each_write),
            "markers": JsonlWriter(self.session_dir / "markers.jsonl", self.config.fsync_each_write),
        }
        self._stream_counts = {name: 0 for name in self._writers}

    def _close_writers(self) -> None:
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    def _write(self, writer_name: str, record: dict[str, Any]) -> None:
        writer = self._writers.get(writer_name)
        if writer:
            record.setdefault("schema_version", SCHEMA_VERSION)
            record.setdefault("captured_at", now_iso())
            record.setdefault("monotonic_seconds", round(time.monotonic(), 6))
            writer.write(record)
            self._stream_counts[writer_name] = self._stream_counts.get(writer_name, 0) + 1

    def _write_diagnostic(self, kind: str, message: str, **extra: Any) -> None:
        record = {"timestamp": now_iso(), "type": kind, "message": message}
        record.update(extra)
        self._write("diagnostics", record)
        self._timeline.append({"timestamp": record["timestamp"], "type": "diagnostic", "detail": f"{kind}: {message}"})

    def _collector_record(self, name: str) -> dict[str, Any]:
        return self._collector_health.setdefault(
            name,
            {
                "success_count": 0,
                "error_count": 0,
                "last_success": None,
                "last_error": None,
                "last_error_at": None,
                "last_record_count": 0,
            },
        )

    def _mark_collector_success(self, name: str, record_count: int = 0) -> None:
        health = self._collector_record(name)
        health["success_count"] = int(health.get("success_count") or 0) + 1
        health["last_success"] = now_iso()
        health["last_record_count"] = record_count

    def _mark_collector_error(self, name: str, message: str, **extra: Any) -> None:
        health = self._collector_record(name)
        health["error_count"] = int(health.get("error_count") or 0) + 1
        health["last_error"] = short_text(message, 500)
        health["last_error_at"] = now_iso()
        if extra:
            health["last_error_detail"] = extra

    def _write_collector_health(self, force: bool = False) -> None:
        interval = max(5.0, float(self.config.collector_health_interval_sec))
        current = time.monotonic()
        if not force and current - self._last_collector_health_write_monotonic < interval:
            return
        self._last_collector_health_write_monotonic = current
        self._write_diagnostic(
            "collector_health",
            "Collector health snapshot.",
            collectors=self._collector_health.copy(),
            stream_counts=self._stream_counts.copy(),
        )

    def _write_session_stream_summary(self) -> None:
        counts = self._stream_counts.copy()
        warnings = []
        for stream_name in ("heartbeat", "display_events", "events", "power_events", "live_kernel_reports"):
            if counts.get(stream_name, 0) == 0:
                warnings.append(f"{stream_name} produced zero records")
        if counts.get("events", 0) == 0:
            warnings.append("Windows events stream is empty; inspect event_channel_error diagnostics and permissions.")
        self._write_diagnostic(
            "session_stream_summary",
            "Session stream record counts.",
            stream_counts=counts,
            warnings=warnings,
            collectors=self._collector_health.copy(),
        )

    def _write_event_query_health(self, reason: str) -> None:
        health_source = "startup_scan" if self._startup_event_query_health else "latest_poll"
        health_items = self._startup_event_query_health or self._latest_event_query_health
        if not health_items:
            return
        critical = []
        optional_errors = []
        for item in health_items:
            severity = str(item.get("Severity") or item.get("severity") or "critical").lower()
            success = bool(item.get("Success", item.get("success", False)))
            event_count = int(item.get("EventCount") or item.get("event_count") or 0)
            captured_count = int(item.get("CapturedEventCount") or item.get("captured_event_count") or 0)
            summary = {
                "query_name": item.get("QueryName"),
                "log_name": item.get("LogName"),
                "provider_name": item.get("ProviderName"),
                "ids": item.get("Ids"),
                "levels": item.get("Levels"),
                "severity": severity,
                "success": success,
                "event_count": event_count,
                "captured_event_count": captured_count,
                "error_type": item.get("ErrorType"),
                "message": short_text(item.get("Message"), 500),
            }
            if severity == "critical":
                critical.append(summary)
            elif not success:
                optional_errors.append(summary)
        self._write_diagnostic(
            "event_query_health",
            "Windows Event Log query health snapshot.",
            reason=reason,
            health_source=health_source,
            critical_queries=critical,
            optional_errors=optional_errors,
            query_health=list(health_items),
            latest_query_health=list(self._latest_event_query_health),
        )

    @staticmethod
    def _event_error_key(error: dict[str, Any]) -> str:
        return "|".join(
            str(error.get(part) or "")
            for part in ("QueryName", "LogName", "ProviderName", "ErrorType", "Message")
        )

    def _write_session_info(self, status: str, **extra: Any) -> None:
        if self.session_dir is None:
            return
        info_path = self.session_dir / "session_info.json"
        info: dict[str, Any] = {}
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except Exception:
                info = {}
        info.setdefault("start_time", now_iso())
        info.update(
            {
                "status": status,
                "last_heartbeat": now_iso(),
                "admin": is_admin(),
                "python": sys.version,
                "platform": platform.platform(),
                "machine": platform.node(),
                "nvidia_smi": self.config.nvidia_smi_path or shutil.which("nvidia-smi"),
                "config": self.config.redacted_dict(),
                "stream_counts": self._stream_counts.copy(),
                "display_topology": (self._latest_display_state or {}).get("topology"),
            }
        )
        info.update(extra)
        atomic_write_json(info_path, info)

    def _start_workers(self) -> None:
        workers: list[tuple[str, Callable[[], None]]] = [
            ("HeartbeatWorker", self._heartbeat_worker),
            ("GpuProbeWorker", self._gpu_probe_worker),
            ("KeyboardWorker", self._keyboard_worker),
            ("DisplayWorker", self._display_worker),
            ("ProcessWorker", self._process_worker),
            ("EventWorker", self._event_worker),
        ]
        for name, target in workers:
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def _heartbeat_worker(self) -> None:
        tracker = CpuTracker()
        interval = max(0.2, float(self.config.heartbeat_interval_sec))
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                record = collect_heartbeat(tracker)
                current_monotonic = float(record["monotonic_seconds"])
                with self._lock:
                    if self._last_heartbeat_monotonic is not None:
                        gap = current_monotonic - self._last_heartbeat_monotonic
                        if gap > HEARTBEAT_GAP_SECONDS:
                            self._write_diagnostic(
                                "logger_gap",
                                "Heartbeat gap exceeded 3 seconds.",
                                gap_seconds=round(gap, 3),
                                previous_monotonic=self._last_heartbeat_monotonic,
                                current_monotonic=current_monotonic,
                            )
                    self._write("heartbeat", record)
                    self._latest_heartbeat = record
                    self._last_heartbeat_monotonic = current_monotonic
                    self._mark_collector_success("heartbeat", 1)
                    self._write_collector_health()
                    if current_monotonic - self._last_session_info_update_monotonic > 5.0:
                        self._last_session_info_update_monotonic = current_monotonic
                        self._write_session_info("running", last_heartbeat=record["timestamp"])
            except Exception as exc:
                with self._lock:
                    self._mark_collector_error("heartbeat", str(exc))
                    self._write_diagnostic("heartbeat_error", "Heartbeat worker failed to capture a tick.", error=str(exc))
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _gpu_probe_worker(self) -> None:
        interval = max(0.5, float(self.config.gpu_probe_interval_sec))
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                record = collect_nvidia_gpu(self.config)
                record["monotonic_seconds"] = round(time.monotonic(), 6)
                with self._lock:
                    if record.get("responded"):
                        self._write("gpu_samples", record)
                        self._latest_gpu_sample = record
                        self._last_gpu_success_monotonic = float(record["monotonic_seconds"])
                        self._mark_collector_success("gpu_probe", 1)
                        gpu = record.get("primary_gpu") or {}
                        self._timeline.append(
                            {
                                "timestamp": record["timestamp"],
                                "type": "gpu",
                                "detail": f"GPU {gpu.get('utilization_gpu', '?')}%, {gpu.get('temperature_gpu', '?')} C, nvidia-smi ok",
                            }
                        )
                    else:
                        self._write("gpu_probe_failures", record)
                        self._latest_gpu_failure = record
                        self._gpu_failure_count += 1
                        self._mark_collector_error("gpu_probe", record.get("stderr") or record.get("error_type") or "GPU probe failed", error_type=record.get("error_type"))
                        self._timeline.append(
                            {
                                "timestamp": record["timestamp"],
                                "type": "gpu_failure",
                                "detail": f"{record.get('error_type') or 'failure'}: {short_text(record.get('stderr'), 140)}",
                            }
                        )
                    self._maybe_write_gpu_stall_diag(float(record["monotonic_seconds"]))
            except Exception as exc:
                with self._lock:
                    record = {
                        "timestamp": now_iso(),
                        "monotonic_seconds": round(time.monotonic(), 6),
                        "responded": False,
                        "error_type": "worker_exception",
                        "stderr": str(exc),
                    }
                    self._write("gpu_probe_failures", record)
                    self._latest_gpu_failure = record
                    self._gpu_failure_count += 1
                    self._mark_collector_error("gpu_probe", str(exc), error_type="worker_exception")
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _maybe_write_gpu_stall_diag(self, current_monotonic: float) -> None:
        last_success = self._last_gpu_success_monotonic or self._session_start_monotonic
        heartbeat_recent = (
            self._last_heartbeat_monotonic is not None
            and current_monotonic - self._last_heartbeat_monotonic <= HEARTBEAT_GAP_SECONDS
        )
        if not heartbeat_recent:
            return
        gap = current_monotonic - last_success
        if gap > GPU_STALL_SECONDS and current_monotonic - self._last_gpu_stall_diag_monotonic > GPU_STALL_SECONDS:
            self._last_gpu_stall_diag_monotonic = current_monotonic
            self._write_diagnostic(
                "gpu_probe_stalled",
                "GPU probe has not produced a successful sample for more than 5 seconds while heartbeat continues.",
                gap_seconds=round(gap, 3),
            )

    def _keyboard_worker(self) -> None:
        interval = max(0.2, float(self.config.keyboard_interval_sec))
        previous_state: dict[str, Any] | None = None
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                record = collect_keyboard_state()
                comparable = {key: value for key, value in record.items() if key != "timestamp"}
                if previous_state is None or comparable != previous_state:
                    record["change_type"] = "initial" if previous_state is None else "changed"
                    with self._lock:
                        self._write("keyboard_state", record)
                        self._latest_keyboard_state = record
                        self._keyboard_change_count += 1
                        self._mark_collector_success("keyboard", 1)
                        self._timeline.append(
                            {
                                "timestamp": record["timestamp"],
                                "type": "keyboard",
                                "detail": self._keyboard_detail(record),
                            }
                        )
                    previous_state = comparable
            except Exception as exc:
                with self._lock:
                    self._mark_collector_error("keyboard", str(exc))
                    self._write_diagnostic("keyboard_error", "Keyboard worker failed.", error=str(exc))
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _display_worker(self) -> None:
        interval = max(0.5, float(self.config.display_interval_sec))
        previous_state_json: str | None = None
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                record = collect_display()
                comparable = json.dumps({key: value for key, value in record.items() if key != "timestamp"}, sort_keys=True)
                if previous_state_json is None or comparable != previous_state_json:
                    record["change_type"] = "initial" if previous_state_json is None else "changed"
                    with self._lock:
                        self._write("display_events", record)
                        self._latest_display_state = record
                        self._display_change_count += 1
                        self._mark_collector_success("display", 1)
                        self._timeline.append(
                            {
                                "timestamp": record["timestamp"],
                                "type": "display",
                                "detail": self._display_detail(record),
                            }
                        )
                    previous_state_json = comparable
            except Exception as exc:
                with self._lock:
                    self._mark_collector_error("display", str(exc))
                    self._write_diagnostic("display_error", "Display worker failed.", error=str(exc))
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _process_worker(self) -> None:
        interval = max(2.0, float(self.config.process_interval_sec))
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                record = collect_target_processes(self.config, self._process_state)
                with self._lock:
                    self._write("processes", record)
                    self._latest_processes = record
                    self._mark_collector_success("processes", len(record.get("processes", [])))
                    names = ", ".join(process["name"] for process in record.get("processes", [])[:8])
                    self._timeline.append(
                        {
                            "timestamp": record["timestamp"],
                            "type": "processes",
                            "detail": names or "No target processes running",
                        }
                    )
            except Exception as exc:
                with self._lock:
                    self._mark_collector_error("processes", str(exc))
                    self._write_diagnostic("process_error", "Process worker failed.", error=str(exc))
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _event_worker(self) -> None:
        scan_days = max(0, int(getattr(self.config, "startup_event_history_days", 0)))
        if scan_days:
            since = (datetime.now().astimezone() - timedelta(days=scan_days)).isoformat(timespec="milliseconds")
        else:
            scan_minutes = max(0, int(self.config.startup_event_scan_minutes))
            since = (datetime.now().astimezone() - timedelta(minutes=scan_minutes)).isoformat(timespec="milliseconds")
        first = True
        interval = max(5.0, float(self.config.event_interval_sec))
        while not self._stop_event.is_set():
            started = time.monotonic()
            capture_reason = "startup_scan" if first else "poll"
            first = False
            try:
                record = collect_events_since(self.config, since, self._event_seen, capture_reason=capture_reason)
                since = now_iso()
                events = record.get("events", [])
                power_events = record.get("power_events", [])
                errors = record.get("errors", [])
                query_health = record.get("query_health", [])
                with self._lock:
                    if query_health:
                        self._latest_event_query_health = list(query_health)
                        if capture_reason == "startup_scan":
                            self._startup_event_query_health = list(query_health)
                    for error in errors:
                        if isinstance(error, dict):
                            message = error.get("Message") or error.get("message") or "Event channel query failed."
                            detail = dict(error)
                        else:
                            message = str(error)
                            detail = {"error": message}
                        severity = str(detail.get("Severity") or detail.get("severity") or "critical").lower()
                        if severity == "optional":
                            key = self._event_error_key(detail)
                            if key in self._optional_event_error_seen:
                                continue
                            self._optional_event_error_seen.add(key)
                        self._mark_collector_error("events", str(message), **detail)
                        self._write_diagnostic(
                            "event_channel_error",
                            "A Windows Event Log query failed but other channels will continue.",
                            **detail,
                        )
                    for event in events:
                        self._write("events", event)
                        detail = f"{event.get('ProviderName')} {event.get('Id')}: {short_text(event.get('Message'), 140)}"
                        self._timeline.append(
                            {
                                "timestamp": event.get("TimeCreated") or record["timestamp"],
                                "type": "event",
                                "detail": detail,
                            }
                        )
                    for power_event in power_events:
                        self._write("power_events", power_event)
                        self._latest_power_events.append(power_event)
                        self._latest_power_events = self._latest_power_events[-50:]
                        self._timeline.append(
                            {
                                "timestamp": power_event.get("timestamp") or record["timestamp"],
                                "type": "power",
                                "detail": f"{power_event.get('transition')} from {power_event.get('source_event', {}).get('provider_name')} {power_event.get('source_event', {}).get('event_id')}",
                            }
                        )
                    self._latest_events.extend(events)
                    self._latest_events = self._latest_events[-50:]
                    self._mark_collector_success("events", len(events))
                    if capture_reason == "startup_scan" and query_health:
                        self._write_event_query_health("startup_scan")
                    if events:
                        self._status_message = f"Captured {len(events)} relevant event(s)"
            except Exception as exc:
                with self._lock:
                    self._mark_collector_error("events", str(exc), error_type="worker_exception")
                    self._write_diagnostic("event_error", "Event worker failed.", error=str(exc))
            self._stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _append_live_kernel_scan(self, session_dir: Path, reason: str) -> None:
        record = collect_live_kernel_reports(self.config)
        record["capture_reason"] = reason
        if self.session_dir == session_dir and "live_kernel_reports" in self._writers:
            self._write("live_kernel_reports", record)
        else:
            writer = JsonlWriter(session_dir / "live_kernel_reports.jsonl", self.config.fsync_each_write)
            try:
                record.setdefault("schema_version", SCHEMA_VERSION)
                record.setdefault("captured_at", now_iso())
                record.setdefault("monotonic_seconds", round(time.monotonic(), 6))
                writer.write(record)
            finally:
                writer.close()
        self._latest_live_kernel_scan = record
        if record.get("errors"):
            self._mark_collector_error("live_kernel_reports", f"{len(record.get('errors', []))} scan note(s)", errors=record.get("errors"))
        else:
            self._mark_collector_success("live_kernel_reports", len(record.get("files", [])))

    def _append_recovery_diagnostics(self, session_dir: Path) -> None:
        heartbeats = read_jsonl(session_dir / "heartbeat.jsonl")
        gpu_samples = read_jsonl(session_dir / "gpu_samples.jsonl")
        if not heartbeats or not gpu_samples:
            return
        last_heartbeat = parse_iso(heartbeats[-1].get("timestamp"))
        last_gpu = parse_iso(gpu_samples[-1].get("timestamp"))
        if not last_heartbeat or not last_gpu:
            return
        delta = abs((last_heartbeat - last_gpu).total_seconds())
        if delta <= SYSTEM_STALL_ALIGNMENT_SECONDS:
            writer = JsonlWriter(session_dir / "diagnostics.jsonl", self.config.fsync_each_write)
            try:
                writer.write(
                    {
                        "timestamp": now_iso(),
                        "type": "system_or_process_stall_suspected",
                        "message": "Previous unclean session stopped heartbeat and GPU samples at nearly the same time.",
                        "last_heartbeat": heartbeats[-1].get("timestamp"),
                        "last_gpu_sample": gpu_samples[-1].get("timestamp"),
                        "delta_seconds": round(delta, 3),
                    }
                )
            finally:
                writer.close()

    @staticmethod
    def _keyboard_detail(record: dict[str, Any]) -> str:
        if not record.get("available", True):
            return f"unavailable: {record.get('error')}"
        parts = []
        for key in ("caps_lock", "num_lock", "scroll_lock"):
            state = record.get(key) or {}
            parts.append(f"{key}={'on' if state.get('toggled') else 'off'}")
        return ", ".join(parts)

    @staticmethod
    def _display_detail(record: dict[str, Any]) -> str:
        mode = record.get("active_mode") or {}
        if mode.get("available"):
            return f"{mode.get('width')}x{mode.get('height')}@{mode.get('refresh_hz')} Hz"
        return f"unavailable: {mode.get('error') or 'display mode not available'}"


def load_recent_report_text(session_dir: Path | None) -> str:
    if session_dir is None:
        return ""
    report_path = session_dir / "final_report.md"
    if not report_path.exists():
        return ""
    return report_path.read_text(encoding="utf-8")


def last_session_dir(log_dir: Path) -> Path | None:
    sessions = sorted(log_dir.glob("session-*")) if log_dir.exists() else []
    return sessions[-1] if sessions else None
