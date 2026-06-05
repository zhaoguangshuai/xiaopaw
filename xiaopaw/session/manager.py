"""Session manager: index.json + JSONL per-session storage."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, replace
from pathlib import Path

from xiaopaw.session.models import MessageEntry, RoutingEntry, SessionEntry

logger = logging.getLogger(__name__)

_LRU_MAX = 1000


class _LRULockCache:
    """Bounded LRU cache for per-session asyncio.Lock instances."""

    def __init__(self, maxsize: int = _LRU_MAX) -> None:
        self._maxsize = maxsize
        self._cache: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        if key in self._cache:
            lock = self._cache.pop(key)
            self._cache[key] = lock
            return lock
        if len(self._cache) >= self._maxsize:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        lock = asyncio.Lock()
        self._cache[key] = lock
        return lock


class SessionManager:
    def __init__(self, data_dir: Path, max_active_sessions: int = _LRU_MAX) -> None:
        self._data_dir = Path(data_dir)
        self._sessions_dir = self._data_dir / "sessions"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._sessions_dir / "index.json"
        self._index_lock = asyncio.Lock()
        self._jsonl_locks = _LRULockCache(maxsize=max_active_sessions)
        self._index: dict[str, RoutingEntry] = {}
        self._load_index()

    def _load_index(self) -> None:
        if self._index_path.exists():
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            for rk, entry in raw.items():
                sessions = [SessionEntry(**s) for s in entry.get("sessions", [])]
                self._index[rk] = RoutingEntry(
                    active_session_id=entry["active_session_id"],
                    sessions=sessions,
                )

    def _save_index(self) -> None:
        data = {}
        for rk, entry in self._index.items():
            data[rk] = {
                "active_session_id": entry.active_session_id,
                "sessions": [asdict(s) for s in entry.sessions],
            }
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(self._index_path)

    async def get_or_create(self, routing_key: str) -> SessionEntry:
        async with self._index_lock:
            entry = self._index.get(routing_key)
            if entry and entry.active_session_id:
                for s in entry.sessions:
                    if s.id == entry.active_session_id:
                        return s
            return await self._create_session_locked(routing_key)

    async def create_new_session(self, routing_key: str) -> SessionEntry:
        async with self._index_lock:
            return await self._create_session_locked(routing_key)

    async def _create_session_locked(self, routing_key: str) -> SessionEntry:
        session = SessionEntry()
        entry = self._index.get(routing_key)
        if entry:
            sessions = list(entry.sessions) + [session]
            self._index[routing_key] = RoutingEntry(
                active_session_id=session.id, sessions=sessions
            )
        else:
            self._index[routing_key] = RoutingEntry(
                active_session_id=session.id, sessions=[session]
            )
        self._save_index()
        logger.info("created session %s for %s", session.id, routing_key)
        return session

    async def update_verbose(self, routing_key: str, verbose: bool) -> None:
        async with self._index_lock:
            entry = self._index.get(routing_key)
            if not entry:
                return
            sessions = [
                replace(s, verbose=verbose) if s.id == entry.active_session_id else s
                for s in entry.sessions
            ]
            self._index[routing_key] = replace(entry, sessions=sessions)
            self._save_index()

    async def load_history(
        self, session_id: str, max_turns: int = 20
    ) -> list[MessageEntry]:
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return []

        def _read() -> list[MessageEntry]:
            lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
            entries: list[MessageEntry] = []
            for line in lines:
                if not line.strip():
                    continue
                data = json.loads(line)
                if "role" in data:
                    entries.append(MessageEntry(**data))
            tail = max_turns * 2
            return entries[-tail:] if len(entries) > tail else entries

        return await asyncio.to_thread(_read)

    async def append(
        self,
        session_id: str,
        *,
        user: str,
        feishu_msg_id: str | None = None,
        assistant: str,
        ts: int = 0,
    ) -> None:
        lock = self._jsonl_locks.get(session_id)
        async with lock:
            jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
            user_entry = MessageEntry(
                role="user", content=user, ts=ts, feishu_msg_id=feishu_msg_id
            )
            asst_entry = MessageEntry(role="assistant", content=assistant, ts=ts)

            def _write() -> None:
                with jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(user_entry), ensure_ascii=False) + "\n")
                    f.write(json.dumps(asdict(asst_entry), ensure_ascii=False) + "\n")

            await asyncio.to_thread(_write)

        async with self._index_lock:
            entry = self._index.get("")
            for rk, re_ in self._index.items():
                if re_.active_session_id == session_id:
                    sessions = [
                        replace(s, message_count=s.message_count + 2)
                        if s.id == session_id
                        else s
                        for s in re_.sessions
                    ]
                    self._index[rk] = replace(re_, sessions=sessions)
                    self._save_index()
                    break

    async def clear_all(self) -> None:
        async with self._index_lock:
            self._index.clear()
            self._save_index()

    def get_session_info(self, routing_key: str) -> SessionEntry | None:
        entry = self._index.get(routing_key)
        if not entry:
            return None
        for s in entry.sessions:
            if s.id == entry.active_session_id:
                return s
        return None
