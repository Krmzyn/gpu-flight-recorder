# GPU Flight Recorder

GPU Flight Recorder is a local Windows diagnostic recorder for GPU/display freezes, driver recovery events, signal drops, wake-from-sleep weirdness, and suspected NVIDIA display-path stalls.

It is source-available for noncommercial use. It is not OSI open source, and it is not licensed for resale, paid bundling, SaaS wrapping, or other commercial use without written permission.

## Why This Exists

I built this after my display started freezing hard after a heavy Autodesk Fusion workflow involving MCP tasks. The PC was still alive, but the screen would stall for 10-25 seconds, sometimes longer, and Windows was not giving me useful codes. Worst case was usually the first wake of the day.

This tool did not fix the issue. It gave me the evidence I was missing: heartbeat proof, GPU probe hangs, Windows event timing, LiveKernelReports/WATCHDOG dump filenames, sleep/resume markers, and display topology. That narrowed the problem to the GPU driver/display path instead of guessing.

I could not find a small free local tool that recorded the exact evidence I needed, so I made one.

## What It Is

- A local flight recorder for Windows GPU/display stability issues.
- A manual GUI tool you leave running while you work.
- A way to correlate freezes with GPU telemetry, Windows events, power transitions, dump filenames, display topology, process state, and manual markers.
- A support-bundle generator that trims the session down to the useful recent window.

## What It Is Not

- Not a repair tool.
- Not a kernel debugger.
- Not a driver resetter.
- Not a hardware stress test.
- Not a warranty bypass.
- Not proof that every freeze is the GPU.
- Not a stability guarantee.

## Requirements

- Windows 10/11.
- Python 3.11 or newer.
- NVIDIA GPU tooling available on PATH for GPU probe data. The recorder still captures heartbeat, events, markers, power transitions, and other diagnostics when `nvidia-smi` is unavailable.

Administrator mode is recommended. The app can run unelevated, but minidumps, LiveKernelReports, some NVIDIA rows, and optional Windows event channels may be unavailable.

## Run

Double-click:

```bat
GPUFlightRecorder.bat
```

Or run from a terminal:

```powershell
python -m gpu_flight_recorder
```

After editable install:

```powershell
gpu-flight-recorder
```

Click **Start Recording**, leave it running while you work, and add markers when something important happens. The quick buttons cover common notes like `display froze / observed`, `display recovered`, `started Blender`, `started Fusion`, and `opened Claude`.

If the app is not running as administrator it will offer to relaunch elevated before recording. You can continue unelevated, but the session diagnostics will record the reduced coverage.

Add a marker from a terminal while the GUI recorder is running:

```powershell
python -m gpu_flight_recorder marker "display froze / observed"
```

Or:

```bat
GPUFlightRecorderMarker.bat "caps lock responded"
```

During an actual display freeze, you probably cannot click a GUI button. The practical flow is:

1. Press Caps Lock or Num Lock once while the screen is frozen.
2. When the display comes back, click `display froze / observed`.
3. Add `display recovered` if the recovery time is not obvious.

The keyboard-state collector records lock-key changes separately, so that is the stronger during-freeze signal. The manual freeze marker is a human observation marker after the fact, not proof of the exact freeze start time.

Verify Windows Event Log coverage without starting a recording session:

```powershell
python -m gpu_flight_recorder verify-events --days 30
```

The command prints critical query counts for Kernel-Power, BugCheck/WER, Display 4101, WHEA, and NVIDIA provider events, plus non-critical notes for optional operational channels that are missing or disabled.

## Logs

By default sessions are written under:

```text
%USERPROFILE%\Documents\GPUFlightRecorder\logs
```

Each run creates a session folder:

```text
session-YYYYMMDD-HHMMSS\
  heartbeat.jsonl
  gpu_samples.jsonl
  gpu_probe_failures.jsonl
  keyboard_state.jsonl
  display_events.jsonl
  diagnostics.jsonl
  live_kernel_reports.jsonl
  events.jsonl
  power_events.jsonl
  processes.jsonl
  markers.jsonl
  session_info.json
  final_report.md
```

Writes are flushed and fsynced by default so the final samples have the best chance of surviving a hard freeze or reboot.

## What It Captures

