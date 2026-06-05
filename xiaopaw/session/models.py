"""Session data models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _new_session_id() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageEntry:
    role: str  # "user" | "assistant"
    content: str
    ts: int  # milliseconds
    feishu_msg_id: str | None = None


@dataclass(frozen=True)
class SessionEntry:
    id: str = field(default_factory=_new_session_id)
    created_at: str = field(default_factory=_now_iso)
    verbose: bool = False
    message_count: int = 0


@dataclass(frozen=True)
class RoutingEntry:
    active_session_id: str = ""
    sessions: list[SessionEntry] = field(default_factory=list)
