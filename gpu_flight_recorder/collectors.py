from __future__ import annotations

import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .io_utils import now_iso, short_text


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCHEMA_VERSION = 2

GPU_QUERY_FIELDS = [
    "timestamp",
    "name",
    "driver_version",
    "kmd_version",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "temperature.gpu",
    "power.draw",
    "power.draw.average",
    "clocks.current.graphics",
    "clocks.current.memory",
    "pstate",
    "pcie.link.gen.gpucurrent",
    "pcie.link.gen.max",
    "pcie.link.width.current",
    "pcie.link.width.max",
    "display_active",
    "display_attached",
]


def record_base(timestamp: str | None = None) -> dict[str, Any]:
    base = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": now_iso(),
        "monotonic_seconds": round(time.monotonic(), 6),
    }
    if timestamp is not None:
        base["timestamp"] = timestamp
    return base


def run_command(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def parse_nvidia_gpu_csv(output: str, fields: list[str] | None = None) -> list[dict[str, str]]:
    keys = fields or GPU_QUERY_FIELDS
    rows: list[dict[str, str]] = []
    for row in csv.reader(output.splitlines()):
        if not row:
            continue
        padded = [cell.strip() for cell in row]
        item = {key.replace(".", "_"): padded[index] if index < len(padded) else "" for index, key in enumerate(keys)}
        rows.append(item)
    return rows


def collect_nvidia_gpu(config: Config) -> dict[str, Any]:
    started = time.perf_counter()
    executable = config.nvidia_smi_path or shutil.which("nvidia-smi") or "nvidia-smi"
    timeout_sec = min(max(float(config.nvidia_smi_timeout_sec), 0.1), 1.0)
    args = [
        executable,
        "--query-gpu=" + ",".join(GPU_QUERY_FIELDS),
        "--format=csv,noheader,nounits",
    ]
    result: dict[str, Any] = {
        "timestamp": now_iso(),
        "executable": executable,
        "responded": False,
        "returncode": None,
        "stderr": "",
        "elapsed_ms": None,
        "timeout_sec": timeout_sec,
        "timed_out": False,
        "error_type": None,
        "gpus": [],
        "primary_gpu": None,
    }
    try:
        completed = run_command(args, timeout=timeout_sec)
        result["returncode"] = completed.returncode
        result["stderr"] = short_text(completed.stderr, 2000)
        result["responded"] = completed.returncode == 0
        if completed.returncode != 0:
            result["error_type"] = "returncode"
        if completed.stdout.strip():
            result["gpus"] = parse_nvidia_gpu_csv(completed.stdout)
            result["primary_gpu"] = result["gpus"][0] if result["gpus"] else None
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error_type"] = "timeout"
        result["stderr"] = f"nvidia-smi timed out after {timeout_sec:.1f}s"
    except FileNotFoundError:
        result["error_type"] = "not_found"
        result["stderr"] = "nvidia-smi was not found on PATH"
    except OSError as exc:
        result["error_type"] = "os_error"
        result["stderr"] = f"nvidia-smi failed to start: {exc}"
    finally:
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def collect_nvidia_processes(config: Config) -> dict[str, Any]:
    executable = config.nvidia_smi_path or shutil.which("nvidia-smi") or "nvidia-smi"
    timeout_sec = min(max(float(config.nvidia_smi_timeout_sec), 0.1), 1.0)
    record: dict[str, Any] = {
        "timestamp": now_iso(),
        "compute_apps": [],
        "pmon": [],
        "errors": [],
    }
    try:
        completed = run_command(
            [
                executable,
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=timeout_sec,
        )
        if completed.returncode == 0:
            for row in csv.reader(completed.stdout.splitlines()):
                if len(row) >= 3:
                    record["compute_apps"].append(
                        {
                            "pid": row[0].strip(),
                            "process_name": row[1].strip(),
                            "used_memory_mib": row[2].strip(),
                        }
                    )
        elif completed.stderr.strip():
            record["errors"].append(short_text(completed.stderr, 1000))
    except Exception as exc:
        record["errors"].append(f"compute-app query failed: {exc}")

    try:
        completed = run_command([executable, "pmon", "-c", "1"], timeout=timeout_sec)
        if completed.returncode == 0:
            for line in completed.stdout.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 10:
                    record["pmon"].append(
                        {
                            "gpu": parts[0],
                            "pid": parts[1],
                            "type": parts[2],
                            "sm_percent": parts[3],
                            "mem_percent": parts[4],
                            "enc_percent": parts[5],
                            "dec_percent": parts[6],
                            "jpg_percent": parts[7],
                            "ofa_percent": parts[8],
                            "command": " ".join(parts[9:]),
                        }
                    )
        elif completed.stderr.strip():
            record["errors"].append(short_text(completed.stderr, 1000))
    except Exception as exc:
        record["errors"].append(f"pmon query failed: {exc}")
    return record


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_int(filetime: FILETIME) -> int:
    return (int(filetime.dwHighDateTime) << 32) + int(filetime.dwLowDateTime)


class CpuTracker:
    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None

    def percent(self) -> float | None:
        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        idle_value = _filetime_to_int(idle)
        total_value = _filetime_to_int(kernel) + _filetime_to_int(user)
        if self._last is None:
            self._last = (idle_value, total_value)
            return None
        last_idle, last_total = self._last
        self._last = (idle_value, total_value)
        total_delta = total_value - last_total
        idle_delta = idle_value - last_idle
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def collect_memory() -> dict[str, Any]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {"error": "GlobalMemoryStatusEx failed"}
    total = int(status.ullTotalPhys)
    free = int(status.ullAvailPhys)
    used = total - free
    return {
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": used,
        "percent_used": int(status.dwMemoryLoad),
    }


def uptime_seconds() -> float:
    try:
        return round(ctypes.windll.kernel32.GetTickCount64() / 1000.0, 3)
    except Exception:
        return round(time.monotonic(), 3)


CCHDEVICENAME = 32
CCHFORMNAME = 32
ENUM_CURRENT_SETTINGS = -1
DISPLAY_DEVICE_ACTIVE = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * CCHDEVICENAME),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * CCHFORMNAME),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


