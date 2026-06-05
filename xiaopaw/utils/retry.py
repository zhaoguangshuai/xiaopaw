"""Generic async retry with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


async def async_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    retry_on: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except retry_on as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "retry %d/%d for %s: %s (backoff %.1fs)",
                    attempt + 1, max_retries, fn.__name__, exc, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
