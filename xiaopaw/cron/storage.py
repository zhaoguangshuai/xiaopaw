"""Cron job file-based storage with filelock protection."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from xiaopaw.cron.models import CronJob

logger = logging.getLogger(__name__)


class CronStorage:
    def __init__(self, data_dir: Path, filelock_timeout: float = 10.0) -> None:
        self._data_dir = data_dir / "cron"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._tasks_path = self._data_dir / "tasks.json"
        self._dlq_path = self._data_dir / "tasks.dlq.jsonl"
        self._lock_path = self._data_dir / "tasks.json.lock"
        self._filelock_timeout = filelock_timeout

    def _get_lock(self):
        try:
            from filelock import FileLock
            return FileLock(str(self._lock_path), timeout=self._filelock_timeout)
        except ImportError:
            from contextlib import nullcontext
            return nullcontext()

    def load_all(self) -> list[CronJob]:
        with self._get_lock():
            if not self._tasks_path.exists():
                return []
            raw = json.loads(self._tasks_path.read_text(encoding="utf-8"))
            return [CronJob(**j) for j in raw]

    def save_all(self, jobs: list[CronJob]) -> None:
        with self._get_lock():
            data = [j.model_dump() for j in jobs]
            tmp = self._tasks_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(self._tasks_path)

    def append_dlq(self, job: CronJob, error: str) -> None:
        entry = {"job": job.model_dump(), "error": error}
        with self._dlq_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