QDC_ONLY_ACTIVE_PATHS = 0x00000002
ERROR_SUCCESS = 0
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", ctypes.c_int),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", wintypes.UINT),
    ]


class DISPLAYCONFIG_2DREGION(ctypes.Structure):
    _fields_ = [("cx", wintypes.UINT), ("cy", wintypes.UINT)]


class DISPLAYCONFIG_VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = [
        ("pixelRate", ctypes.c_uint64),
        ("hSyncFreq", DISPLAYCONFIG_RATIONAL),
        ("vSyncFreq", DISPLAYCONFIG_RATIONAL),
        ("activeSize", DISPLAYCONFIG_2DREGION),
        ("totalSize", DISPLAYCONFIG_2DREGION),
        ("videoStandard", wintypes.UINT),
        ("scanLineOrdering", wintypes.UINT),
    ]


class DISPLAYCONFIG_TARGET_MODE(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", DISPLAYCONFIG_VIDEO_SIGNAL_INFO)]


class POINTL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECTL(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class DISPLAYCONFIG_SOURCE_MODE(ctypes.Structure):
    _fields_ = [
        ("width", wintypes.UINT),
        ("height", wintypes.UINT),
        ("pixelFormat", wintypes.UINT),
        ("position", POINTL),
    ]


class DISPLAYCONFIG_DESKTOP_IMAGE_INFO(ctypes.Structure):
    _fields_ = [
        ("PathSourceSize", POINTL),
        ("DesktopImageRegion", RECTL),
        ("DesktopImageClip", RECTL),
    ]


class DISPLAYCONFIG_MODE_INFO_UNION(ctypes.Union):
    _fields_ = [
        ("targetMode", DISPLAYCONFIG_TARGET_MODE),
        ("sourceMode", DISPLAYCONFIG_SOURCE_MODE),
        ("desktopImageInfo", DISPLAYCONFIG_DESKTOP_IMAGE_INFO),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("infoType", wintypes.UINT),
        ("id", wintypes.UINT),
        ("adapterId", LUID),
        ("modeInfo", DISPLAYCONFIG_MODE_INFO_UNION),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.UINT),
        ("size", wintypes.UINT),
        ("adapterId", LUID),
        ("id", wintypes.UINT),
    ]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", wintypes.UINT),
        ("outputTechnology", ctypes.c_int),
        ("edidManufactureId", wintypes.USHORT),
        ("edidProductCodeId", wintypes.USHORT),
        ("connectorInstance", wintypes.UINT),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
        ("monitorDevicePath", wintypes.WCHAR * 128),
    ]


class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),
        ("colorEncoding", wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
    ]


OUTPUT_TECHNOLOGY_NAMES = {
    -1: "other",
    0: "hd15",
    1: "s-video",
    2: "composite-video",
    3: "component-video",
    4: "dvi",
    5: "hdmi",
    6: "lvds",
    8: "d-jpn",
    9: "sdi",
    10: "displayport_external",
    11: "displayport_embedded",
    12: "udi_external",
    13: "udi_embedded",
    14: "sdtv_dongle",
    15: "miracast",
    16: "indirect_wired",
    17: "indirect_virtual",
    -2147483648: "internal",
}


def _luid_text(luid: LUID) -> str:
    return f"{int(luid.HighPart)}:{int(luid.LowPart)}"


def _rational_hz(value: DISPLAYCONFIG_RATIONAL) -> float | None:
    numerator = int(value.Numerator)
    denominator = int(value.Denominator)
    if numerator <= 0 or denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _output_technology_name(code: int) -> str:
    return OUTPUT_TECHNOLOGY_NAMES.get(int(code), f"unknown_{int(code)}")


