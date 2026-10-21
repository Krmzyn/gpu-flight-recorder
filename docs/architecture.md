# Architecture

[Back to the README](../README.md)

## Key Capabilities

- 1-second heartbeat proof-of-life that never calls `nvidia-smi`, so a wedged GPU tool cannot hide whether Windows was still alive.
- Isolated NVIDIA GPU probe in its own worker with a hard timeout: GPU name, driver version, utilization, VRAM, temperature, power, clocks, pstate, PCIe link, display attached/active. Probe failures and timeouts go to a *separate* stream.
- Keyboard lock-state tracking (Caps/Num/Scroll Lock) as a during-freeze "is input still alive?" signal.
- Display mode and topology: resolution/refresh, HDMI/DisplayPort output technology, monitor friendly name, EDID native timing, advanced color/HDR metadata, and below-native-refresh detection.
- Structured Windows Event Log capture for Kernel-Power, BugCheck/WER, Display 4101 (TDR), WHEA, and NVIDIA `nvlddmkm` (including Xid extraction), with per-query health so empty channels are visible instead of silently missing.
- Sleep/resume/wake markers derived from Kernel-Power and Power-Troubleshooter events.
- Filename-only crash-dump scan of LiveKernelReports/WATCHDOG and both minidump locations (resolved from the `CrashControl` registry key).
- Manual markers (typed or one-click) plus a freeze classifier that distinguishes a likely display-path stall from an NVIDIA management-path wedge, a thermal/power event, or a full system stall.
- Privacy-trimmed support bundle (ZIP) for sharing when you ask for help.
- Zero third-party runtime dependencies: Python standard library only.

## Example output

Every session produces a `final_report.md`. A trimmed example ([full sample](../sample_report.md)):

```markdown
## Freeze Classification

- Analysis window uses display-freeze marker: 2026-07-03T10:42:46 to 2026-07-03T10:43:34.
- Heartbeat evidence present: logger/Windows proof-of-life records continued in the analysis window.
- Display-path stall likely: heartbeat continued, input/audio evidence exists, and GPU probing failed or stalled.
- Thermal/power pressure unlikely: temperature, power, and VRAM were not elevated in the analysis window.

## Recent Windows Events

| Time                          | Log    | Provider | ID   | Level   | Message                                              |
| ----------------------------- | ------ | -------- | ---- | ------- | ---------------------------------------------------- |
| 2026-07-03T10:43:09.000-04:00 | System | Display  | 4101 | Warning | Display driver stopped responding and has recovered. |
```

You can also check Windows Event Log coverage without recording a session:

```text
> gpu-flight-recorder verify-events --days 30
Event verification window: last 30 day(s)
Captured relevant events: 4
Captured power events: 2

Critical queries:
- kernel_power_transitions: raw=6, captured=2, status=ok
- display_tdr: raw=1, captured=1, status=ok
- nvlddmkm: raw=0, captured=0, status=ok
- whea_logger: raw=0, captured=0, status=ok
```

## Recorder design

GPU Flight Recorder is a Tkinter GUI over a threaded recorder core. Each collector runs in its own daemon thread and writes to an append-only JSONL stream that is flushed and `fsync`-ed on every write, so the last samples have the best chance of surviving a hard freeze or reboot.

```
GpuFlightRecorderApp  (Tkinter GUI, gui.py)
        |  Start / Stop / Marker / Export
        v
FlightRecorder  (recorder.py) -- supervises 6 daemon worker threads
        |
        +-- HeartbeatWorker  -> heartbeat.jsonl            (1s proof-of-life, never calls nvidia-smi)
        +-- GpuProbeWorker   -> gpu_samples.jsonl          (isolated nvidia-smi with hard timeout)
        |                       gpu_probe_failures.jsonl   (failures/timeouts kept separate)
        +-- KeyboardWorker   -> keyboard_state.jsonl       (Caps / Num / Scroll lock changes)
        +-- DisplayWorker    -> display_events.jsonl       (mode + topology + EDID native timing)
        +-- ProcessWorker    -> processes.jsonl            (target apps via PowerShell Get-Process)
        +-- EventWorker      -> events.jsonl               (Get-WinEvent structured queries)
                                power_events.jsonl         (normalized sleep/resume/wake)
        |
        v
report.py    -> final_report.md         (freeze classifier + summary + collector health)
exporter.py  -> support_bundle_*.zip    (privacy-trimmed, last N minutes)
```

Supporting modules:

- `collectors.py`: all data acquisition: Win32 APIs via `ctypes` (memory, display config, EDID, keyboard state, foreground process), the `nvidia-smi` subprocess, and PowerShell `Get-WinEvent` / `Get-Process` queries.
- `io_utils.py`: append-only JSONL writer with flush + `fsync`, atomic JSON writes, and ISO-8601 timestamps.
- `marker.py`: active-session pointer + CLI marker injection into a running session.
- `config.py`: dataclass config with JSON overrides and a redacted view for support bundles.
- `tk_bootstrap.py`: best-effort Tcl/Tk path resolution so the GUI starts across Python installs.

Design choices worth calling out:

- The heartbeat is decoupled from `nvidia-smi`. A hung GPU query can never block or hide proof-of-life.
- GPU probe failures are first-class data in their own stream, not swallowed exceptions.
- Unclean sessions are recovered on next launch: the recorder finds sessions still marked "running," writes recovery diagnostics, and regenerates their report.
