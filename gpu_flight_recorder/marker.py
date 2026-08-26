from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .io_utils import JsonlWriter, atomic_write_json, now_iso


def active_session_pointer(config: Config) -> Path:
    return config.log_path / "active_session.json"


def write_active_session(config: Config, session_dir: Path) -> None:
    atomic_write_json(
        active_session_pointer(config),
        {
            "active": True,
            "session_dir": str(session_dir),
            "pid": os.getpid(),
            "updated_at": now_iso(),
        },
    )


def clear_active_session(config: Config, session_dir: Path | None = None) -> None:
    pointer = active_session_pointer(config)
    current: dict[str, Any] = {}
    if pointer.exists():
        try:
            current = json.loads(pointer.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    if session_dir is not None and current.get("session_dir") and Path(current["session_dir"]) != session_dir:
        return
    current.update({"active": False, "updated_at": now_iso()})
    atomic_write_json(pointer, current)


def resolve_active_session(config: Config) -> Path | None:
    pointer = active_session_pointer(config)
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not data.get("active"):
        return None
    session_dir = Path(str(data.get("session_dir", "")))
    if not session_dir.exists():
        return None
    info_path = session_dir / "session_info.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if info.get("status") == "closed":
                return None
        except json.JSONDecodeError:
            pass
    return session_dir


def append_marker(session_dir: Path, note: str, source: str = "cli") -> Path:
    writer = JsonlWriter(session_dir / "markers.jsonl", fsync_each_write=True)
    try:
        writer.write({"timestamp": now_iso(), "note": note.strip(), "source": source})
    finally:
        writer.close()
    return session_dir / "markers.jsonl"


def add_marker_to_active(note: str, config: Config | None = None) -> Path:
    resolved_config = config or load_config()
    session_dir = resolve_active_session(resolved_config)
    if session_dir is None:
        raise RuntimeError("No active GPU Flight Recorder session was found.")
    return append_marker(session_dir, note, source="cli")