def _displayconfig_target_name(adapter_id: LUID, target_id: int) -> dict[str, Any]:
    target_name = DISPLAYCONFIG_TARGET_DEVICE_NAME()
    target_name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
    target_name.header.size = ctypes.sizeof(DISPLAYCONFIG_TARGET_DEVICE_NAME)
    target_name.header.adapterId = adapter_id
    target_name.header.id = target_id
    error = ctypes.windll.user32.DisplayConfigGetDeviceInfo(ctypes.byref(target_name))
    if error != ERROR_SUCCESS:
        return {"available": False, "error": f"DisplayConfigGetDeviceInfo target name failed: {error}"}
    return {
        "available": True,
        "output_technology": _output_technology_name(int(target_name.outputTechnology)),
        "output_technology_code": int(target_name.outputTechnology),
        "edid_manufacturer_id": int(target_name.edidManufactureId),
        "edid_product_code_id": int(target_name.edidProductCodeId),
        "connector_instance": int(target_name.connectorInstance),
        "monitor_friendly_name": target_name.monitorFriendlyDeviceName,
        "monitor_device_path": target_name.monitorDevicePath,
    }


def _advanced_color_info(adapter_id: LUID, target_id: int) -> dict[str, Any]:
    info = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
    info.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
    info.header.size = ctypes.sizeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO)
    info.header.adapterId = adapter_id
    info.header.id = target_id
    try:
        error = ctypes.windll.user32.DisplayConfigGetDeviceInfo(ctypes.byref(info))
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    if error != ERROR_SUCCESS:
        return {"available": False, "error": f"DisplayConfigGetDeviceInfo advanced color failed: {error}"}
    flags = int(info.value)
    return {
        "available": True,
        "advanced_color_supported": bool(flags & 0x1),
        "advanced_color_enabled": bool(flags & 0x2),
        "wide_color_enforced": bool(flags & 0x4),
        "advanced_color_force_disabled": bool(flags & 0x8),
        "color_encoding": int(info.colorEncoding),
        "bits_per_color_channel": int(info.bitsPerColorChannel),
    }


def _query_displayconfig_paths() -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    try:
        path_count = wintypes.UINT()
        mode_count = wintypes.UINT()
        error = ctypes.windll.user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(path_count),
            ctypes.byref(mode_count),
        )
        if error != ERROR_SUCCESS:
            return [], [f"GetDisplayConfigBufferSizes failed: {error}"]

        path_array = (DISPLAYCONFIG_PATH_INFO * int(path_count.value))()
        mode_array = (DISPLAYCONFIG_MODE_INFO * max(1, int(mode_count.value)))()
        error = ctypes.windll.user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(path_count),
            path_array,
            ctypes.byref(mode_count),
            mode_array,
            None,
        )
        if error != ERROR_SUCCESS:
            return [], [f"QueryDisplayConfig failed: {error}"]
    except Exception as exc:
        return [], [f"DisplayConfig unavailable: {exc}"]

    paths: list[dict[str, Any]] = []
    for index in range(int(path_count.value)):
        path = path_array[index]
        target = path.targetInfo
        name_info = _displayconfig_target_name(target.adapterId, int(target.id))
        output_code = int(name_info.get("output_technology_code", target.outputTechnology))
        item: dict[str, Any] = {
            "adapter_luid": _luid_text(target.adapterId),
            "source_id": int(path.sourceInfo.id),
            "target_id": int(target.id),
            "target_available": bool(target.targetAvailable),
            "output_technology": _output_technology_name(output_code),
            "output_technology_code": output_code,
            "refresh_hz": _rational_hz(target.refreshRate),
            "status_flags": int(target.statusFlags),
            "target_name": name_info,
            "advanced_color": _advanced_color_info(target.adapterId, int(target.id)),
        }
        if not name_info.get("available", False):
            errors.append(str(name_info.get("error") or "DisplayConfig target name unavailable"))
        paths.append(item)
    return paths, errors


def _monitor_registry_parts(monitor_device_path: str) -> tuple[str, str] | None:
    match = re.search(r"DISPLAY#([^#\\]+)#([^#\\]+)", monitor_device_path, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1), match.group(2)


def _read_edid_from_registry(monitor_device_path: str) -> bytes | None:
    parts = _monitor_registry_parts(monitor_device_path)
    if not parts:
        return None
    try:
        import winreg

        key_path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[0]}\{parts[1]}\Device Parameters"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "EDID")
        return bytes(value)
    except Exception:
        return None


