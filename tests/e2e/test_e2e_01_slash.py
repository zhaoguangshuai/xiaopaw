"""E2E-01: Slash command full flow.

Covers: L17 (slash command intercept, session lifecycle, SessionManager)
Tier: T0 (No LLM, echo agent)
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import send_message

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.no_llm,
    pytest.mark.timeout(30),
]

RK = "p2p:ou_slash01"


class TestSlashCommandFullFlow:
    """E2E-01: 8-step slash command walkthrough."""

    async def test_help_lists_commands(self, slash_client):
        result = await send_message(slash_client, "/help")
        reply = result["reply"]
        assert "可用命令" in reply
        assert "/new" in reply
        assert "/status" in reply
        assert "/verbose" in reply

    async def test_normal_message_echoes(self, slash_client):
        result = await send_message(slash_client, "hello", routing_key=RK)
        assert "Echo:" in result["reply"] or "hello" in result["reply"]

    async def test_status_shows_session(self, slash_client):
        await send_message(slash_client, "hello", routing_key=RK)
        result = await send_message(slash_client, "/status", routing_key=RK)
        assert "会话 ID" in result["reply"]
        assert "消息数" in result["reply"]

    async def test_verbose_on(self, slash_client):
        result = await send_message(slash_client, "/verbose on", routing_key=RK)
        assert "开启" in result["reply"]

    async def test_verbose_off(self, slash_client):
        result = await send_message(slash_client, "/verbose off", routing_key=RK)
        assert "关闭" in result["reply"]

    async def test_new_creates_session(self, slash_client):
        await send_message(slash_client, "msg", routing_key=RK)
        result = await send_message(slash_client, "/new", routing_key=RK)
        assert "已创建新会话" in result["reply"]

    async def test_new_resets_status(self, slash_client):
        rk = "p2p:ou_slash01_reset"
        await send_message(slash_client, "msg", routing_key=rk)
        r1 = await send_message(slash_client, "/status", routing_key=rk)
        await send_message(slash_client, "/new", routing_key=rk)
        r2 = await send_message(slash_client, "/status", routing_key=rk)
        assert r1["reply"] != r2["reply"]

    async def test_unknown_command_passes_to_agent(self, slash_client):
        result = await send_message(slash_client, "/invalid")
        assert "Echo:" in result["reply"]
