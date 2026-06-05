"""CleanupService: scheduled cleanup of expired sessions, traces, and raw logs."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(
        self,
        data_dir: Path,
        session_ttl_days: int = 180,
        trace_ttl_days: int = 30,
        raw_ttl_days: int = 30,
        run_hour_utc: int = 3,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._session_ttl = session_ttl_days * 86400
        self._trace_ttl = trace_ttl_days * 86400
        self._raw_ttl = raw_ttl_days * 86400
        self._run_hour = run_hour_utc
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cleanup-service")
        logger.info("cleanup service started (run_hour_utc=%d)", self._run_hour)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            if now.hour == self._run_hour:
                try:
                    await asyncio.to_thread(self._cleanup)
                except Exception:
                    logger.exception("cleanup failed")
                await asyncio.sleep(3600)
            else:
                await asyncio.sleep(300)

    def _cleanup(self) -> None:
        now = time.time()
        cleaned = 0

        # Clean raw JSONL files
        ctx_dir = self._data_dir / "ctx"
        if ctx_dir.exists():
            for f in ctx_dir.glob("*_raw.jsonl"):
                if now - f.stat().st_mtime > self._raw_ttl:
                    f.unlink()
                    cleaned += 1

        # Clean trace directories
        traces_dir = self._data_dir / "traces"
        if traces_dir.exists():
            for d in traces_dir.iterdir():
                if d.is_dir() and now - d.stat().st_mtime > self._trace_ttl:
                    import shutil
                    shutil.rmtree(d)
                    cleaned += 1

        if cleaned:
            logger.info("cleanup removed %d expired items", cleaned)