def parse_edid_native_timing(edid: bytes | bytearray | None) -> dict[str, Any]:
    if not edid or len(edid) < 128:
        return {"available": False, "error": "EDID data unavailable or too short"}
    timings: list[dict[str, Any]] = []
    data = bytes(edid)
    for offset in range(54, 126, 18):
        block = data[offset : offset + 18]
        if len(block) < 18:
            continue
        pixel_clock_10khz = int.from_bytes(block[0:2], "little")
        if pixel_clock_10khz <= 0:
            continue
        h_active = block[2] + ((block[4] & 0xF0) << 4)
        h_blank = block[3] + ((block[4] & 0x0F) << 8)
        v_active = block[5] + ((block[7] & 0xF0) << 4)
        v_blank = block[6] + ((block[7] & 0x0F) << 8)
        total_pixels = (h_active + h_blank) * (v_active + v_blank)
        if h_active <= 0 or v_active <= 0 or total_pixels <= 0:
            continue
        refresh_hz = round((pixel_clock_10khz * 10000) / total_pixels, 3)
        timings.append(
            {
                "width": h_active,
                "height": v_active,
                "refresh_hz": refresh_hz,
                "pixel_clock_mhz": round(pixel_clock_10khz / 100.0, 3),
            }
        )
    if not timings:
        return {"available": False, "error": "No detailed timing descriptors found in base EDID"}
    native = timings[0]
    return {
        "available": True,
        "native_width": native["width"],
        "native_height": native["height"],
        "native_refresh_hz": native["refresh_hz"],
        "detailed_timings": timings,
    }


def collect_display_mode() -> dict[str, Any]:
    devmode = DEVMODEW()
    devmode.dmSize = ctypes.sizeof(DEVMODEW)
    ok = ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(devmode))
    if not ok:
        return {"available": False, "error": "EnumDisplaySettingsW failed"}
    return {
        "available": True,
        "width": int(devmode.dmPelsWidth),
        "height": int(devmode.dmPelsHeight),
        "refresh_hz": int(devmode.dmDisplayFrequency),
        "bits_per_pixel": int(devmode.dmBitsPerPel),
    }


def collect_display_devices() -> dict[str, Any]:
    adapters: list[dict[str, Any]] = []
    primary_name: str | None = None
    index = 0
    while True:
        adapter = DISPLAY_DEVICEW()
        adapter.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        ok = ctypes.windll.user32.EnumDisplayDevicesW(None, index, ctypes.byref(adapter), 0)
        if not ok:
            break
        state_flags = int(adapter.StateFlags)
        adapter_record = {
            "device_name": adapter.DeviceName,
            "device_string": adapter.DeviceString,
            "active": bool(state_flags & DISPLAY_DEVICE_ACTIVE),
            "primary": bool(state_flags & DISPLAY_DEVICE_PRIMARY_DEVICE),
            "mirroring_driver": bool(state_flags & DISPLAY_DEVICE_MIRRORING_DRIVER),
            "state_flags": state_flags,
            "monitors": [],
        }
        if adapter_record["primary"]:
            primary_name = adapter.DeviceString or adapter.DeviceName
        monitor_index = 0
        while True:
            monitor = DISPLAY_DEVICEW()
            monitor.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            monitor_ok = ctypes.windll.user32.EnumDisplayDevicesW(adapter.DeviceName, monitor_index, ctypes.byref(monitor), 0)
            if not monitor_ok:
                break
            monitor_flags = int(monitor.StateFlags)
            adapter_record["monitors"].append(
                {
                    "device_name": monitor.DeviceName,
                    "device_string": monitor.DeviceString,
                    "active": bool(monitor_flags & DISPLAY_DEVICE_ACTIVE),
                    "state_flags": monitor_flags,
                }
            )
            monitor_index += 1
        adapters.append(adapter_record)
        index += 1
    paths, _errors = _query_displayconfig_paths()
    connection_type = paths[0].get("output_technology") if paths else "unavailable"
    return {
        "adapters": adapters,
        "primary_display_name": primary_name,
        "connection_type": connection_type,
        "hdr": "unavailable",
        "metadata_note": "Resolution/refresh and display names use Win32 APIs. Connection type/HDR are best-effort through DisplayConfig.",
    }


def collect_display_topology(active_mode: dict[str, Any] | None = None) -> dict[str, Any]:
    paths, errors = _query_displayconfig_paths()
    active_mode = active_mode or {}
    topology: dict[str, Any] = {
        "available": bool(paths),
        "paths": paths,
        "errors": errors,
        "native_mode": None,
        "below_native_refresh": None,
        "current_output_technology": paths[0].get("output_technology") if paths else None,
        "current_monitor_friendly_name": None,
    }
    best_native: dict[str, Any] | None = None
    for path in paths:
        target_name = path.get("target_name") or {}
        friendly_name = target_name.get("monitor_friendly_name")
        if friendly_name and topology["current_monitor_friendly_name"] is None:
            topology["current_monitor_friendly_name"] = friendly_name
        device_path = str(target_name.get("monitor_device_path") or "")
        edid = _read_edid_from_registry(device_path)
        native = parse_edid_native_timing(edid)
        path["edid_native_timing"] = native
        if native.get("available") and best_native is None:
            best_native = native

    if best_native:
        topology["native_mode"] = {
            "width": best_native.get("native_width"),
            "height": best_native.get("native_height"),
            "refresh_hz": best_native.get("native_refresh_hz"),
        }
        try:
            current_refresh = float(active_mode.get("refresh_hz"))
            native_refresh = float(best_native.get("native_refresh_hz"))
            same_resolution = (
                int(active_mode.get("width") or 0) == int(best_native.get("native_width") or 0)
                and int(active_mode.get("height") or 0) == int(best_native.get("native_height") or 0)
            )
            topology["below_native_refresh"] = bool(same_resolution and native_refresh > current_refresh + 1.0)
        except (TypeError, ValueError):
            topology["below_native_refresh"] = None
    return topology


