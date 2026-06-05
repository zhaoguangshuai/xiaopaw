"""E2E-03: Multi-turn conversation and context lifecycle.

Covers: L8 (Task expected_output), L9 (TaskOutput context), L17 (/new),
        L18 (context governance), L19 (session persistence + reset)
Tier: T1 (real LLM)
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import (
    assert_langfuse_trace,
    assert_langfuse_trace_quality,
    llm_assert,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.timeout(600),
]

RK = "p2p:ou_conv03"


class TestMultiTurnE2E:
    """E2E-03: 5-step multi-turn with /new reset."""

    async def test_greeting(self, llm_client, qwen_api_key, langfuse_available):
        result = await send_message(llm_client, "你好", routing_key=RK, timeout=300.0)
        reply = result["reply"]
        assert reply and len(reply) > 0
        assert llm_assert(reply, "回复是友好的问候", api_key=qwen_api_key)
        if langfuse_available:
            await assert_langfuse_trace(result["trace_id"])

    async def test_context_retained_across_turns(self, llm_client, qwen_api_key, langfuse_available):
        rk = "p2p:ou_conv03_ctx"
        await send_message(llm_client, "你好", routing_key=rk, timeout=300.0)
        await send_message(
            llm_client,
            "我叫张三，我是一名 Python 工程师",
            routing_key=rk,
            timeout=300.0,
        )
        result = await send_message(
            llm_client,
            "我叫什么名字？我是做什么的？",
            routing_key=rk,
            timeout=300.0,
        )
        reply = result["reply"]
        assert "张三" in reply, f"Expected '张三' in reply: {reply[:300]}"
        assert llm_assert(
            reply,
            "回复提到了张三和Python（或编程/工程师）",
            api_key=qwen_api_key,
        )
        if langfuse_available:
            await assert_langfuse_trace_quality(result["trace_id"])

    async def test_new_clears_context(self, llm_client, qwen_api_key, langfuse_available):
        rk = "p2p:ou_conv03_new"
        await send_message(
            llm_client, "我叫张三", routing_key=rk, timeout=300.0
        )
        await send_message(llm_client, "/new", routing_key=rk)
        result = await send_message(
            llm_client, "我叫什么名字？", routing_key=rk, timeout=300.0
        )
        assert llm_assert(
            result["reply"],
            "回复表示不知道或无法确定用户的名字",
            api_key=qwen_api_key,
        )
        if langfuse_available:
            await assert_langfuse_trace_quality(result["trace_id"])
