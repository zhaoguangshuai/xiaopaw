"""E2E-05: Search skill end-to-end.

Covers: L12 (Function Calling), L13 (BaseTool args_schema),
        L16 (SkillLoaderTool progressive disclosure -> Sub-Crew),
        L17 (Main -> Sub two-layer architecture)
Tier: T2 (LLM + Sandbox)
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import (
    assert_langfuse_has_observations,
    assert_langfuse_trace,
    llm_assert,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.sandbox_required,
    pytest.mark.timeout(900),
]


class TestSearchSkillE2E:
    """E2E-05: Agent -> SkillLoaderTool -> Sub-Crew -> Sandbox MCP -> search results."""

    async def test_search_returns_real_results(
        self, sandbox_client, qwen_api_key, langfuse_available
    ):
        result = await send_message(
            sandbox_client,
            "帮我搜索一下 Python 3.13 有什么新特性",
            routing_key="p2p:ou_search",
            timeout=550.0,
        )
        reply = result["reply"]
        assert reply and len(reply) > 10, f"Reply too short: {reply!r}"

        assert llm_assert(
            reply,
            "回复与 Python 3.13 相关，且包含具体信息（如 PEP 编号、特性名称、发布计划等），不是纯粹的'我不知道'",
            api_key=qwen_api_key,
        ), f"LLM-as-Judge failed. Reply: {reply[:500]}"

        if langfuse_available:
            await assert_langfuse_trace(result["trace_id"])
            obs = await assert_langfuse_has_observations(result["trace_id"], min_count=3)
            tool_spans = [
                o for o in obs
                if "skill" in (o.get("name") or "").lower()
                or "baidu_search" in (o.get("name") or "").lower()
                or (o.get("type") == "SPAN" and "tool" in (o.get("name") or "").lower())
            ]
            assert tool_spans, (
                f"No tool span found for skill/baidu_search in trace {result['trace_id']}. "
                f"Observation names: {[o.get('name') for o in obs]}"
            )
