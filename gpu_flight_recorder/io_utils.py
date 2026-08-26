from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def now_utc() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def now_iso() -> str:
    return now_utc().isoformat(timespec="milliseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


class JsonlWriter:
    def __init__(self, path: Path, fsync_each_write: bool = True) -> None:
        self.path = path
        self.fsync_each_write = fsync_each_write
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, item: dict[str, Any]) -> None:
        self._handle.write(json.dumps(item, ensure_ascii=False, default=str))
        self._handle.write("\n")
        self._handle.flush()
        if self.fsync_each_write:
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            if self.fsync_each_write:
                os.fsync(self._handle.fileno())
            self._handle.close()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"_parse_error": True, "raw": line[:1000]})
    return records


def write_jsonl_lines(records: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records)


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"[N/A]", "N/A", "None"}:
        return None
    text = text.replace("%", "").replace("W", "").replace("MiB", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def short_text(value: Any, limit: int = 240) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