def collect_display() -> dict[str, Any]:
    mode = collect_display_mode()
    devices = collect_display_devices()
    topology = collect_display_topology(mode)
    return {
        "timestamp": now_iso(),
        "available": bool(mode.get("available")),
        "active_mode": mode,
        "devices": devices,
        "topology": topology,
    }


def collect_keyboard_state() -> dict[str, Any]:
    keys = {
        "caps_lock": 0x14,
        "num_lock": 0x90,
        "scroll_lock": 0x91,
    }
    state: dict[str, Any] = {"timestamp": now_iso(), "available": True}
    try:
        for name, virtual_key in keys.items():
            value = ctypes.windll.user32.GetKeyState(virtual_key)
            state[name] = {
                "toggled": bool(value & 0x0001),
                "down": bool(value & 0x8000),
                "raw": int(value),
            }
    except Exception as exc:
        state = {"timestamp": now_iso(), "available": False, "error": str(exc)}
    return state


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_path_from_pid(pid: int) -> str | None:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return None


def foreground_process() -> dict[str, Any]:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return {"pid": None, "name": None}
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid_int = int(pid.value)
    path = process_path_from_pid(pid_int) if pid_int else None
    return {
        "pid": pid_int or None,
        "name": Path(path).name if path else None,
    }


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def collect_system_sample(cpu_tracker: CpuTracker) -> dict[str, Any]:
    return {
        "timestamp": now_iso(),
        "uptime_seconds": uptime_seconds(),
        "cpu_percent": cpu_tracker.percent(),
        "memory": collect_memory(),
        "display": collect_display(),
        "foreground_process": foreground_process(),
    }


def collect_heartbeat(cpu_tracker: CpuTracker) -> dict[str, Any]:
    return {
        "timestamp": now_iso(),
        "monotonic_seconds": round(time.monotonic(), 6),
        "uptime_seconds": uptime_seconds(),
        "cpu_percent": cpu_tracker.percent(),
        "memory": collect_memory(),
        "foreground_process": foreground_process(),
        "alive": True,
    }


