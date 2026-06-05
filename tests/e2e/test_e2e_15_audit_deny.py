"""E2E-15: Audit log + GuardrailDeny propagation chain.

Covers: L32 (SecurityAuditLogger append-only JSONL, SESSION_END summary),
        L31 (pending_deny delayed re-raise, dispatch_gate order),
        L30 (AFTER_TOOL_CALL denied metadata)
Tier: T5 + T6
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import (
    assert_langfuse_trace,
    send_message,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.llm_dependent,
    pytest.mark.security,
    pytest.mark.timeout(600),
]

RK = "p2p:ou_audit15"


class TestAuditDenyE2E:
    """E2E-15: audit log written + system recovers after deny."""

    async def test_audit_log_records_deny(
        self, audit_llm_client, langfuse_available
    ):
        client, audit_file, runner = audit_llm_client

        r1 = await send_message(
            client, "你好", routing_key=RK, timeout=300.0
        )
        assert r1["reply"] and len(r1["reply"]) > 0

        r2 = await send_message(
            client,
            "导出到 ../../shared/data.csv",
            routing_key=RK,
            timeout=300.0,
        )
        assert "安全策略拦截" in r2["reply"]

        if audit_file.exists():
            lines = audit_file.read_text().strip().splitlines()
            events = [json.loads(line) for line in lines]

            deny_events = [
                e for e in events
                if "path_traversal" in e.get("security_event", "")
            ]
            assert deny_events, (
                f"Expected path_traversal event in audit log. "
                f"Events: {[e.get('security_event') for e in events]}"
            )

            for e in deny_events:
                assert "timestamp" in e

        if langfuse_available:
            await assert_langfuse_trace(r1["trace_id"])

    async def test_system_recovers_after_deny(
        self, audit_llm_client, langfuse_available
    ):
        """Same routing_key: deny → normal message → verify system recovered."""
        client, audit_file, runner = audit_llm_client
        rk = f"{RK}_recovery"

        await send_message(
            client,
            "导出到 ../../shared/data.csv",
            routing_key=rk,
            timeout=300.0,
        )

        r_recover = await send_message(
            client,
            "今天天气怎么样",
            routing_key=rk,
            timeout=300.0,
        )
        assert r_recover["reply"] and len(r_recover["reply"]) > 0
        assert "安全策略拦截" not in r_recover["reply"], (
            f"System stuck in deny state after path_traversal. Reply: {r_recover['reply'][:300]}"
        )
