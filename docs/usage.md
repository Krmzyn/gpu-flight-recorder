# Usage

[Back to the README](../README.md)

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

Click **Start Recording**, leave it running while you work, and add markers when something important happens. Quick buttons cover common notes like `display froze / observed`, `display recovered`, `started Blender`, `started Fusion`, and `opened Chrome`. If the app is not elevated it will offer to relaunch as administrator before recording; you can decline and continue with reduced coverage.

### During an actual freeze

You usually cannot click a GUI button while the screen is frozen. The practical flow is:

1. Press **Caps Lock** or **Num Lock** once while the screen is frozen. The keyboard-state collector records the lock-key change, which is the stronger during-freeze signal.
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

Use **Export Support Bundle** to create a privacy-trimmed ZIP in the session folder. The default bundle includes the report, a `summary.json`, a redacted config, `chart_data.csv`, and the last 10 minutes of each stream, not the full raw logs. Review it before sharing.

## Configuration and Environment Variables

Click **Create Config** in the app, or copy [`config.example.json`](../config.example.json) to:

```text
%USERPROFILE%\Documents\GPUFlightRecorder\config.json
```

You can change worker intervals, target processes, event keywords, log directory, support-bundle window, LiveKernel scan paths, freeze-classifier thresholds, and whether to collect process command lines (off by default for privacy).

Config is discovered in this order (first match wins):

1. The path in the `GPU_FLIGHT_RECORDER_CONFIG` environment variable, if set.
2. `config.json` in the current working directory.
3. `%USERPROFILE%\Documents\GPUFlightRecorder\config.json`.

The only application-specific environment variable is `GPU_FLIGHT_RECORDER_CONFIG`. See [`.env.example`](../.env.example) for details. The tool reads real process environment variables; it does not auto-load a `.env` file.

## Privacy

The recorder is local-only. It does not upload anything, take screenshots, read documents, install a driver, or change registry keys or GPU settings.

It can still capture sensitive diagnostic context: process names/PIDs, foreground process, GPU model and driver version, Windows event metadata and messages, dump filenames and timestamps, monitor/display topology and EDID-derived timing, power/wake timing, markers you type, and collector diagnostics. Command-line capture is **disabled by default**. The default support bundle is trimmed and redacted, but it is still a diagnostic artifact from your machine. Review it before sharing.

## Limitations

- This is a **beta**, not production-hardened software.
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
