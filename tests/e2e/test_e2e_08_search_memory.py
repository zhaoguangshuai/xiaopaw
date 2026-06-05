"""E2E-08: search_memory semantic search + degradation.

Covers: L21 (pgvector hybrid search vector*0.7 + fulltext*0.3, 3-level degradation),
        L22 (search_memory skill, async background indexing)
Tier: T3 (LLM + Sandbox + pgvector)
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.e2e.conftest import (
    assert_langfuse_trace,
    llm_assert,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.sandbox_required,
    pytest.mark.pgvector_required,
    pytest.mark.skipif(not os.environ.get("PGVECTOR_URL"), reason="PGVECTOR_URL not set"),
    pytest.mark.timeout(600),
]

RK = "p2p:ou_vecmem08"


class TestSearchMemoryE2E:
    """E2E-08: semantic search over conversation history."""

    async def test_semantic_recall_and_degradation(
        self, sandbox_client, qwen_api_key, langfuse_available
    ):
        conversations = [
            "我最近在研究 LangGraph 框架，觉得它的状态管理设计很有意思",
            "跟 CrewAI 比起来，LangGraph 更适合复杂的工作流编排",
            "但 CrewAI 的 agent 定义方式更直观",
        ]
        for msg in conversations:
            await send_message(
                sandbox_client, msg, routing_key=RK, timeout=300.0
            )

        await asyncio.sleep(5)

        await send_message(sandbox_client, "/new", routing_key=RK)

        r_recall = await send_message(
            sandbox_client,
            "我之前提到过哪些 AI 框架？我对它们有什么看法？",
            routing_key=RK,
            timeout=300.0,
        )
        assert llm_assert(
            r_recall["reply"],
            "回复提到了 LangGraph 和 CrewAI 的对比观点",
            api_key=qwen_api_key,
        ), f"Semantic recall failed. Reply: {r_recall['reply'][:500]}"

        r_miss = await send_message(
            sandbox_client,
            "帮我找一下去年关于投资策略的讨论",
            routing_key=RK,
            timeout=300.0,
        )
        assert llm_assert(
            r_miss["reply"],
            "回复表示未找到相关记忆或没有相关讨论记录",
            api_key=qwen_api_key,
        ), f"Degradation failed. Reply: {r_miss['reply'][:500]}"

        if langfuse_available:
            await assert_langfuse_trace(r_recall["trace_id"])
