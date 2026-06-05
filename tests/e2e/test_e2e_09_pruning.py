"""E2E-09: Long conversation pruning and compression.

Covers: L19 (PRUNE_KEEP_TURNS=10 pruning, COMPRESS_THRESHOLD=0.45 compression,
        ctx.json persistence, raw.jsonl audit log)
Tier: T1 (real LLM)
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import llm_assert, send_message

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.timeout(900),
]

RK = "p2p:ou_prune09"

MESSAGES = [
    "3+5等于几",
    "7*8等于几",
    "100-37等于几",
    "15的平方是多少",
    "根号144是多少",
    "地球有几大洲",
    "水的化学式是什么",
    "光速大约是多少",
    "太阳系有几颗行星",
    "人体有多少块骨头",
    "Python 的 list 和 tuple 有什么区别",
    "什么是装饰器",
    "什么是生成器",
    "解释一下 GIL",
    "什么是虚拟环境",
]


class TestPruningE2E:
    """E2E-09: 15 messages then summarize — verify no context overflow."""

    async def test_long_conversation_survives_pruning(
        self, llm_client_with_dirs, qwen_api_key, langfuse_available
    ):
        client, dirs = llm_client_with_dirs

        for msg in MESSAGES:
            r = await send_message(client, msg, routing_key=RK, timeout=300.0)
            assert r["reply"] and len(r["reply"]) > 0, f"Empty reply for: {msg}"

        r_summary = await send_message(
            client, "总结一下我们刚才聊了什么", routing_key=RK, timeout=300.0
        )
        reply = r_summary["reply"]
        assert reply and len(reply) > 10, f"Summary too short: {reply!r}"

        assert llm_assert(
            reply,
            "回复是对之前对话的总结，至少提到了编程相关的话题（如 Python、装饰器、GIL 等）",
            api_key=qwen_api_key,
        ), f"Summary quality check failed. Reply: {reply[:500]}"

        # Verify ctx.json and raw.jsonl persistence (L19)
        ctx_dir = dirs["ctx_dir"]
        session_id = r_summary["session_id"]

        ctx_file = ctx_dir / f"{session_id}_ctx.json"
        assert ctx_file.exists(), f"ctx.json not found at {ctx_file}"
        ctx_data = json.loads(ctx_file.read_text(encoding="utf-8"))
        assert isinstance(ctx_data, list) and len(ctx_data) > 0, (
            f"ctx.json should contain non-empty message list, got {len(ctx_data)} entries"
        )

        raw_file = ctx_dir / f"{session_id}_raw.jsonl"
        assert raw_file.exists(), f"raw.jsonl not found at {raw_file}"
        raw_lines = raw_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(raw_lines) >= 16 * 2, (
            f"raw.jsonl should contain >= 32 entries (16 user + 16 assistant), "
            f"got {len(raw_lines)}"
        )
