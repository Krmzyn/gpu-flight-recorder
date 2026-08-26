import tempfile
import unittest
from tkinter import TclError
from unittest import mock

from gpu_flight_recorder.config import Config
from gpu_flight_recorder.gui import GpuFlightRecorderApp
from gpu_flight_recorder.recorder import FlightRecorder


class GuiTests(unittest.TestCase):
    def test_gui_can_instantiate_without_starting_recorder(self):
        with tempfile.TemporaryDirectory() as temp:
            try:
                app = GpuFlightRecorderApp(recorder=FlightRecorder(Config(log_dir=temp)), auto_recover=False)
            except TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
            try:
                app.update_idletasks()
                self.assertIn("heartbeat_age", app.metric_vars)
                self.assertIn("gpu_probe_age", app.metric_vars)
                self.assertEqual(app.recorder.running, False)
            finally:
                app.destroy()

    def test_gui_prompts_for_admin_relaunch_before_start(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch("gpu_flight_recorder.gui.is_admin", return_value=False), mock.patch(
            "gpu_flight_recorder.gui.messagebox.askyesno", return_value=True
        ), mock.patch("gpu_flight_recorder.gui.relaunch_current_process_as_admin", return_value=True) as relaunch:
            try:
                app = GpuFlightRecorderApp(recorder=FlightRecorder(Config(log_dir=temp)), auto_recover=False)
            except TclError as exc:
                self.skipTest(f"Tk unavailable: {exc}")
            result = app._handle_admin_before_start()
            self.assertFalse(result)
            relaunch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