- Heartbeat proof-of-life every 1 second without calling `nvidia-smi`.
- NVIDIA telemetry from an isolated GPU probe worker: GPU name, driver version, utilization, VRAM, temperature, power, clocks, pstate, PCIe link, display attached/active.
- GPU probe failures and timeouts in a separate stream so a wedged `nvidia-smi` cannot hide the failure.
- System telemetry: uptime, CPU load, RAM use, foreground process name/PID.
- Keyboard lock-state changes for Caps Lock, Num Lock, and Scroll Lock.
- Display mode and display-device changes.
- Display topology where Windows exposes it: HDMI/DisplayPort output technology, monitor friendly name/path, EDID native timing, advanced color/HDR metadata, and below-native-refresh warnings.
- Sleep/resume/wake markers derived from Kernel-Power and Power-Troubleshooter events.
- Filename-only LiveKernelReports and minidump scans where Windows permissions allow it. The minidump scanner reads `HKLM\SYSTEM\CurrentControlSet\Control\CrashControl\MinidumpDir` and also checks both `C:\Windows\Minidump` and `C:\Windows\Minidumps`.
- Target process presence for apps commonly involved in GPU/display workflows.
- Relevant Windows warning/error/critical events from System, Application, WER, WHEA, power/thermal, and available driver logs.
- Collector health and end-of-session stream counts so empty important streams are visible in `diagnostics.jsonl` and the final report.
- Event query health showing critical query counts separately from optional channel failures.
- Manual markers you type or add with quick buttons.

Command-line capture is disabled by default for privacy.

## Privacy

The recorder is local-only. It does not upload anything, collect screenshots, read private documents, install a driver, change registry keys, change GPU settings, or reset the GPU.

It can still capture sensitive diagnostic context:

- process names and PIDs
- foreground process name/PID
- GPU model and driver version
- Windows event metadata and messages
- dump filenames and timestamps
- monitor/display topology and EDID-derived timing
- power events and wake/resume timing
- markers you type yourself
- collector diagnostics and error messages

Review support bundles before sharing them. The default support bundle is trimmed and privacy-conscious, but it is still a diagnostic artifact from your machine.

## Export For Help

Use **Export Support Bundle** to create a privacy-trimmed ZIP in the session folder. The default support bundle includes:

- `final_report.md`
- `summary.json`
- `config.redacted.json`
- last 10 minutes of heartbeat, GPU samples, GPU failures, keyboard state changes, display changes, diagnostics, events, power events, processes, and markers
- `chart_data.csv`

This is the file to share when asking for help. It avoids full raw logs by default, but you should still review it first.

## Interpreting Results

- If heartbeat continues near a `display froze / observed` marker, Windows was still alive and the issue is likely in the display/output/driver path.
- If heartbeat stops at the same time as the freeze, suspect a broader system stall, kernel hang, power interruption, or hard GPU failure.
- If `nvidia-smi` fails while heartbeat continues, the NVIDIA driver/GPU management path may be unavailable while Windows remains responsive.
- If keyboard state changes or a `caps lock responded` marker appears while the display is frozen, Windows input is still alive.
- If a LiveKernelReports or WATCHDOG dump filename appears after recovery, include that filename/timestamp when asking for help.
- If events only appear after recovery or reboot, compare their timestamps with the final sample and your markers.
- The freeze classifier ignores unmarked startup GPU-probe blips during the warmup window and only treats sustained or clustered probe failures as stall evidence.

## Config

Click **Create Config** in the app or copy `config.example.json` to:

```text
%USERPROFILE%\Documents\GPUFlightRecorder\config.json
```

You can change worker intervals, target processes, event keywords, log directory, support-bundle window, LiveKernel scan paths, and whether to collect process command lines.

## Install For Development

```powershell
python -m pip install -e .
python -m unittest discover -v
```

## License

GPU Flight Recorder is licensed under the PolyForm Noncommercial License 1.0.0.

You can read, run, share, and modify it for personal and other noncommercial uses. Commercial use, resale, paid bundling, SaaS wrapping, or redistribution for profit requires separate written permission. See [LICENSE](LICENSE), [NOTICE.md](NOTICE.md), and [COMMERCIAL_USE.md](COMMERCIAL_USE.md).

## Limitations

- This is not a kernel debugger and cannot prove every GPU driver failure mode.
- Some `nvidia-smi` per-process rows may show `[Insufficient Permissions]` without administrator rights.
- Some Windows event channels vary by machine and Windows build; unavailable optional channels are skipped.
- ETW/WPR/GPUView traces are not part of the beta because they require Windows Performance Toolkit, admin elevation, and large `.etl` captures.
