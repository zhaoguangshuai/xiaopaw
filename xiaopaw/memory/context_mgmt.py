"""Context window management: prune, compress, persist."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from xiaopaw.memory.token_counter import count_tokens

logger = logging.getLogger(__name__)


def prune_tool_results(messages: list[dict], keep_turns: int = 10) -> None:
    """Replace old tool_result content with summaries, in-place."""
    if len(messages) <= keep_turns * 2:
        return
    cutoff = len(messages) - keep_turns * 2
    for i in range(cutoff):
        msg = messages[i]
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if len(content) > 200:
                messages[i] = {**msg, "content": content[:100] + "... [pruned]"}


def maybe_compress(
    messages: list[dict],
    *,
    model_limit: int = 32000,
    fresh_keep_turns: int = 10,
    chunk_tokens: int = 2000,
    compress_threshold: float = 0.45,
    compress_fn=None,
) -> None:
    """Compress old messages when context exceeds threshold, in-place."""
    total = count_tokens(messages)
    if total < model_limit * compress_threshold:
        return

    keep_count = fresh_keep_turns * 2
    if len(messages) <= keep_count:
        return

    split_idx = len(messages) - keep_count
    # Protect tool_calls pairs: scan backward to find complete user boundary
    while split_idx > 0 and messages[split_idx - 1].get("role") != "user":
        split_idx -= 1

    if split_idx <= 0:
        return

    old_messages = messages[:split_idx]
    if compress_fn is None:
        summary = _default_compress(old_messages)
    else:
        summary = compress_fn(old_messages)

    messages[:split_idx] = [{"role": "system", "content": f"[Earlier conversation summary]\n{summary}"}]
    logger.info("compressed %d messages into summary (%d tokens saved)", len(old_messages), total - count_tokens(messages))


def _default_compress(messages: list[dict]) -> str:
    """Simple extractive summary fallback."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            parts.append(f"{role}: {content[:200]}")
    return "\n".join(parts[-10:])


def load_session_ctx(session_id: str, ctx_dir: Path) -> list[dict]:
    ctx_path = ctx_dir / f"{session_id}_ctx.json"
    if not ctx_path.exists():
        return []
    try:
        return json.loads(ctx_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("failed to load ctx for %s", session_id)
        return []


def save_session_ctx(session_id: str, messages: list[dict], ctx_dir: Path) -> None:
    ctx_dir.mkdir(parents=True, exist_ok=True)
    ctx_path = ctx_dir / f"{session_id}_ctx.json"
    tmp = ctx_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    tmp.rename(ctx_path)


def append_session_raw(session_id: str, messages: list[dict], ctx_dir: Path) -> None:
    ctx_dir.mkdir(parents=True, exist_ok=True)
    raw_path = ctx_dir / f"{session_id}_raw.jsonl"
    with raw_path.open("a", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
