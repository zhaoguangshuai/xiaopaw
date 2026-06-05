"""E2E-06: Web browsing + code execution skills.

Covers: L14 (MCP protocol, MCPServerHTTP), L15 (headless browser, code interpreter,
        sandbox isolation), L16 (task-type skill)
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

RK = "p2p:ou_browse06"


class TestBrowseAndCodeE2E:
    """E2E-06: web browsing + code execution via sandbox MCP."""

    async def test_browse_example_com(self, sandbox_client, qwen_api_key, langfuse_available):
        result = await send_message(
            sandbox_client,
            "帮我打开 https://example.com 看看页面上写了什么",
            routing_key=RK,
            timeout=300.0,
        )
        reply = result["reply"]
        assert reply and len(reply) > 10
        assert llm_assert(
            reply,
            "回复描述了 example.com 页面的内容（如提到 'Example Domain' 或 'illustrative examples'）",
            api_key=qwen_api_key,
        ), f"LLM-as-Judge failed. Reply: {reply[:500]}"
        if langfuse_available:
            await assert_langfuse_trace(result["trace_id"])

    async def test_code_execution(self, sandbox_client, qwen_api_key, langfuse_available):
        result = await send_message(
            sandbox_client,
            "用 Python 代码计算 2 的 20 次方，告诉我结果",
            routing_key=f"{RK}_code",
            timeout=300.0,
        )
        reply = result["reply"]
        assert "1048576" in reply or "1,048,576" in reply, (
            f"Expected '1048576' or '1,048,576' in reply: {reply[:500]}"
        )
        if langfuse_available:
            await assert_langfuse_trace(result["trace_id"])
            await assert_langfuse_has_observations(result["trace_id"], min_count=2)
