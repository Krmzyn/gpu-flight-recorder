# GPU Flight Recorder Report

- Session folder: `C:\Users\You\Documents\GPUFlightRecorder\logs\session-20260703-101500`
- Started: 2026-07-03T10:15:00.000-04:00
- Ended/last heartbeat: 2026-07-03T10:45:14.000-04:00
- Clean exit: no
- Heartbeats: 1800 total, 600 in final 10 minutes
- GPU samples: 890 total, 145 in final 10 minutes
- GPU probe failures: 2 total, 2 in final 10 minutes
- Events: 3 total, 2 in final 10 minutes
- Markers: 5

## Freeze Classification

- Analysis window uses display-freeze marker: 2026-07-03T10:42:46-04:00 to 2026-07-03T10:43:34-04:00.
- Heartbeat evidence present: logger/Windows proof-of-life records continued in the analysis window.
- Display-path stall likely: heartbeat continued, input/audio evidence exists, and GPU probing failed or stalled.
- Thermal/power pressure unlikely: temperature, power, and VRAM were not elevated in the analysis window.

## Final Five Minutes

- Max GPU temperature: 47 C
- Max GPU power: 75 W
- Max GPU memory used: 2200 MiB
- Max GPU utilization: 12%
- nvidia-smi failures/timeouts: 2

## Suspicion Hints

- Display-path stall likely: heartbeat continued, input/audio evidence exists, and GPU probing failed or stalled.
- Thermal/power pressure unlikely: temperature, power, and VRAM were not elevated in the analysis window.

## Recent Diagnostics

- 2026-07-03T10:43:21.000-04:00: gpu_probe_stalled - GPU probe has not produced a successful sample for more than 5 seconds while heartbeat continues.

## GPU Probe Failures

| Time | Type | Timeout | Elapsed ms | Error |
| --- | --- | ---: | ---: | --- |
| 2026-07-03T10:43:21.000-04:00 | timeout | 1.0 | 1001 | nvidia-smi timed out |

## Target Processes Near End

| Process | PID | CPU % | RAM MB | Responding |
| --- | ---: | ---: | ---: | --- |
| blender.exe | 12344 | 18.2 | 6120.4 | True |
| claude.exe | 28632 | 0.7 | 412.1 | True |
| dwm.exe | 1668 | 2.1 | 180.3 | True |

## Recent Windows Events

| Time | Log | Provider | ID | Level | Message |
| --- | --- | --- | ---: | --- | --- |
| 2026-07-03T10:43:09.000-04:00 | System | Display | 4101 | Warning | Display driver stopped responding and has recovered. |

## Markers

- 2026-07-03T10:30:02.000-04:00: started Blender render
- 2026-07-03T10:43:01.000-04:00: display froze / observed
- 2026-07-03T10:43:34.000-04:00: display recovered
- 2026-07-03T10:43:35.000-04:00: caps lock responded
- 2026-07-03T10:44:10.000-04:00: rebooted
