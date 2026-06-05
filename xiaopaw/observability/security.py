"""Security utilities: rate limiting, replay cache, memory content safety."""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict


class RateLimiter:
    """Per-user sliding-window rate limiter."""

    def __init__(self, per_user_per_minute: int = 20) -> None:
        self._limit = per_user_per_minute
        self._windows: dict[str, list[float]] = {}

    def allow(self, user_key: str) -> bool:
        now = time.monotonic()
        window = self._windows.setdefault(user_key, [])
        cutoff = now - 60.0
        self._windows[user_key] = [t for t in window if t > cutoff]
        if len(self._windows[user_key]) >= self._limit:
            return False
        self._windows[user_key].append(now)
        return True


BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<\s*system\s*>", re.IGNORECASE),
    re.compile(r"(忽略|ignore)\s*(之前|previous|所有|all)", re.IGNORECASE),
    re.compile(r"(你现在是|pretend\s+you\s+are)", re.IGNORECASE),
    re.compile(r"```\s*system", re.IGNORECASE),
    re.compile(r"\[SYSTEM\]", re.IGNORECASE),
]


def is_safe_memory_content(text: str, max_length: int = 2000) -> bool:
    if len(text) > max_length:
        return False
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(text):
            return False
    return True


class ReplayCache:
    """Event-ID dedup with LRU eviction and TTL."""

    def __init__(self, maxsize: int = 10000, ttl_sec: float = 300.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_sec
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def seen(self, event_id: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            # evict expired
            expired = [k for k, t in self._cache.items() if now - t > self._ttl]
            for k in expired:
                del self._cache[k]

            if event_id in self._cache:
                self._cache.move_to_end(event_id)
                return True

            if len(self._cache) >= self._maxsize:
                self._cache.popitem(last=False)

            self._cache[event_id] = now
            return False
