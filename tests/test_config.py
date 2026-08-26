import json
import tempfile
import unittest
from pathlib import Path

from gpu_flight_recorder.config import Config, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults_are_usable(self):
        config = Config()
        self.assertGreater(config.sample_interval_sec, 0)
        self.assertIn("blender.exe", config.target_processes)
        self.assertIn("System", config.event_logs)
        self.assertEqual(config.startup_event_history_days, 30)
        self.assertTrue(config.admin_relaunch_prompt)
        self.assertEqual(config.freeze_classifier_warmup_seconds, 60.0)
        self.assertEqual(config.freeze_classifier_cluster_min_failures, 3)

    def test_load_config_overrides_known_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"sample_interval_sec": 1.5, "unknown": "ignored"}), encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config.sample_interval_sec, 1.5)
        self.assertFalse(hasattr(config, "unknown"))

    def test_redacted_log_path_uses_profile_token(self):
        data = Config().redacted_dict()
        self.assertIn("log_dir", data)
        self.assertTrue(data["log_dir"])


if __name__ == "__main__":
    unittest.main()
