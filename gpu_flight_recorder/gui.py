from __future__ import annotations

import os
import ctypes
import subprocess
import sys
from pathlib import Path

from .tk_bootstrap import ensure_tcl_tk_paths

ensure_tcl_tk_paths()

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .collectors import is_admin
from .config import load_config, write_default_user_config
from .io_utils import parse_iso, safe_float, short_text
from .recorder import FlightRecorder, load_recent_report_text


def relaunch_current_process_as_admin() -> bool:
    params = subprocess.list2cmdline(["-m", "gpu_flight_recorder", *sys.argv[1:]])
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
    return int(result) > 32


class GpuFlightRecorderApp(tk.Tk):
    def __init__(self, recorder: FlightRecorder | None = None, auto_recover: bool = True) -> None:
        super().__init__()
        self.title("GPU Flight Recorder")
        self.geometry("1120x760")
        self.minsize(980, 640)
        self.recorder = recorder or FlightRecorder(load_config())
        self._last_timeline_len = 0
        self._build_style()
        self._build_ui()
        if auto_recover:
            recovered = self.recorder.recover_unclean_sessions()
            if recovered:
                self._set_banner(f"Generated report for {len(recovered)} unclean previous session(s).")
        if not is_admin():
            self._set_banner("Not elevated: dump files and some event channels may be unavailable.")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._refresh)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtle.TLabel", foreground="#5c6470")
        style.configure("MetricTitle.TLabel", foreground="#5c6470", font=("Segoe UI", 9))
        style.configure("MetricValue.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Good.TLabel", foreground="#106b21", font=("Segoe UI", 13, "bold"))
        style.configure("Bad.TLabel", foreground="#b42318", font=("Segoe UI", 13, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(header, text="GPU Flight Recorder", style="Header.TLabel").pack(side=tk.LEFT)
        self.banner_var = tk.StringVar(value="Idle")
        ttk.Label(header, textvariable=self.banner_var, style="Subtle.TLabel").pack(side=tk.RIGHT)

        controls = ttk.Frame(root, padding=(0, 12, 0, 8))
        controls.pack(fill=tk.X)
        self.start_button = ttk.Button(controls, text="Start Recording", style="Accent.TButton", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(controls, text="Stop", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Separator(controls, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)
        ttk.Button(controls, text="Generate Report", command=self._generate_report).pack(side=tk.LEFT)
        ttk.Button(controls, text="Export Support Bundle", command=self._export_bundle).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Open Logs Folder", command=self._open_logs_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Create Config", command=self._create_config).pack(side=tk.RIGHT)

        metrics = ttk.Frame(root)
        metrics.pack(fill=tk.X, pady=(2, 10))
        self.metric_vars: dict[str, tk.StringVar] = {}
        self.metric_widgets: dict[str, ttk.Label] = {}
        metric_defs = [
            ("heartbeat_age", "Heartbeat Age"),
            ("gpu_probe_age", "GPU Probe Age"),
            ("nvidia_health", "nvidia-smi"),
            ("gpu_failures", "GPU Failures"),
            ("gpu_util", "GPU Load"),
            ("gpu_temp", "GPU Temp"),
            ("gpu_power", "GPU Power"),
            ("gpu_memory", "VRAM Used"),
            ("display", "Display"),
            ("display_changes", "Display Changes"),
            ("foreground", "Foreground"),
            ("keyboard_changes", "Keyboard Changes"),
            ("live_kernel", "LiveKernel Scan"),
            ("session", "Session"),
        ]
        for index, (key, title) in enumerate(metric_defs):
            card = ttk.Frame(metrics, padding=8, relief=tk.GROOVE)
            card.grid(row=index // 5, column=index % 5, sticky="ew", padx=4, pady=4)
            metrics.columnconfigure(index % 5, weight=1)
            ttk.Label(card, text=title, style="MetricTitle.TLabel").pack(anchor=tk.W)
            var = tk.StringVar(value="-")
            value = ttk.Label(card, textvariable=var, style="MetricValue.TLabel")
            value.pack(anchor=tk.W)
            self.metric_vars[key] = var
            self.metric_widgets[key] = value

        marker = ttk.LabelFrame(root, text="Markers", padding=10)
        marker.pack(fill=tk.X, pady=(0, 10))
        self.marker_var = tk.StringVar()
        marker_input = ttk.Frame(marker)
        marker_input.pack(fill=tk.X)
        entry = ttk.Entry(marker_input, textvariable=self.marker_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _event: self._add_marker())
        ttk.Button(marker_input, text="Add Marker", command=self._add_marker).pack(side=tk.LEFT, padx=(8, 0))
        quick_markers = ttk.Frame(marker)
        quick_markers.pack(fill=tk.X, pady=(8, 0))
        for note in [
            "display froze / observed",
            "display recovered",
            "caps lock responded",
            "audio continued",
            "driver reset attempted",
            "started Blender",
            "started Fusion",
            "opened Chrome",
            "rebooted",
        ]:
            ttk.Button(quick_markers, text=note, command=lambda value=note: self._quick_marker(value)).pack(side=tk.LEFT, padx=(0, 6), pady=(0, 4))

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True)
        timeline_frame = ttk.Frame(notebook, padding=8)
        report_frame = ttk.Frame(notebook, padding=8)
        notebook.add(timeline_frame, text="Timeline")
        notebook.add(report_frame, text="Report Preview")

        columns = ("time", "type", "detail")
        self.timeline = ttk.Treeview(timeline_frame, columns=columns, show="headings", height=16)
        self.timeline.heading("time", text="Time")
        self.timeline.heading("type", text="Type")
        self.timeline.heading("detail", text="Detail")
        self.timeline.column("time", width=210, anchor=tk.W)
        self.timeline.column("type", width=95, anchor=tk.W)
        self.timeline.column("detail", width=760, anchor=tk.W)
        scroll = ttk.Scrollbar(timeline_frame, orient=tk.VERTICAL, command=self.timeline.yview)
        self.timeline.configure(yscrollcommand=scroll.set)
        self.timeline.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.report_text = tk.Text(report_frame, wrap=tk.WORD, height=16, font=("Consolas", 10), relief=tk.FLAT)
        report_scroll = ttk.Scrollbar(report_frame, orient=tk.VERTICAL, command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        self.report_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        report_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status_var, style="Subtle.TLabel").pack(fill=tk.X, pady=(8, 0))

    def _set_banner(self, value: str) -> None:
        self.banner_var.set(value)
        self.status_var.set(value)

    def _start(self) -> None:
        if not self._handle_admin_before_start():
            return
        try:
            session = self.recorder.start()
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))
            return
        self._set_banner(f"Recording to {session}")

    def _handle_admin_before_start(self) -> bool:
        if is_admin() or not self.recorder.config.admin_relaunch_prompt:
            return True
        should_relaunch = messagebox.askyesno(
            "Run as administrator?",
            "GPU Flight Recorder is not elevated. Run as administrator to capture LiveKernelReports, minidumps, and restricted event channels?\n\nChoose No to continue with reduced coverage.",
        )
        if not should_relaunch:
            self._set_banner("Recording without elevation; reduced coverage will be logged in diagnostics.")
            return True
        try:
            if relaunch_current_process_as_admin():
                self.destroy()
                return False
        except Exception as exc:
            messagebox.showerror("Relaunch failed", str(exc))
            return False
        messagebox.showerror("Relaunch failed", "Windows did not start an elevated recorder process.")
        return False

    def _stop(self) -> None:
        report = self.recorder.stop()
        if report:
            self._load_report(report)
            self._set_banner(f"Stopped. Report generated: {report}")

    def _add_marker(self) -> None:
        note = self.marker_var.get().strip()
        if self.recorder.add_marker(note):
            self.marker_var.set("")
        else:
            self._set_banner("Start recording before adding a marker.")

    def _quick_marker(self, note: str) -> None:
        if not self.recorder.add_marker(note):
            self._set_banner("Start recording before adding a marker.")

    def _generate_report(self) -> None:
        report = self.recorder.generate_report()
        if not report:
            messagebox.showinfo("No session", "Start a recording session first.")
            return
        self._load_report(report)
        self._set_banner(f"Report generated: {report}")

    def _export_bundle(self) -> None:
        bundle = self.recorder.export_support_bundle(include_raw=False)
        if not bundle:
            messagebox.showinfo("No session", "Start a recording session first.")
            return
        self._set_banner(f"Support bundle exported: {bundle}")
        messagebox.showinfo("Support bundle exported", f"Created:\n{bundle}")

    def _open_logs_folder(self) -> None:
        path = self.recorder.config.log_path
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _create_config(self) -> None:
        path = write_default_user_config()
        self._set_banner(f"Config file ready: {path}")
        messagebox.showinfo("Config file", f"Config file ready:\n{path}")

    def _load_report(self, report: Path) -> None:
        text = report.read_text(encoding="utf-8") if report.exists() else ""
        self.report_text.delete("1.0", tk.END)
        self.report_text.insert(tk.END, text)

    def _refresh(self) -> None:
        snapshot = self.recorder.snapshot()
        self.start_button.configure(state=tk.DISABLED if snapshot["running"] else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if snapshot["running"] else tk.DISABLED)
        self.status_var.set(snapshot.get("status_message") or "Ready")
        self._refresh_metrics(snapshot)
        self._refresh_timeline(snapshot)
        self.after(1000, self._refresh)

    def _refresh_metrics(self, snapshot: dict[str, Any]) -> None:
        heartbeat = snapshot.get("latest_heartbeat") or {}
        nvidia = snapshot.get("latest_gpu_sample") or snapshot.get("latest_gpu_failure") or {}
        gpu = nvidia.get("primary_gpu") or {}

        timestamp = parse_iso(heartbeat.get("timestamp"))
        if timestamp:
            import datetime as _dt

            age = (_dt.datetime.now().astimezone() - timestamp).total_seconds()
            self.metric_vars["heartbeat_age"].set(f"{age:.0f}s")
        else:
            self.metric_vars["heartbeat_age"].set("-")

        gpu_timestamp = parse_iso(nvidia.get("timestamp"))
        if gpu_timestamp:
            import datetime as _dt

            age = (_dt.datetime.now().astimezone() - gpu_timestamp).total_seconds()
            self.metric_vars["gpu_probe_age"].set(f"{age:.0f}s")
        else:
            self.metric_vars["gpu_probe_age"].set("-")

        responded = nvidia.get("responded")
        self.metric_vars["nvidia_health"].set("OK" if responded else ("Failed" if responded is False else "-"))
        self.metric_widgets["nvidia_health"].configure(style="Good.TLabel" if responded else "Bad.TLabel" if responded is False else "MetricValue.TLabel")
        self.metric_vars["gpu_failures"].set(str(snapshot.get("gpu_failure_count") or 0))

        self.metric_vars["gpu_util"].set(_with_unit(gpu.get("utilization_gpu"), "%"))
        self.metric_vars["gpu_temp"].set(_with_unit(gpu.get("temperature_gpu"), " C"))
        self.metric_vars["gpu_power"].set(_with_unit(gpu.get("power_draw") or gpu.get("power_draw_average"), " W"))
        mem_used = safe_float(gpu.get("memory_used"))
        mem_total = safe_float(gpu.get("memory_total"))
        if mem_used is not None and mem_total:
            self.metric_vars["gpu_memory"].set(f"{mem_used:.0f}/{mem_total:.0f} MiB")
        else:
            self.metric_vars["gpu_memory"].set("-")

        display = snapshot.get("latest_display_state") or {}
        mode = display.get("active_mode") or display
        if mode.get("available"):
            self.metric_vars["display"].set(f"{mode.get('width')}x{mode.get('height')}@{mode.get('refresh_hz')}")
        else:
            self.metric_vars["display"].set("-")
        self.metric_vars["display_changes"].set(str(snapshot.get("display_change_count") or 0))

        fg = heartbeat.get("foreground_process") or {}
        self.metric_vars["foreground"].set(short_text(fg.get("name") or "-", 24))
        self.metric_vars["keyboard_changes"].set(str(snapshot.get("keyboard_change_count") or 0))
        live_kernel = snapshot.get("latest_live_kernel_scan") or {}
        live_files = len(live_kernel.get("files") or [])
        live_errors = len(live_kernel.get("errors") or [])
        self.metric_vars["live_kernel"].set(f"{live_files} files / {live_errors} notes" if live_kernel else "-")
        session_dir = snapshot.get("session_dir")
        self.metric_vars["session"].set(Path(session_dir).name if session_dir else "-")

    def _refresh_timeline(self, snapshot: dict[str, Any]) -> None:
        timeline = snapshot.get("timeline") or []
        if len(timeline) == self._last_timeline_len:
            return
        self._last_timeline_len = len(timeline)
        self.timeline.delete(*self.timeline.get_children())
        for item in timeline[-200:]:
            self.timeline.insert(
                "",
                tk.END,
                values=(item.get("timestamp", ""), item.get("type", ""), item.get("detail", "")),
            )
        children = self.timeline.get_children()
        if children:
            self.timeline.see(children[-1])
        report_text = load_recent_report_text(Path(snapshot["session_dir"]) if snapshot.get("session_dir") else None)
        if report_text and not self.report_text.get("1.0", "end-1c").strip():
            self.report_text.insert(tk.END, report_text)

    def _on_close(self) -> None:
        if self.recorder.running:
            if not messagebox.askyesno("Stop recording?", "A recording session is running. Stop it and close?"):
                return
            self.recorder.stop()
        self.destroy()


def _with_unit(value: Any, unit: str) -> str:
    numeric = safe_float(value)
    if numeric is None:
        return "-"
    return f"{numeric:.0f}{unit}"


def main() -> None:
    app = GpuFlightRecorderApp()
    app.mainloop()
