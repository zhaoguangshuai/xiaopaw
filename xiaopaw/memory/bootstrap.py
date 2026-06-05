"""Build Agent backstory from workspace files."""

from __future__ import annotations

import logging
from pathlib import Path

from xiaopaw.memory.config import MEMORY_HARD_LIMIT

logger = logging.getLogger(__name__)

_SECTIONS = [
    ("soul", "soul.md"),
    ("user", "user.md"),
    ("agent", "agent.md"),
    ("memory", "memory.md"),
]


def build_bootstrap_prompt(workspace_dir: Path) -> str:
    parts: list[str] = []
    for tag, filename in _SECTIONS:
        filepath = workspace_dir / filename
        if not filepath.exists():
            continue
        content = filepath.read_text(encoding="utf-8").strip()
        if tag == "memory":
            lines = content.split("\n")
            if len(lines) > MEMORY_HARD_LIMIT:
                content = "\n".join(lines[:MEMORY_HARD_LIMIT])
                logger.warning(
                    "memory.md truncated to %d lines (had %d)",
                    MEMORY_HARD_LIMIT, len(lines),
                )
        parts.append(f"<{tag}>\n{content}\n</{tag}>")
    return "\n\n".join(parts)
