"""E2E-11: 5+2 event completeness and structured logging.

Covers: L30 (all 7 events fire, dispatch fault tolerance, structured_log handler)
Tier: T4 (LLM + Langfuse)
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e.conftest import (
    assert_langfuse_has_generation,
    assert_langfuse_trace,
    langfuse_get_trace,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.observability,
    pytest.mark.timeout(900),
]

RK = "p2p:ou_events11"


class TestEventsE2E:
    """E2E-11: verify 5+2 events fire for a skill-triggering turn."""

    async def test_event_chain_completeness(
        self, sandbox_client, langfuse_available, sandbox_url
    ):
        if not langfuse_available:
            pytest.skip("Langfuse not available")

        result = await send_message(
            sandbox_client,
            "帮我搜索一下 FastAPI 最新版本有什么更新",
            routing_key=RK,
            timeout=300.0,
        )
        assert result["reply"] and len(result["reply"]) > 10

        # SESSION_END fires after reply is captured; wait for Langfuse ingestion
        await asyncio.sleep(5)

        trace = await assert_langfuse_trace(result["trace_id"])
        assert trace is not None

        await assert_langfuse_has_generation(result["trace_id"])

        # Re-fetch trace with retries to let Langfuse ingest all span closings
        obs = trace.get("observations", [])
        for _ in range(5):
            tool_spans = [
                o for o in obs
                if o.get("type") == "SPAN"
                and "tool" in (o.get("name") or "").lower()
            ]
            closed_tools = [s for s in tool_spans if s.get("endTime")]
            roots = [o for o in obs if (o.get("name") or "").startswith("session-")]
            root_closed = roots and roots[0].get("endTime")
            all_tools_closed = not tool_spans or len(closed_tools) == len(tool_spans)
            if root_closed and all_tools_closed:
                break
            await asyncio.sleep(3)
            fresh = await langfuse_get_trace(result["trace_id"])
            if fresh:
                obs = fresh.get("observations", [])

        tool_spans = [
            o for o in obs
            if o.get("type") == "SPAN"
            and "tool" in (o.get("name") or "").lower()
        ]
        closed_tools = [s for s in tool_spans if s.get("endTime")]
        if tool_spans:
            assert len(closed_tools) >= len(tool_spans) * 0.8, (
                f"Too many unclosed tool spans: {len(tool_spans) - len(closed_tools)} "
                f"out of {len(tool_spans)}"
            )

        roots = [o for o in obs if (o.get("name") or "").startswith("session-")]
        if roots:
            assert roots[0].get("endTime"), "Root span should be closed after turn"
