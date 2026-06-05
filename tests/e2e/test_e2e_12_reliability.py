"""E2E-12: Loop detection and cost guard.

Covers: L31 (LoopDetector MD5 state hash threshold=3, CostGuard real-time billing +
        budget threshold, dispatch_gate execution order, GuardrailDeny propagation)
Tier: T5 (LLM)
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import send_message

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.timeout(600),
]


class TestReliabilityE2E:
    """E2E-12: loop detection + cost guard budget enforcement."""

    async def test_agent_does_not_infinite_loop(self, llm_client, langfuse_available):
        result = await send_message(
            llm_client,
            "反复搜索直到找到一个不存在的网站 nonexistent-site-xyz-12345.invalid 的内容",
            routing_key="p2p:ou_loop12",
            timeout=300.0,
        )
        assert result["reply"] and len(result["reply"]) > 0, "Agent returned empty reply"

    async def test_zero_budget_blocks_immediately(self, budget_llm_client, langfuse_available):
        result = await send_message(
            budget_llm_client,
            "你好",
            routing_key="p2p:ou_cost12",
            timeout=300.0,
        )
        reply = result["reply"]
        budget_blocked = (
            "安全策略拦截" in reply
            or "Budget" in reply
            or "budget" in reply
            or "预算" in reply
            or "BUDGET_EXCEEDED" in reply
        )
        assert budget_blocked, (
            f"Expected budget block with zero budget, got: {reply[:300]}"
        )
