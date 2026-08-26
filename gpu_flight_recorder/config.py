from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_TARGET_PROCESSES = [
    "blender.exe",
    "fusion360.exe",
    "adskfusion360.exe",
    "fusionlauncher.exe",
    "claude.exe",
    "code.exe",
    "cursor.exe",
    "chrome.exe",
    "nvidia container.exe",
    "nvcontainer.exe",
    "dwm.exe",
    "explorer.exe",
]

DEFAULT_EVENT_KEYWORDS = [
    "Display",
    "nvlddmkm",
    "NVIDIA",
    "LiveKernelEvent",
    "WHEA",
    "WHEA-Logger",
    "Kernel-Power",
    "Desktop Window Manager",
    "dwm",
    "dwm.exe",
    "TDR",
    "WATCHDOG",
    "hardware error",
    "device reset",
    "display driver",
    "stopped responding",
    "recovered",
    "BugCheck",
]

DEFAULT_EVENT_LOGS = [
    "System",
    "Application",
    "Microsoft-Windows-WerKernel/Operational",
    "Microsoft-Windows-Kernel-WHEA/Errors",
    "Microsoft-Windows-Kernel-Power/Thermal-Operational",
    "Microsoft-Windows-Kernel-PnP/Driver Watchdog",
    "Microsoft-Windows-Dwm-Core/Operational",
    "Microsoft-Windows-DxgKrnl/Operational",
    "Microsoft-Windows-Diagnostics-Performance/Operational",
]

DEFAULT_LIVE_KERNEL_PATHS = [
    r"C:\Windows\LiveKernelReports",
    r"C:\Windows\LiveKernelReports\WATCHDOG",
    r"C:\Windows\Minidump",
]


def default_log_dir() -> str:
    return str(Path.home() / "Documents" / "GPUFlightRecorder" / "logs")


@dataclass
class Config:
    sample_interval_sec: float = 2.0
    heartbeat_interval_sec: float = 1.0
    keyboard_interval_sec: float = 0.5
    display_interval_sec: float = 1.0
    gpu_probe_interval_sec: float = 2.0
    process_interval_sec: float = 10.0
    event_interval_sec: float = 30.0
    log_dir: str = field(default_factory=default_log_dir)
    nvidia_smi_path: str | None = None
    target_processes: list[str] = field(default_factory=lambda: list(DEFAULT_TARGET_PROCESSES))
    event_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_EVENT_KEYWORDS))
    event_logs: list[str] = field(default_factory=lambda: list(DEFAULT_EVENT_LOGS))
    live_kernel_paths: list[str] = field(default_factory=lambda: list(DEFAULT_LIVE_KERNEL_PATHS))
    max_session_log_mb: int = 512
    nvidia_smi_timeout_sec: float = 1.0
    startup_event_scan_minutes: int = 30
    startup_event_history_days: int = 30
    collector_health_interval_sec: float = 60.0
    prompt_enable_optional_event_channels: bool = True
    admin_relaunch_prompt: bool = True
    freeze_classifier_warmup_seconds: float = 60.0
    freeze_classifier_strong_failure_ms: float = 5000.0
    freeze_classifier_minor_failure_ms: float = 1500.0
    freeze_classifier_cluster_window_sec: float = 30.0
    freeze_classifier_cluster_min_failures: int = 3
    freeze_classifier_cluster_total_ms: float = 5000.0
    command_line_capture: bool = False
    generate_reports_automatically: bool = True
    support_bundle_minutes: int = 10
    fsync_each_write: bool = True

    @property
    def log_path(self) -> Path:
        return Path(os.path.expandvars(self.log_dir)).expanduser()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["log_dir"] = str(self.log_path)
        return data

    def redacted_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        home = str(Path.home())
        if data.get("log_dir", "").lower().startswith(home.lower()):
            data["log_dir"] = "%USERPROFILE%" + data["log_dir"][len(home) :]
        if not self.command_line_capture:
            data["command_line_capture"] = False
        return data


def config_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = os.environ.get("GPU_FLIGHT_RECORDER_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "config.json")
    candidates.append(Path.home() / "Documents" / "GPUFlightRecorder" / "config.json")
    return candidates


def load_config(path: str | Path | None = None) -> Config:
    selected: Path | None = Path(path) if path else None
    if selected is None:
        selected = next((candidate for candidate in config_candidates() if candidate.exists()), None)
    if selected is None or not selected.exists():
        return Config()

    with selected.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    config = Config()
    valid_fields = set(Config.__dataclass_fields__)
    for key, value in raw.items():
        if key in valid_fields:
            setattr(config, key, value)
    return config


def write_default_user_config(destination: Path | None = None) -> Path:
    path = destination or (Path.home() / "Documents" / "GPUFlightRecorder" / "config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8") as handle:
            json.dump(Config().redacted_dict(), handle, indent=2)
            handle.write("\n")
    return path
