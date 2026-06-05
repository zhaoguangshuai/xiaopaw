"""E2E-04: Bootstrap persona and user profile loading.

Covers: L18 (Bootstrap 4-file preload, <soul>/<user> XML injection),
        L22 (build_bootstrap_prompt)
Tier: T1 (real LLM)
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import (
    assert_langfuse_has_generation,
    assert_langfuse_trace,
    llm_assert,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.timeout(600),
]

RK = "p2p:ou_boot04"


class TestBootstrapE2E:
    """E2E-04: identity, facts, and knowledge via Bootstrap."""

    async def test_identity_from_soul_md(self, llm_client, qwen_api_key, langfuse_available):
        result = await send_message(
            llm_client,
            "你是谁？你叫什么名字？介绍一下你自己",
            routing_key=RK,
            timeout=300.0,
        )
        reply = result["reply"]
        assert llm_assert(
            reply,
            "回复中提到了自己的身份信息（如名字、角色定位），表现出有明确的人设",
            api_key=qwen_api_key,
        )
        if langfuse_available:
            await assert_langfuse_trace(result["trace_id"])

    async def test_simple_fact(self, llm_client, langfuse_available):
        result = await send_message(
            llm_client, "1+1等于几", routing_key=RK, timeout=300.0
        )
        assert "2" in result["reply"]
        if langfuse_available:
            await assert_langfuse_has_generation(result["trace_id"])

    async def test_knowledge_question(self, llm_client, qwen_api_key, langfuse_available):
        result = await send_message(
            llm_client,
            "用一句话解释什么是 RAG",
            routing_key=RK,
            timeout=300.0,
        )
        assert llm_assert(
            result["reply"],
            "回复提到了检索增强生成或 Retrieval-Augmented Generation",
            api_key=qwen_api_key,
        )
        if langfuse_available:
            await assert_langfuse_has_generation(result["trace_id"])
