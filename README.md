# GPU Flight Recorder

GPU Flight Recorder is a local, zero-dependency Windows tool that continuously records GPU, display, power, and Windows-event evidence so you can tell what actually happened during a display freeze or driver-recovery event — instead of guessing.

> **Built with:** Python 3.11+, Tkinter, Windows Win32 APIs via `ctypes`, PowerShell, NVIDIA `nvidia-smi`, append-only JSONL logs, `unittest`, and GitHub Actions.

![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6)
![Status: Beta](https://img.shields.io/badge/status-beta-orange)

> **Status: public beta / portfolio project.** It works and is covered by an automated test suite, but it is not production-hardened, it targets Windows + NVIDIA specifically, and it is a diagnostic recorder — not a repair tool. See [Limitations](#limitations).

## The Problem

When a Windows PC freezes at the display while the machine itself is still alive, the built-in tooling rarely tells you *what* stalled. The screen locks up for 10–25 seconds, sometimes longer, then recovers — and the Event Viewer either shows nothing useful or shows it minutes later with no correlation to what you were doing. You are left guessing whether it was the GPU, the driver, the display path, thermals, power, or the whole system.

GPU Flight Recorder answers the first question you actually need answered: **was the rest of the machine still alive while the screen was frozen?** From there it correlates the freeze against GPU telemetry, Windows events, power transitions, crash-dump filenames, display topology, and your own markers so the evidence points somewhere specific instead of nowhere.

## Why This Exists

I built this after my display started freezing hard during heavy Autodesk Fusion and Blender sessions. The PC was still running, but the screen would stall for 10–25 seconds, sometimes longer, and Windows was not giving me useful codes. The worst case was usually the first wake of the day.

This tool did not fix the freezes. It gave me the evidence I was missing: heartbeat proof-of-life, GPU probe hangs, Windows event timing, LiveKernelReports/WATCHDOG dump filenames, sleep/resume markers, and display topology. That narrowed the problem to the GPU driver / display path instead of guessing.

I could not find a small, free, local tool that recorded the exact evidence I needed, so I made one.

## Who It Is For

- PC builders and enthusiasts chasing intermittent GPU/display freezes on Windows + NVIDIA.
- Anyone assembling evidence for a driver bug report, RMA, or vendor support ticket who needs timestamps and dump filenames rather than "it froze again."
- Developers and technical users who want a local, auditable, no-network diagnostic they can leave running while they work.

## What It Is

- A local flight recorder for Windows GPU/display stability issues.
- A manual GUI tool you leave running while you work.
- A way to correlate freezes with GPU telemetry, Windows events, power transitions, dump filenames, display topology, process state, and manual markers.
- A support-bundle generator that trims a session down to the useful recent window.

## What It Is Not

- Not a repair tool, kernel debugger, driver resetter, or hardware stress test.
- Not a warranty bypass.
- Not proof that every freeze is the GPU.
- Not a stability guarantee.

## Key Capabilities

- **1-second heartbeat proof-of-life** that never calls `nvidia-smi`, so a wedged GPU tool cannot hide whether Windows was still alive.
- **Isolated NVIDIA GPU probe** in its own worker with a hard timeout: GPU name, driver version, utilization, VRAM, temperature, power, clocks, pstate, PCIe link, display attached/active — with probe failures and timeouts written to a *separate* stream.
- **Keyboard lock-state tracking** (Caps/Num/Scroll Lock) as a during-freeze "is input still alive?" signal.
- **Display mode and topology**: resolution/refresh, HDMI/DisplayPort output technology, monitor friendly name, EDID native timing, advanced color/HDR metadata, and below-native-refresh detection.
- **Structured Windows Event Log capture** for Kernel-Power, BugCheck/WER, Display 4101 (TDR), WHEA, and NVIDIA `nvlddmkm` (including Xid extraction), with per-query health so empty channels are visible instead of silently missing.
- **Sleep/resume/wake markers** derived from Kernel-Power and Power-Troubleshooter events.
- **Filename-only crash-dump scan** of LiveKernelReports/WATCHDOG and both minidump locations (resolved from the `CrashControl` registry key).
- **Manual markers** (typed or one-click) plus a **freeze classifier** that distinguishes a likely display-path stall from an NVIDIA management-path wedge, a thermal/power event, or a full system stall.
- **Privacy-trimmed support bundle** (ZIP) for sharing when you ask for help.
- **Zero third-party runtime dependencies** — Python standard library only.

## Example Output

Every session produces a `final_report.md`. A trimmed example ([full sample](sample_report.md)):

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

## Architecture

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
        +-- DisplayWorker    -> display_events.jsonl        (mode + topology + EDID native timing)
        +-- ProcessWorker    -> processes.jsonl            (target apps via PowerShell Get-Process)
        +-- EventWorker      -> events.jsonl               (Get-WinEvent structured queries)
                                power_events.jsonl         (normalized sleep/resume/wake)
        |
        v
report.py    -> final_report.md         (freeze classifier + summary + collector health)
exporter.py  -> support_bundle_*.zip    (privacy-trimmed, last N minutes)
```

Supporting modules:

- **`collectors.py`** — all data acquisition: Win32 APIs via `ctypes` (memory, display config, EDID, keyboard state, foreground process), the `nvidia-smi` subprocess, and PowerShell `Get-WinEvent` / `Get-Process` queries.
- **`io_utils.py`** — append-only JSONL writer with flush + `fsync`, atomic JSON writes, and ISO-8601 timestamps.
- **`marker.py`** — active-session pointer + CLI marker injection into a running session.
- **`config.py`** — dataclass config with JSON overrides and a redacted view for support bundles.
- **`tk_bootstrap.py`** — best-effort Tcl/Tk path resolution so the GUI starts across Python installs.

Design choices worth calling out:

- The heartbeat is **decoupled from `nvidia-smi`**. A hung GPU query can never block or hide proof-of-life.
- GPU probe **failures are first-class data** in their own stream, not swallowed exceptions.
- Unclean sessions are **recovered on next launch**: the recorder finds sessions still marked "running," writes recovery diagnostics, and regenerates their report.

## Requirements

- Windows 10 or 11.
- Python 3.11 or newer.
- NVIDIA GPU tooling (`nvidia-smi`) on `PATH` for GPU probe data. The recorder still captures heartbeat, events, markers, power transitions, keyboard state, and display topology when `nvidia-smi` is unavailable.

Administrator mode is recommended. The app runs unelevated, but minidumps, LiveKernelReports, some NVIDIA per-process rows, and optional Windows event channels may be unavailable. When unelevated, the reduced coverage is recorded in the session diagnostics.

## Installation

Clone and install in editable mode:

```powershell
git clone https://github.com/Krmzyn/gpu-flight-recorder.git
cd gpu-flight-recorder
python -m pip install -e .
```

This installs the `gpu-flight-recorder` console command. There are no third-party runtime dependencies.

## Quick Start

Launch the GUI by double-clicking `GPUFlightRecorder.bat`, or from a terminal:

```powershell
python -m gpu_flight_recorder
```

After an editable install you can also run:

```powershell
gpu-flight-recorder
```

Click **Start Recording**, leave it running while you work, and add markers when something important happens. Quick buttons cover common notes like `display froze / observed`, `display recovered`, `started Blender`, `started Fusion`, and `opened Claude`. If the app is not elevated it will offer to relaunch as administrator before recording; you can decline and continue with reduced coverage.

### During an actual freeze

You usually cannot click a GUI button while the screen is frozen. The practical flow is:

1. Press **Caps Lock** or **Num Lock** once while the screen is frozen — the keyboard-state collector records the lock-key change, which is the stronger during-freeze signal.
2. When the display comes back, click `display froze / observed`.
3. Add `display recovered` if the recovery time is not obvious.

The manual freeze marker is a human observation *after the fact*, not proof of the exact freeze start time.

You can also add a marker from a terminal while the GUI recorder is running:

```powershell
python -m gpu_flight_recorder marker "display froze / observed"
# or:
GPUFlightRecorderMarker.bat "caps lock responded"
```

### Check event-log coverage without recording

```powershell
python -m gpu_flight_recorder verify-events --days 30
```

This prints critical query counts for Kernel-Power, BugCheck/WER, Display 4101, WHEA, and NVIDIA provider events, plus notes for optional operational channels that are missing or disabled.

## Logs and Session Layout

By default, sessions are written under:

```text
%USERPROFILE%\Documents\GPUFlightRecorder\logs
```

Each run creates a `session-YYYYMMDD-HHMMSS\` folder containing per-stream JSONL files (`heartbeat.jsonl`, `gpu_samples.jsonl`, `gpu_probe_failures.jsonl`, `keyboard_state.jsonl`, `display_events.jsonl`, `diagnostics.jsonl`, `live_kernel_reports.jsonl`, `events.jsonl`, `power_events.jsonl`, `processes.jsonl`, `markers.jsonl`), a `session_info.json`, and the generated `final_report.md`.

Use **Export Support Bundle** to create a privacy-trimmed ZIP in the session folder. The default bundle includes the report, a `summary.json`, a redacted config, `chart_data.csv`, and the last 10 minutes of each stream — not the full raw logs. Review it before sharing.

## Configuration and Environment Variables

Click **Create Config** in the app, or copy [`config.example.json`](config.example.json) to:

```text
%USERPROFILE%\Documents\GPUFlightRecorder\config.json
```

You can change worker intervals, target processes, event keywords, log directory, support-bundle window, LiveKernel scan paths, freeze-classifier thresholds, and whether to collect process command lines (off by default for privacy).

Config is discovered in this order (first match wins):

1. The path in the `GPU_FLIGHT_RECORDER_CONFIG` environment variable, if set.
2. `config.json` in the current working directory.
3. `%USERPROFILE%\Documents\GPUFlightRecorder\config.json`.

The only application-specific environment variable is `GPU_FLIGHT_RECORDER_CONFIG`. See [`.env.example`](.env.example) for details. The tool reads real process environment variables — it does not auto-load a `.env` file.

## Privacy

The recorder is local-only. It does not upload anything, take screenshots, read documents, install a driver, or change registry keys or GPU settings.

It can still capture sensitive diagnostic context: process names/PIDs, foreground process, GPU model and driver version, Windows event metadata and messages, dump filenames and timestamps, monitor/display topology and EDID-derived timing, power/wake timing, markers you type, and collector diagnostics. Command-line capture is **disabled by default**. The default support bundle is trimmed and redacted, but it is still a diagnostic artifact from your machine — review it before sharing.

## Testing and Verification

```powershell
python -m pip install -e .
python -m unittest discover -v
```

The suite (26 tests) covers the NVIDIA CSV parser, EDID timing parser, event filtering and structured query building, power-event normalization, the freeze classifier, report generation, support-bundle export, crash recovery, and the CLI marker path — plus a smoke test that the GUI instantiates.

Smoke-test the event query path end to end without creating a session:

```powershell
gpu-flight-recorder verify-events --days 0
```

## Limitations

- This is a **portfolio-quality beta**, not production-hardened software.
- Windows + NVIDIA only. There is no AMD/Intel GPU path and no macOS/Linux support (the collectors use Win32 APIs, `nvidia-smi`, and PowerShell).
- It is not a kernel debugger and cannot prove every GPU driver failure mode.
- Some `nvidia-smi` per-process rows may show `[Insufficient Permissions]` without administrator rights.
- Some Windows event channels vary by machine and Windows build; unavailable optional channels are skipped and noted.
- ETW/WPR/GPUView traces are out of scope for the beta (they require the Windows Performance Toolkit, admin elevation, and large `.etl` captures).

## Roadmap

Ideas under consideration, not commitments:

- Optional deeper tracing hooks (ETW / GPUView) behind an explicit opt-in.
- In-app charts for the GPU telemetry and heartbeat timeline.
- HTML report export alongside Markdown.
- Non-NVIDIA GPU telemetry (AMD/Intel) where equivalent CLIs exist.
- A packaged executable so non-Python users can run it without a toolchain.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Please never attach private logs, support bundles, or machine-specific data to issues or pull requests. Security reporting guidance is in [SECURITY.md](SECURITY.md).

## License

GPU Flight Recorder is released under the [MIT License](LICENSE) — free to use, modify, and distribute, including commercially, with attribution.

Created by [Alex Jardine](https://github.com/Krmzyn).