def _powershell_json(script: str, timeout: float = 8.0) -> Any:
    completed = run_command(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(short_text(completed.stderr or completed.stdout, 2000))
    output = completed.stdout.strip()
    if not output:
        return []
    return json.loads(output)


@dataclass
class ProcessCpuState:
    last_seen: dict[int, tuple[float | None, float]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.last_seen is None:
            self.last_seen = {}


def collect_target_processes(config: Config, state: ProcessCpuState) -> dict[str, Any]:
    timestamp = now_iso()
    script = r"""
$items = foreach ($p in Get-Process) {
  $start = $null
  $responding = $null
  try { $start = $p.StartTime.ToString("o") } catch {}
  try { $responding = $p.Responding } catch {}
  [PSCustomObject]@{
    ProcessName = $p.ProcessName
    Id = $p.Id
    CPU = $p.CPU
    WorkingSet64 = $p.WorkingSet64
    PrivateMemorySize64 = $p.PrivateMemorySize64
    StartTime = $start
    Responding = $responding
  }
}
$items | ConvertTo-Json -Depth 4 -Compress
"""
    record: dict[str, Any] = {"timestamp": timestamp, "processes": [], "errors": []}
    try:
        raw = _powershell_json(script, timeout=10.0)
    except Exception as exc:
        record["errors"].append(f"Get-Process query failed: {exc}")
        raw = []
    if isinstance(raw, dict):
        raw = [raw]

    target_names = {name.lower().removesuffix(".exe") for name in config.target_processes}
    now_perf = time.perf_counter()
    cpu_count = os.cpu_count() or 1
    seen_pids: set[int] = set()
    for item in raw:
        process_name = str(item.get("ProcessName") or "")
        if process_name.lower() not in target_names:
            continue
        pid = int(item.get("Id") or 0)
        seen_pids.add(pid)
        cpu_total = item.get("CPU")
        try:
            cpu_total_float = float(cpu_total) if cpu_total is not None else None
        except (TypeError, ValueError):
            cpu_total_float = None
        cpu_percent = None
        previous = state.last_seen.get(pid)
        if previous and cpu_total_float is not None:
            last_cpu, last_time = previous
            if last_cpu is not None and now_perf > last_time:
                cpu_percent = round(max(0.0, (cpu_total_float - last_cpu) / (now_perf - last_time) / cpu_count * 100.0), 1)
        state.last_seen[pid] = (cpu_total_float, now_perf)
        record["processes"].append(
            {
                "name": process_name + ".exe",
                "pid": pid,
                "cpu_percent": cpu_percent,
                "cpu_total_seconds": cpu_total_float,
                "working_set_bytes": item.get("WorkingSet64"),
                "private_memory_bytes": item.get("PrivateMemorySize64"),
                "start_time": item.get("StartTime"),
                "responding": item.get("Responding"),
            }
        )
    for pid in list(state.last_seen):
        if pid not in seen_pids:
            state.last_seen.pop(pid, None)

    record["nvidia_processes"] = {
        "skipped": True,
        "reason": "nvidia-smi is isolated to the GPU probe worker so process polling cannot block proof-of-life logging.",
    }
    return record


IMPORTANT_EVENT_IDS = {13, 14, 17, 18, 19, 41, 42, 46, 47, 1000, 1001, 1002, 107, 109, 117, 141, 142, 4101}
IMPORTANT_EVENT_PROVIDERS = {
    "bugcheck",
    "display",
    "microsoft-windows-dxgkrnl",
    "microsoft-windows-dwm-core",
    "microsoft-windows-kernel-pnp",
    "microsoft-windows-kernel-power",
    "microsoft-windows-power-troubleshooter",
    "microsoft-windows-wer-systemerrorreporting",
    "microsoft-windows-whea-logger",
    "nvlddmkm",
}

POWER_EVENT_MAP = {
    ("microsoft-windows-kernel-power", 41): "unexpected_shutdown",
    ("microsoft-windows-kernel-power", 42): "sleep_enter",
    ("microsoft-windows-kernel-power", 107): "resume",
    ("microsoft-windows-kernel-power", 109): "power_transition",
    ("microsoft-windows-power-troubleshooter", 1): "wake",
}


def _event_id(event: dict[str, Any]) -> int:
    try:
        return int(event.get("Id") or event.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _provider_name(event: dict[str, Any]) -> str:
    return str(event.get("ProviderName") or event.get("provider_name") or "").lower()


def event_matches(event: dict[str, Any], keywords: list[str]) -> bool:
    event_id = _event_id(event)
    provider = _provider_name(event)
    if event_id in IMPORTANT_EVENT_IDS or provider in IMPORTANT_EVENT_PROVIDERS:
        return True
    haystack = " ".join(
        str(event.get(key) or "")
        for key in ("ProviderName", "LogName", "Message", "LevelDisplayName")
    ).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def event_triage_tags(event: dict[str, Any]) -> list[str]:
    event_id = _event_id(event)
    provider = _provider_name(event)
    message = str(event.get("Message") or "")
    tags: list[str] = []
    if provider == "nvlddmkm":
        tags.append("nvidia")
        xid = extract_nvidia_xid(message)
        if xid is not None:
            tags.append(f"xid_{xid}")
    if provider == "microsoft-windows-kernel-power":
        tags.append("power")
    if provider == "microsoft-windows-power-troubleshooter":
        tags.append("wake")
    if event_id == 4101:
        tags.append("tdr")
    if event_id in {17, 18, 19, 46, 47} or "whea" in provider:
        tags.append("whea")
    if event_id == 1001:
        tags.append("bugcheck_or_wer")
    if event_id in {1000, 1002}:
        tags.append("app_hang_or_crash")
    return sorted(set(tags))


def extract_nvidia_xid(message: str) -> int | None:
    match = re.search(r"\bXid\b[^\d]*(\d+)", message, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalize_power_event(event: dict[str, Any]) -> dict[str, Any] | None:
    key = (_provider_name(event), _event_id(event))
    transition = POWER_EVENT_MAP.get(key)
    if transition is None:
        return None
    timestamp = event.get("timestamp") or event.get("TimeCreated") or now_iso()
    return {
        **record_base(str(timestamp)),
        "transition": transition,
        "source_event": {
            "log_name": event.get("LogName"),
            "provider_name": event.get("ProviderName"),
            "event_id": _event_id(event),
            "record_id": event.get("RecordId"),
            "timestamp": timestamp,
        },
        "message": short_text(event.get("Message"), 1000),
    }


def _event_spec(
    query_name: str,
    log_name: str,
    provider_name: str = "",
    ids: list[int] | None = None,
    levels: list[int] | None = None,
    severity: str = "critical",
) -> dict[str, Any]:
    return {
        "QueryName": query_name,
        "LogName": log_name,
        "ProviderName": provider_name,
        "Ids": ids or [],
        "Levels": levels or [],
        "Severity": severity,
    }


def _critical_event_specs() -> list[dict[str, Any]]:
    return [
        _event_spec("kernel_power_transitions", "System", "Microsoft-Windows-Kernel-Power", [41, 42, 107, 109]),
        _event_spec("power_troubleshooter_wake", "System", "Microsoft-Windows-Power-Troubleshooter", [1]),
        _event_spec("display_tdr", "System", "Display", [4101]),
        _event_spec("nvlddmkm", "System", "nvlddmkm"),
        _event_spec("whea_logger", "System", "Microsoft-Windows-WHEA-Logger", [17, 18, 19, 46, 47]),
        _event_spec("system_bugcheck_1001", "System", "", [1001]),
        _event_spec("wer_system_error", "System", "Microsoft-Windows-WER-SystemErrorReporting", [1001]),
        _event_spec("application_hang_crash", "Application", "", [1000, 1002], severity="optional"),
        _event_spec("dxgkrnl_warnings", "Microsoft-Windows-DxgKrnl/Operational", levels=[1, 2, 3], severity="optional"),
        _event_spec("dwm_core_warnings", "Microsoft-Windows-Dwm-Core/Operational", levels=[1, 2, 3], severity="optional"),
        _event_spec("kernel_pnp_watchdog", "Microsoft-Windows-Kernel-PnP/Driver Watchdog", ids=[219], levels=[1, 2, 3], severity="optional"),
        _event_spec("wer_kernel", "Microsoft-Windows-WerKernel/Operational", ids=[1001, 117, 141, 142, 116]),
        _event_spec("kernel_whea_errors", "Microsoft-Windows-Kernel-WHEA/Errors", levels=[1, 2, 3]),
    ]


def build_event_query_specs(config: Config) -> list[dict[str, Any]]:
    specs = _critical_event_specs()
    covered_logs = {str(spec["LogName"]).lower() for spec in specs}
    for log in config.event_logs:
        if log.lower() in covered_logs:
            continue
        specs.append(
            {
                "QueryName": f"configured_{log}",
                "LogName": log,
                "ProviderName": "",
                "Ids": [],
                "Levels": [1, 2, 3],
                "Severity": "optional",
            }
        )
    return specs


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def collect_events_since(config: Config, since_iso: str, seen_keys: set[str], capture_reason: str = "poll") -> dict[str, Any]:
    since = _ps_quote(since_iso)
    specs = _ps_quote(json.dumps(build_event_query_specs(config), separators=(",", ":")))
    script = f"""
$start = [datetime]::Parse({since})
$specs = {specs} | ConvertFrom-Json
$items = @()
$errors = @()
$health = @()
foreach ($spec in @($specs)) {{
  $filter = @{{LogName=[string]$spec.LogName; StartTime=$start}}
  $ids = @($spec.Ids | ForEach-Object {{ [int]$_ }})
  if ($ids.Count -gt 0) {{ $filter.Id = $ids }}
  $levels = @($spec.Levels | ForEach-Object {{ [int]$_ }})
  if ($levels.Count -gt 0) {{ $filter.Level = $levels }}
  if ($null -ne $spec.ProviderName -and [string]$spec.ProviderName -ne '') {{ $filter.ProviderName = [string]$spec.ProviderName }}
  try {{
    $events = @(Get-WinEvent -FilterHashtable $filter -MaxEvents 500 -ErrorAction Stop)
    foreach ($event in $events) {{
      $message = ''
      try {{ $message = $event.Message }} catch {{ $message = '' }}
      $items += [PSCustomObject]@{{
        QueryName = [string]$spec.QueryName
        QuerySeverity = [string]$spec.Severity
        TimeCreated = $event.TimeCreated.ToString("o")
        LogName = $event.LogName
        Id = $event.Id
        RecordId = $event.RecordId
        ProviderName = $event.ProviderName
        LevelDisplayName = $event.LevelDisplayName
        Message = $message
      }}
    }}
    $health += [PSCustomObject]@{{
      QueryName = [string]$spec.QueryName
      LogName = [string]$spec.LogName
      ProviderName = [string]$spec.ProviderName
      Ids = @($ids)
      Levels = @($levels)
      Severity = [string]$spec.Severity
      Success = $true
      EventCount = $events.Count
      ErrorType = $null
      Message = $null
    }}
  }} catch {{
    $message = $_.Exception.Message
    if ($message -like '*No events were found*') {{
      $health += [PSCustomObject]@{{
        QueryName = [string]$spec.QueryName
        LogName = [string]$spec.LogName
        ProviderName = [string]$spec.ProviderName
        Ids = @($ids)
        Levels = @($levels)
        Severity = [string]$spec.Severity
        Success = $true
        EventCount = 0
        ErrorType = $null
        Message = $null
      }}
    }} else {{
      $errors += [PSCustomObject]@{{
        QueryName = [string]$spec.QueryName
        LogName = [string]$spec.LogName
        ProviderName = [string]$spec.ProviderName
        Severity = [string]$spec.Severity
        ErrorType = $_.Exception.GetType().Name
        Message = $message
      }}
      $health += [PSCustomObject]@{{
        QueryName = [string]$spec.QueryName
        LogName = [string]$spec.LogName
        ProviderName = [string]$spec.ProviderName
        Ids = @($ids)
        Levels = @($levels)
        Severity = [string]$spec.Severity
        Success = $false
        EventCount = 0
        ErrorType = $_.Exception.GetType().Name
        Message = $message
      }}
    }}
  }}
}}
[PSCustomObject]@{{
  Events = @($items | Sort-Object TimeCreated)
  Errors = @($errors)
  QueryHealth = @($health)
}} | ConvertTo-Json -Depth 6 -Compress
"""
    record = {**record_base(now_iso()), "capture_reason": capture_reason, "events": [], "power_events": [], "errors": [], "query_health": []}
    try:
        raw = _powershell_json(script, timeout=30.0)
    except Exception as exc:
        record["errors"].append(f"Get-WinEvent query failed: {exc}")
        return record
    raw_events: list[dict[str, Any]]
    if isinstance(raw, dict) and ("Events" in raw or "Errors" in raw or "QueryHealth" in raw):
        raw_events = raw.get("Events") or []
        raw_errors = raw.get("Errors") or []
        raw_health = raw.get("QueryHealth") or []
        if isinstance(raw_events, dict):
            raw_events = [raw_events]
        if isinstance(raw_errors, dict):
            raw_errors = [raw_errors]
        if isinstance(raw_health, dict):
            raw_health = [raw_health]
        record["errors"].extend(raw_errors)
        record["query_health"].extend(raw_health)
    else:
        raw_events = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    captured_counts: dict[str, int] = {}
    for event in raw_events:
        key = f"{event.get('LogName')}:{event.get('RecordId')}:{event.get('Id')}"
        if key in seen_keys:
            continue
        if not event_matches(event, config.event_keywords):
            continue
        event_time = str(event.get("TimeCreated") or now_iso())
        enriched = {
            **record_base(event_time),
            "TimeCreated": event_time,
            "LogName": event.get("LogName"),
            "Id": _event_id(event),
            "RecordId": event.get("RecordId"),
            "ProviderName": event.get("ProviderName"),
            "LevelDisplayName": event.get("LevelDisplayName"),
            "Message": short_text(event.get("Message"), 4000),
            "QueryName": event.get("QueryName"),
            "QuerySeverity": event.get("QuerySeverity"),
            "capture_reason": capture_reason,
            "triage_tags": event_triage_tags(event),
        }
        xid = extract_nvidia_xid(enriched["Message"])
        if xid is not None:
            enriched["nvidia_xid"] = xid
        record["events"].append(enriched)
        power_event = normalize_power_event(enriched)
        if power_event:
            record["power_events"].append(power_event)
        seen_keys.add(key)
        query_name = str(enriched.get("QueryName") or "unknown")
        captured_counts[query_name] = captured_counts.get(query_name, 0) + 1
    for item in record["query_health"]:
        if isinstance(item, dict):
            item["CapturedEventCount"] = captured_counts.get(str(item.get("QueryName") or "unknown"), 0)
    return record


def crash_control_minidump_dir() -> str | None:
    try:
        import winreg

        key_path = r"SYSTEM\CurrentControlSet\Control\CrashControl"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "MinidumpDir")
        return str(value) if value else None
    except Exception:
        return None


def _expanded_windows_path(value: str) -> str:
    expanded = os.path.expandvars(value)
    if expanded.startswith(r"\SystemRoot"):
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        expanded = system_root + expanded[len(r"\SystemRoot") :]
    return str(Path(expanded).expanduser())


def resolve_live_kernel_paths(config: Config) -> list[dict[str, str]]:
    candidates: list[tuple[str, str]] = []
    for raw_path in config.live_kernel_paths:
        candidates.append((_expanded_windows_path(raw_path), "configured"))

    registry_path = crash_control_minidump_dir()
    if registry_path:
        candidates.append((_expanded_windows_path(registry_path), "registry"))

    candidates.extend(
        [
            (r"C:\Windows\Minidump", "fallback"),
            (r"C:\Windows\Minidumps", "fallback"),
        ]
    )

    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for path_text, source in candidates:
        key = path_text.rstrip("\\/").lower()
        if key in seen:
            continue
        seen.add(key)
        resolved.append({"path": path_text, "source": source})
    return resolved


def collect_live_kernel_reports(config: Config) -> dict[str, Any]:
    scanned_at = now_iso()
    resolved_paths = resolve_live_kernel_paths(config)
    record: dict[str, Any] = {"timestamp": scanned_at, "files": [], "errors": [], "scanned_paths": resolved_paths}
    for path_info in resolved_paths:
        folder = Path(path_info["path"])
        source = path_info["source"]
        try:
            if not folder.exists():
                record["errors"].append(
                    {
                        "folder": str(folder),
                        "path_source": source,
                        "error_type": "not_found",
                        "message": "Folder does not exist",
                    }
                )
                continue
            for child in sorted(folder.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)[:100]:
                if not child.is_file():
                    continue
                try:
                    stat = child.stat()
                    record["files"].append(
                        {
                            "folder": str(folder),
                            "path_source": source,
                            "filename": child.name,
                            "modified_time": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="milliseconds"),
                            "size_bytes": stat.st_size,
                        }
                    )
                except OSError as exc:
                    record["errors"].append(
                        {
                            "folder": str(folder),
                            "path_source": source,
                            "filename": child.name,
                            "error_type": exc.__class__.__name__,
                            "message": str(exc),
                        }
                    )
        except PermissionError as exc:
            record["errors"].append(
                {
                    "folder": str(folder),
                    "path_source": source,
                    "error_type": "access_denied",
                    "message": str(exc),
                }
            )
        except OSError as exc:
            record["errors"].append(
                {
                    "folder": str(folder),
                    "path_source": source,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                }
            )
    return record
