"""E2E-02: Routing isolation and concurrency safety.

Covers: L17 (per-routing_key serial queue, workspace isolation),
        L12 (contextvars isolation)
Tier: T0 (No LLM)
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e.conftest import send_message

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.no_llm,
    pytest.mark.timeout(30),
]


class TestRoutingIsolationE2E:
    """E2E-02: cross-user isolation + parallel safety."""

    async def test_alice_has_session_bob_does_not(self, slash_client):
        await send_message(slash_client, "I am Alice", routing_key="p2p:ou_alice02")
        r_alice = await send_message(slash_client, "/status", routing_key="p2p:ou_alice02")
        r_bob = await send_message(slash_client, "/status", routing_key="p2p:ou_bob02")
        assert "消息数" in r_alice["reply"]
        assert "当前无活动会话" in r_bob["reply"]

    async def test_concurrent_messages_no_crosstalk(self, slash_client):
        tasks = [
            send_message(slash_client, f"msg {i}", routing_key=f"p2p:ou_par_{i}")
            for i in range(3)
        ]
        results = await asyncio.gather(*tasks)
        for i, r in enumerate(results):
            assert f"msg {i}" in r["reply"], (
                f"Expected 'msg {i}' in reply, got: {r['reply']!r}"
            )
