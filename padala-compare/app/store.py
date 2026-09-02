"""Tiny JSON-file storage.  No database needed, so it runs on free hosting.

Holds: cached comparisons, cached market summaries, Telegram subscribers,
our own daily rate log, and affiliate click counts.  Writes are atomic
(write to temp file, then rename) and guarded by a process-wide lock.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    # ---- low level ---------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if self._data is None:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._data = {}
            else:
                self._data = {}
        return self._data

    def _flush(self) -> None:
        assert self._data is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, section: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(section, {}).get(key, default)

    def section(self, section: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._load().get(section, {}))

    def put(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(section, {})[key] = value
            self._flush()

    def delete(self, section: str, key: str) -> bool:
        with self._lock:
            data = self._load()
            existed = key in data.get(section, {})
            if existed:
                del data[section][key]
                self._flush()
            return existed

    def increment(self, section: str, key: str, by: int = 1) -> int:
        with self._lock:
            data = self._load()
            bucket = data.setdefault(section, {})
            bucket[key] = int(bucket.get(key, 0)) + by
            self._flush()
            return bucket[key]

    # ---- typed helpers -----------------------------------------------------
    def cache_get(self, key: str, max_age_seconds: int | None) -> tuple[Any | None, bool]:
        """Return (value, is_fresh).  value is None when nothing is cached."""
        entry = self.get("cache", key)
        if not entry:
            return None, False
        saved = entry.get("saved_at", 0)
        age = datetime.now(timezone.utc).timestamp() - float(saved)
        fresh = max_age_seconds is None or age <= max_age_seconds
        return entry.get("value"), fresh

    def cache_put(self, key: str, value: Any) -> None:
        self.put("cache", key, {"saved_at": datetime.now(timezone.utc).timestamp(), "value": value})

    def log_rate(self, currency: str, day: str, rate: float) -> None:
        with self._lock:
            data = self._load()
            data.setdefault("rate_log", {}).setdefault(currency, {})[day] = rate
            # keep two years max
            log = data["rate_log"][currency]
            if len(log) > 800:
                for old in sorted(log)[: len(log) - 800]:
                    del log[old]
            self._flush()

    def rate_log(self, currency: str) -> dict[str, float]:
        return {k: float(v) for k, v in self.get("rate_log", currency, {}).items()}

    # subscribers -------------------------------------------------------------
    def subscribers(self) -> dict[str, dict[str, Any]]:
        return self.section("subscribers")

    def subscriber(self, chat_id: int | str) -> dict[str, Any] | None:
        return self.get("subscribers", str(chat_id))

    def save_subscriber(self, chat_id: int | str, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("since", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.put("subscribers", str(chat_id), record)

    def remove_subscriber(self, chat_id: int | str) -> bool:
        return self.delete("subscribers", str(chat_id))

    # clicks -------------------------------------------------------------------
    def record_click(self, provider_key: str, currency: str) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self.increment("clicks_total", provider_key)
            self.increment("clicks_daily", f"{day}:{provider_key}:{currency}")

    def click_stats(self) -> dict[str, Any]:
        return {"total": self.section("clicks_total"), "daily": self.section("clicks_daily")}
