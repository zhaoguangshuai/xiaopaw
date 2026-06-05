"""CronService: scheduled task execution."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from xiaopaw.cron.models import CronJob
from xiaopaw.cron.storage import CronStorage
from xiaopaw.models import InboundMessage
from xiaopaw.observability.metrics import cron_dlq_total

logger = logging.getLogger(__name__)


class CronService:
    def __init__(
        self,
        storage: CronStorage,
        dispatch_fn: Callable[[InboundMessage], Awaitable[None]],
        check_interval: float = 30.0,
    ) -> None:
        self._storage = storage
        self._dispatch = dispatch_fn
        self._interval = check_interval
        self._main_task: asyncio.Task | None = None
        self._last_run: dict[str, float] = {}

    async def start(self) -> None:
        self._main_task = asyncio.create_task(self._loop(), name="cron-service")
        logger.info("cron service started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            logger.info("cron service stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_and_dispatch()
            except Exception:
                logger.exception("cron check error")
            await asyncio.sleep(self._interval)

    async def _check_and_dispatch(self) -> None:
        jobs = await asyncio.to_thread(self._storage.load_all)
        now = time.time()

        for job in jobs:
            if not job.enabled:
                continue

            if self._should_run(job, now):
                await self._run_job(job)
                self._last_run[job.id] = now

    def _should_run(self, job: CronJob, now: float) -> bool:
        try:
            from croniter import croniter
            cron = croniter(job.cron_expr, now - self._interval)
            next_time = cron.get_next(float)
            return next_time <= now
        except Exception:
            logger.warning("invalid cron expression: %s (job %s)", job.cron_expr, job.id)
            return False

    async def _run_job(self, job: CronJob) -> None:
        trace_id = f"cron-{job.id}-{uuid.uuid4().hex[:8]}"
        inbound = InboundMessage(
            routing_key=job.routing_key,
            content=job.content,
            msg_id=f"cron_{job.id}_{int(time.time())}",
            sender_id="cron",
            ts=int(time.time() * 1000),
            is_cron=True,
            trace_id=trace_id,
        )

        try:
            await self._dispatch(inbound)
            logger.info("cron job %s dispatched", job.id)
        except Exception as exc:
            logger.exception("cron job %s failed", job.id)
            job_updated = job.model_copy(update={"fail_count": job.fail_count + 1})
            if job_updated.fail_count >= job.max_retries:
                self._storage.append_dlq(job_updated, str(exc))
                cron_dlq_total.inc()
                logger.warning("cron job %s moved to DLQ", job.id)
