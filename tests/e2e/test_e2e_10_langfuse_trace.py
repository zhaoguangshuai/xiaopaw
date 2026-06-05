"""E2E-10: Langfuse trace full-chain verification.

Covers: L30 (trace -> root span -> GENERATION 3-layer structure, span stack push/pop,
        sessionId association, source metadata)
Tier: T4 (LLM + Langfuse)
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import (
    assert_langfuse_has_generation,
    assert_langfuse_has_observations,
    assert_langfuse_trace,
    assert_langfuse_trace_quality,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.observability,
    pytest.mark.timeout(900),
]

RK = "p2p:ou_lf10"


class TestLangfuseTraceE2E:
    """E2E-10: verify Langfuse trace structure for simple + skill-triggering turns."""

    async def test_simple_conversation_trace(self, llm_client, langfuse_available):
        if not langfuse_available:
            pytest.skip("Langfuse not available")

        result = await send_message(llm_client, "你好", routing_key=RK, timeout=300.0)
        trace = await assert_langfuse_trace_quality(result["trace_id"])

        obs = trace.get("observations", [])
        roots = [o for o in obs if (o.get("name") or "").startswith("session-")]
        assert roots, "No root span starting with 'session-'"

        meta = roots[0].get("metadata", {})
        assert meta.get("source") == "xiaopaw-v2", "Root span missing source=xiaopaw-v2"
        assert roots[0].get("endTime"), "Root span not closed (no endTime)"

        await assert_langfuse_has_generation(result["trace_id"])

    async def test_skill_trigger_trace(self, sandbox_client, langfuse_available, sandbox_url):
        if not langfuse_available:
            pytest.skip("Langfuse not available")

        r1 = await send_message(
            sandbox_client, "你好", routing_key=f"{RK}_skill", timeout=300.0
        )
        r2 = await send_message(
            sandbox_client,
            "帮我搜索 CrewAI 最新版本",
            routing_key=f"{RK}_skill",
            timeout=300.0,
        )

        t1 = await assert_langfuse_trace(r1["trace_id"])
        t2 = await assert_langfuse_trace(r2["trace_id"])
        assert t1.get("id") != t2.get("id"), "Two turns should have different trace_ids"

        if t1.get("sessionId") and t2.get("sessionId"):
            assert t1["sessionId"] == t2["sessionId"], "Same session should share sessionId"

        obs2 = await assert_langfuse_has_observations(r2["trace_id"], min_count=3)
        obs_ids = {o.get("id") for o in obs2}
        for o in obs2:
            parent = o.get("parentObservationId")
            if parent:
                assert parent in obs_ids, (
                    f"Orphan observation: {o.get('name')} parent={parent}"
                )
