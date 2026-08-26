import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from gpu_flight_recorder.collectors import (
    build_event_query_specs,
    collect_display_topology,
    collect_events_since,
    collect_live_kernel_reports,
    event_matches,
    extract_nvidia_xid,
    normalize_power_event,
    parse_edid_native_timing,
    parse_nvidia_gpu_csv,
    resolve_live_kernel_paths,
)
from gpu_flight_recorder.config import Config
from gpu_flight_recorder.exporter import export_support_bundle
from gpu_flight_recorder.io_utils import JsonlWriter, atomic_write_json, read_jsonl
from gpu_flight_recorder.marker import add_marker_to_active, write_active_session
from gpu_flight_recorder.recorder import FlightRecorder
from gpu_flight_recorder.report import freeze_classification, generate_report, summarize_session


class CoreTests(unittest.TestCase):
    def test_parse_nvidia_gpu_csv(self):
        output = "2026/07/03 10:11:06.066, NVIDIA GeForce RTX 3080, 610.62, 610.62, 34, 18, 10240, 1435, 8617, 45, 38.63, 38.63, 210, 405, P8, 1, 4, 4, 16, Enabled, Yes\n"
        rows = parse_nvidia_gpu_csv(output)
        self.assertEqual(rows[0]["name"], "NVIDIA GeForce RTX 3080")
        self.assertEqual(rows[0]["memory_total"], "10240")
        self.assertEqual(rows[0]["display_active"], "Enabled")

    def test_event_filter_matches_provider_keyword_and_id(self):
        config = Config()
        self.assertTrue(event_matches({"ProviderName": "nvlddmkm", "Id": 13, "Message": "x"}, config.event_keywords))
        self.assertTrue(event_matches({"ProviderName": "Other", "Id": 4101, "Message": "x"}, config.event_keywords))
        self.assertFalse(event_matches({"ProviderName": "Other", "Id": 1, "Message": "routine"}, config.event_keywords))

    def test_event_specs_include_priority_provider_id_queries(self):
        specs = build_event_query_specs(Config())
        names = {spec["QueryName"] for spec in specs}
        self.assertIn("kernel_power_transitions", names)
        self.assertIn("display_tdr", names)
        self.assertIn("nvlddmkm", names)
        power = next(spec for spec in specs if spec["QueryName"] == "kernel_power_transitions")
        self.assertEqual(power["ProviderName"], "Microsoft-Windows-Kernel-Power")
        self.assertIn(41, power["Ids"])
        self.assertIn(107, power["Ids"])

    def test_collect_events_uses_structured_query_and_reports_channel_errors(self):
        captured = {}
        config = Config(event_keywords=["DO_NOT_SERVER_FILTER"])

        def fake_powershell(script, timeout=8.0):
            captured["script"] = script
            return {
                "Events": [
                    {
                        "QueryName": "kernel_power_transitions",
                        "TimeCreated": "2026-07-04T09:41:00.000-04:00",
                        "LogName": "System",
                        "Id": 107,
                        "RecordId": 22,
                        "ProviderName": "Microsoft-Windows-Kernel-Power",
                        "LevelDisplayName": "Information",
                        "Message": "The system has resumed from sleep.",
                    },
                    {
                        "QueryName": "nvlddmkm",
                        "TimeCreated": "2026-07-04T09:42:00.000-04:00",
                        "LogName": "System",
                        "Id": 14,
                        "RecordId": 23,
                        "ProviderName": "nvlddmkm",
                        "LevelDisplayName": "Error",
                        "Message": "NVIDIA Xid 79",
                    },
                ],
                "Errors": [
                    {
                        "QueryName": "dwm_core_warnings",
                        "LogName": "Microsoft-Windows-Dwm-Core/Operational",
                        "ProviderName": "",
                        "Severity": "optional",
                        "ErrorType": "EventLogException",
                        "Message": "channel disabled",
                    }
                ],
                "QueryHealth": [
                    {
                        "QueryName": "kernel_power_transitions",
                        "LogName": "System",
                        "ProviderName": "Microsoft-Windows-Kernel-Power",
                        "Ids": [41, 42, 107, 109],
                        "Levels": [],
                        "Severity": "critical",
                        "Success": True,
                        "EventCount": 1,
                    },
                    {
                        "QueryName": "dwm_core_warnings",
                        "LogName": "Microsoft-Windows-Dwm-Core/Operational",
                        "ProviderName": "",
                        "Ids": [],
                        "Levels": [1, 2, 3],
                        "Severity": "optional",
                        "Success": False,
                        "EventCount": 0,
                        "ErrorType": "EventLogException",
                        "Message": "channel disabled",
                    },
                ],
            }

        seen = set()
        with mock.patch("gpu_flight_recorder.collectors._powershell_json", side_effect=fake_powershell):
            record = collect_events_since(config, "2026-07-04T00:00:00.000-04:00", seen, "startup_scan")
            duplicate = collect_events_since(config, "2026-07-04T00:00:00.000-04:00", seen, "poll")

        self.assertIn("Get-WinEvent -FilterHashtable $filter", captured["script"])
        self.assertNotIn("DO_NOT_SERVER_FILTER", captured["script"])
        self.assertEqual(len(record["events"]), 2)
        self.assertEqual(record["events"][1]["nvidia_xid"], 79)
        self.assertEqual(record["power_events"][0]["transition"], "resume")
        self.assertEqual(record["errors"][0]["QueryName"], "dwm_core_warnings")
        self.assertEqual(record["errors"][0]["Severity"], "optional")
        self.assertEqual(record["query_health"][0]["CapturedEventCount"], 1)
        self.assertEqual(duplicate["events"], [])

    def test_power_event_normalization_and_xid_extraction(self):
        event = {
            "timestamp": "2026-07-04T09:41:00.000-04:00",
            "LogName": "System",
            "Id": 42,
            "RecordId": 55,
            "ProviderName": "Microsoft-Windows-Kernel-Power",
            "Message": "The system is entering sleep.",
        }
        normalized = normalize_power_event(event)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["transition"], "sleep_enter")
        self.assertEqual(extract_nvidia_xid("Display driver nvlddmkm stopped, Xid 61"), 61)

    def _edid_with_detailed_timing(self) -> bytes:
        edid = bytearray(128)
        block = bytearray(18)
        pixel_clock_10khz = 33546
        block[0:2] = pixel_clock_10khz.to_bytes(2, "little")
        block[2] = 0x80
        block[3] = 0xA0
        block[4] = 0x70
        block[5] = 0x38
        block[6] = 0x28
        block[7] = 0x40
        edid[54:72] = block
        return bytes(edid)

    def test_edid_native_timing_parser(self):
        native = parse_edid_native_timing(self._edid_with_detailed_timing())
        self.assertTrue(native["available"])
        self.assertEqual(native["native_width"], 1920)
        self.assertEqual(native["native_height"], 1080)
        self.assertAlmostEqual(native["native_refresh_hz"], 144.0, delta=0.1)

    def test_display_topology_flags_below_native_refresh(self):
        paths = [
            {
                "output_technology": "hdmi",
                "target_name": {
                    "monitor_friendly_name": "Test Panel",
                    "monitor_device_path": r"\\?\DISPLAY#ABC123#MONITOR0#{guid}",
                },
            }
        ]
        with mock.patch("gpu_flight_recorder.collectors._query_displayconfig_paths", return_value=(paths, [])), mock.patch(
            "gpu_flight_recorder.collectors._read_edid_from_registry",
            return_value=self._edid_with_detailed_timing(),
        ):
            topology = collect_display_topology({"available": True, "width": 1920, "height": 1080, "refresh_hz": 85})
        self.assertEqual(topology["current_output_technology"], "hdmi")
        self.assertEqual(topology["current_monitor_friendly_name"], "Test Panel")
        self.assertTrue(topology["below_native_refresh"])

    def test_jsonl_writer_flushes_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "records.jsonl"
            writer = JsonlWriter(path, fsync_each_write=True)
            writer.write({"a": 1})
            writer.close()
            self.assertEqual(read_jsonl(path), [{"a": 1}])

    def _make_session(self, root: Path) -> Path:
        session = root / "session-20260703-101500"
        session.mkdir()
        atomic_write_json(
            session / "session_info.json",
            {
                "status": "running",
                "start_time": "2026-07-03T10:15:00.000-04:00",
                "last_heartbeat": "2026-07-03T10:20:00.000-04:00",
            },
        )
        writer = JsonlWriter(session / "samples.jsonl", False)
        writer.write(
            {
                "timestamp": "2026-07-03T10:19:58.000-04:00",
                "system": {"foreground_process": {"name": "blender.exe"}, "display": {"available": True, "width": 3840, "height": 2160, "refresh_hz": 120}},
                "nvidia": {
                    "responded": True,
                    "primary_gpu": {
                        "temperature_gpu": "82",
                        "power_draw": "315",
                        "memory_used": "9200",
                        "memory_total": "10240",
                        "utilization_gpu": "99",
                    },
                },
            }
        )
        writer.close()
        writer = JsonlWriter(session / "events.jsonl", False)
        writer.write(
            {
                "TimeCreated": "2026-07-03T10:19:59.000-04:00",
                "LogName": "System",
                "ProviderName": "Display",
                "Id": 4101,
                "LevelDisplayName": "Warning",
                "Message": "Display driver stopped responding and has recovered.",
            }
        )
        writer.close()
        writer = JsonlWriter(session / "processes.jsonl", False)
        writer.write(
            {
                "timestamp": "2026-07-03T10:19:57.000-04:00",
                "processes": [{"name": "blender.exe", "pid": 123, "working_set_bytes": 1048576, "responding": True}],
            }
        )
        writer.close()
        writer = JsonlWriter(session / "markers.jsonl", False)
        writer.write(
            {"timestamp": "2026-07-03T10:19:40.000-04:00", "note": "screen froze"}
        )
        writer.close()
        return session

    def test_report_and_export_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self._make_session(Path(temp))
            report = generate_report(session)
            self.assertTrue(report.exists())
            summary = summarize_session(session)
            self.assertEqual(summary["max_gpu_temp_c"], 82)
            config = Config(log_dir=temp, support_bundle_minutes=10)
            bundle = export_support_bundle(session, config)
            self.assertTrue(bundle.exists())
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                self.assertIn("final_report.md", names)
                self.assertIn("summary.json", names)
                self.assertIn("config.redacted.json", names)
                self.assertIn("chart_data.csv", names)
                self.assertIn("heartbeat.last_10m.jsonl", names)
                self.assertIn("gpu_samples.last_10m.jsonl", names)
                self.assertIn("gpu_probe_failures.last_10m.jsonl", names)
                self.assertFalse(any(name.startswith("raw/") for name in names))

    def test_report_uses_jsonl_counts_when_health_snapshot_is_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session-stale-health"
            session.mkdir()
            atomic_write_json(
                session / "session_info.json",
                {
                    "status": "closed",
                    "start_time": "2026-07-04T18:00:00.000-04:00",
                    "end_time": "2026-07-04T18:10:00.000-04:00",
                },
            )
            writer = JsonlWriter(session / "events.jsonl", False)
            writer.write(
                {
                    "timestamp": "2026-07-04T18:05:00.000-04:00",
                    "ProviderName": "Microsoft-Windows-Kernel-Power",
                    "Id": 41,
                    "Message": "unexpected shutdown",
                }
            )
            writer.close()
            writer = JsonlWriter(session / "diagnostics.jsonl", False)
            writer.write(
                {
                    "timestamp": "2026-07-04T18:01:00.000-04:00",
                    "type": "collector_health",
                    "stream_counts": {"events": 0, "heartbeat": 0},
                }
            )
            writer.write(
                {
                    "timestamp": "2026-07-04T18:02:00.000-04:00",
                    "type": "event_query_health",
                    "critical_queries": [
                        {
                            "query_name": "kernel_power_transitions",
                            "log_name": "System",
                            "provider_name": "Microsoft-Windows-Kernel-Power",
                            "event_count": 1,
                            "captured_event_count": 1,
                            "success": True,
                        }
                    ],
                    "optional_errors": [],
                }
            )
            writer.close()

            report = generate_report(session).read_text(encoding="utf-8")
            self.assertIn("Count source: computed_jsonl", report)
            self.assertIn("| events | 1 |", report)
            self.assertIn("## Event Query Health", report)
            self.assertIn("kernel_power_transitions", report)

    def test_recover_unclean_sessions_generates_report(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self._make_session(Path(temp))
            recorder = FlightRecorder(Config(log_dir=temp))
            recovered = recorder.recover_unclean_sessions()
            self.assertIn(session, recovered)
            info = json.loads((session / "session_info.json").read_text(encoding="utf-8"))
            self.assertEqual(info["status"], "recovered_unclean")
            self.assertTrue((session / "final_report.md").exists())

    def test_split_session_report_classifies_nvidia_wedge(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session-20260704-084900"
            session.mkdir()
            atomic_write_json(
                session / "session_info.json",
                {
                    "status": "closed",
                    "start_time": "2026-07-04T08:48:00.000-04:00",
                    "last_heartbeat": "2026-07-04T08:50:45.000-04:00",
                    "end_time": "2026-07-04T08:51:00.000-04:00",
                },
            )
            for timestamp in (
                "2026-07-04T08:49:41.000-04:00",
                "2026-07-04T08:49:43.000-04:00",
                "2026-07-04T08:50:21.000-04:00",
            ):
                writer = JsonlWriter(session / "heartbeat.jsonl", False)
                writer.write({"timestamp": timestamp, "monotonic_seconds": 1.0, "alive": True})
                writer.close()
            writer = JsonlWriter(session / "gpu_samples.jsonl", False)
            writer.write(
                {
                    "timestamp": "2026-07-04T08:49:41.000-04:00",
                    "responded": True,
                    "primary_gpu": {
                        "temperature_gpu": "47",
                        "power_draw": "75",
                        "memory_used": "2200",
                        "utilization_gpu": "12",
                    },
                }
            )
            writer.close()
            writer = JsonlWriter(session / "gpu_probe_failures.jsonl", False)
            writer.write(
                {
                    "timestamp": "2026-07-04T08:50:21.000-04:00",
                    "responded": False,
                    "error_type": "timeout",
                    "timeout_sec": 20.0,
                    "elapsed_ms": 20001,
                    "stderr": "nvidia-smi timed out",
                }
            )
            writer.close()
            writer = JsonlWriter(session / "keyboard_state.jsonl", False)
            writer.write({"timestamp": "2026-07-04T08:49:50.000-04:00", "caps_lock": {"toggled": True}})
            writer.close()
            writer = JsonlWriter(session / "markers.jsonl", False)
            writer.write({"timestamp": "2026-07-04T08:49:42.000-04:00", "note": "screen froze"})
            writer.write({"timestamp": "2026-07-04T08:49:55.000-04:00", "note": "audio continued"})
            writer.close()
            report = generate_report(session)
            text = report.read_text(encoding="utf-8")
            self.assertIn("Freeze Classification", text)
            classification = "\n".join(freeze_classification(session))
            self.assertIn("Display-path stall likely", classification)
            self.assertIn("Thermal/power pressure unlikely", classification)

    def test_freeze_classifier_ignores_startup_probe_blips(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session-20260704-180336"
            session.mkdir()
            atomic_write_json(
                session / "session_info.json",
                {
                    "status": "closed",
                    "start_time": "2026-07-04T18:03:36.000-04:00",
                    "last_heartbeat": "2026-07-04T18:04:26.000-04:00",
                    "end_time": "2026-07-04T18:04:26.000-04:00",
                },
            )
            writer = JsonlWriter(session / "heartbeat.jsonl", False)
            writer.write({"timestamp": "2026-07-04T18:03:40.000-04:00", "alive": True})
            writer.write({"timestamp": "2026-07-04T18:04:20.000-04:00", "alive": True})
            writer.close()
            writer = JsonlWriter(session / "gpu_probe_failures.jsonl", False)
            for timestamp in ("2026-07-04T18:03:44.000-04:00", "2026-07-04T18:03:46.000-04:00"):
                writer.write(
                    {
                        "timestamp": timestamp,
                        "responded": False,
                        "error_type": "timeout",
                        "timeout_sec": 1.0,
                        "elapsed_ms": 1000,
                        "stderr": "nvidia-smi timed out",
                    }
                )
            writer.close()
            writer = JsonlWriter(session / "keyboard_state.jsonl", False)
            writer.write({"timestamp": "2026-07-04T18:03:50.000-04:00", "caps_lock": {"toggled": True}})
            writer.close()

            classification = "\n".join(freeze_classification(session))
            self.assertIn("Ignored 2 startup GPU probe blip", classification)
            self.assertNotIn("Display-path stall likely", classification)

    def test_freeze_classifier_scores_clustered_later_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session-20260704-080000"
            session.mkdir()
            atomic_write_json(
                session / "session_info.json",
                {
                    "status": "closed",
                    "start_time": "2026-07-04T08:00:00.000-04:00",
                    "last_heartbeat": "2026-07-04T08:03:00.000-04:00",
                    "end_time": "2026-07-04T08:03:00.000-04:00",
                },
            )
            writer = JsonlWriter(session / "heartbeat.jsonl", False)
            writer.write({"timestamp": "2026-07-04T08:01:10.000-04:00", "alive": True})
            writer.write({"timestamp": "2026-07-04T08:02:50.000-04:00", "alive": True})
            writer.close()
            writer = JsonlWriter(session / "gpu_probe_failures.jsonl", False)
            for timestamp in (
                "2026-07-04T08:01:20.000-04:00",
                "2026-07-04T08:01:25.000-04:00",
                "2026-07-04T08:01:35.000-04:00",
            ):
                writer.write(
                    {
                        "timestamp": timestamp,
                        "responded": False,
                        "error_type": "timeout",
                        "timeout_sec": 2.0,
                        "elapsed_ms": 2000,
                        "stderr": "nvidia-smi timed out",
                    }
                )
            writer.close()
            writer = JsonlWriter(session / "keyboard_state.jsonl", False)
            writer.write({"timestamp": "2026-07-04T08:01:30.000-04:00", "caps_lock": {"toggled": True}})
            writer.close()

            classification = "\n".join(freeze_classification(session))
            self.assertIn("Display-path stall likely", classification)

    def test_cli_marker_writes_to_active_session(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(log_dir=temp)
            session = Path(temp) / "session-active"
            session.mkdir()
            atomic_write_json(session / "session_info.json", {"status": "running"})
            write_active_session(config, session)
            path = add_marker_to_active("caps lock responded", config)
            self.assertEqual(path, session / "markers.jsonl")
            markers = read_jsonl(path)
            self.assertEqual(markers[-1]["note"], "caps lock responded")
            self.assertEqual(markers[-1]["source"], "cli")

    def test_live_kernel_access_denied_is_recorded(self):
        config = Config(live_kernel_paths=[r"C:\Windows\LiveKernelReports"])
        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch(
            "pathlib.Path.iterdir", side_effect=PermissionError("denied")
        ):
            record = collect_live_kernel_reports(config)
        self.assertEqual(record["errors"][0]["error_type"], "access_denied")

    def test_live_kernel_paths_include_registry_and_both_minidump_spellings(self):
        config = Config(live_kernel_paths=[r"C:\Windows\LiveKernelReports", r"C:\Windows\Minidump"])
        with mock.patch("gpu_flight_recorder.collectors.crash_control_minidump_dir", return_value=r"C:\Windows\Minidumps"):
            paths = resolve_live_kernel_paths(config)
        by_path = {item["path"].lower(): item["source"] for item in paths}
        self.assertEqual(by_path[r"c:\windows\minidumps"], "registry")
        self.assertEqual(by_path[r"c:\windows\minidump"], "configured")
        self.assertEqual(len([item for item in paths if item["path"].lower() == r"c:\windows\minidumps"]), 1)

    def test_live_kernel_report_records_path_source(self):
        config = Config(live_kernel_paths=[r"C:\Windows\LiveKernelReports"])
        with mock.patch("gpu_flight_recorder.collectors.crash_control_minidump_dir", return_value=r"C:\Windows\Minidumps"), mock.patch(
            "pathlib.Path.exists", return_value=False
        ):
            record = collect_live_kernel_reports(config)
        sources = {item["path_source"] for item in record["errors"]}
        self.assertIn("configured", sources)
        self.assertIn("registry", sources)
        self.assertIn("fallback", sources)

    def test_heartbeat_continues_when_gpu_probe_is_slow(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(
                log_dir=temp,
                heartbeat_interval_sec=0.2,
                gpu_probe_interval_sec=0.2,
                keyboard_interval_sec=10.0,
                display_interval_sec=10.0,
                process_interval_sec=60.0,
                event_interval_sec=60.0,
                startup_event_scan_minutes=0,
                fsync_each_write=False,
                live_kernel_paths=[str(Path(temp) / "nope")],
            )

            def slow_gpu(_config):
                time.sleep(1.1)
                return {
                    "timestamp": "2026-07-04T08:50:21.000-04:00",
                    "responded": False,
                    "error_type": "timeout",
                    "timed_out": True,
                    "timeout_sec": 1.0,
                    "elapsed_ms": 1100,
                    "stderr": "fake timeout",
                }

            with mock.patch("gpu_flight_recorder.recorder.collect_nvidia_gpu", side_effect=slow_gpu), mock.patch(
                "gpu_flight_recorder.recorder.collect_target_processes",
                return_value={"timestamp": "2026-07-04T08:50:20.000-04:00", "processes": []},
            ), mock.patch(
                "gpu_flight_recorder.recorder.collect_events_since",
                return_value={"timestamp": "2026-07-04T08:50:20.000-04:00", "events": [], "errors": []},
            ):
                recorder = FlightRecorder(config)
                session = recorder.start()
                time.sleep(1.6)
                recorder.stop()

            heartbeats = read_jsonl(session / "heartbeat.jsonl")
            failures = read_jsonl(session / "gpu_probe_failures.jsonl")
            diagnostics = read_jsonl(session / "diagnostics.jsonl")
            self.assertGreaterEqual(len(heartbeats), 4)
            self.assertGreaterEqual(len(failures), 1)
            self.assertTrue(any(record.get("type") == "session_stream_summary" for record in diagnostics))
            summary = next(record for record in diagnostics if record.get("type") == "session_stream_summary")
            self.assertIn("events", summary.get("stream_counts", {}))

    def test_event_worker_startup_scan_uses_history_days(self):
        config = Config(startup_event_history_days=7, event_interval_sec=60.0)
        recorder = FlightRecorder(config)
        captured = {}

        def fake_collect(_config, since, _seen, capture_reason="poll"):
            captured["since"] = since
            captured["capture_reason"] = capture_reason
            recorder._stop_event.set()
            return {"timestamp": "2026-07-04T09:00:00.000-04:00", "events": [], "power_events": [], "errors": []}

        with mock.patch("gpu_flight_recorder.recorder.collect_events_since", side_effect=fake_collect):
            recorder._event_worker()

        from datetime import datetime, timezone

        since = datetime.fromisoformat(captured["since"])
        age_days = (datetime.now(timezone.utc).astimezone() - since).total_seconds() / 86400
        self.assertGreater(age_days, 6.5)
        self.assertLess(age_days, 7.5)
        self.assertEqual(captured["capture_reason"], "startup_scan")

    def test_event_worker_suppresses_repeated_optional_channel_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(log_dir=temp, startup_event_history_days=0, startup_event_scan_minutes=0, event_interval_sec=5)
            recorder = FlightRecorder(config)
            recorder.session_dir = Path(temp) / "session-test"
            recorder.session_dir.mkdir()
            recorder._reset_runtime_state()
            recorder._open_writers()
            calls = {"count": 0}

            def fake_collect(_config, _since, _seen, capture_reason="poll"):
                calls["count"] += 1
                if calls["count"] >= 2:
                    recorder._stop_event.set()
                return {
                    "timestamp": "2026-07-04T09:00:00.000-04:00",
                    "events": [],
                    "power_events": [],
                    "query_health": [],
                    "errors": [
                        {
                            "QueryName": "dwm_core_warnings",
                            "LogName": "Microsoft-Windows-Dwm-Core/Operational",
                            "ProviderName": "",
                            "Severity": "optional",
                            "ErrorType": "Exception",
                            "Message": "channel disabled",
                        }
                    ],
                }

            try:
                with mock.patch("gpu_flight_recorder.recorder.collect_events_since", side_effect=fake_collect), mock.patch.object(
                    recorder._stop_event, "wait", side_effect=[False, True]
                ):
                    recorder._event_worker()
            finally:
                recorder._close_writers()

            diagnostics = read_jsonl(recorder.session_dir / "diagnostics.jsonl")
            channel_errors = [item for item in diagnostics if item.get("type") == "event_channel_error"]
            self.assertEqual(len(channel_errors), 1)


if __name__ == "__main__":
    unittest.main()
