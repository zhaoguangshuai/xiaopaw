"""Persona A: Daily user — real LLM + Langfuse trace verification."""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e.conftest import (
    assert_langfuse_has_generation,
    assert_langfuse_has_observations,
    assert_langfuse_trace,
    assert_langfuse_trace_quality,
    llm_assert,
    send_message,
)

pytestmark = [pytest.mark.e2e]


# ── Slash commands (fast, no LLM — but still through Hook framework) ───


class TestSlashCommands:
    """E2E-A-020~024: slash commands bypass LLM but still exercise routing."""

    @pytest.mark.no_llm
    async def test_help(self, slash_client):
        result = await send_message(slash_client, "/help")
        reply = result["reply"]
        assert "可用命令" in reply
        assert "/new" in reply
        assert "/status" in reply

    @pytest.mark.no_llm
    async def test_status_after_message(self, slash_client):
        rk = "p2p:ou_status_test"
        await send_message(slash_client, "hello", routing_key=rk)
        result = await send_message(slash_client, "/status", routing_key=rk)
        assert "会话 ID" in result["reply"]
        assert "消息数" in result["reply"]

    @pytest.mark.no_llm
    async def test_verbose_on_off(self, slash_client):
        r1 = await send_message(slash_client, "/verbose on")
        assert "开启" in r1["reply"]
        r2 = await send_message(slash_client, "/verbose off")
        assert "关闭" in r2["reply"]

    @pytest.mark.no_llm
    async def test_new_session(self, slash_client):
        rk = "p2p:ou_new_test"
        await send_message(slash_client, "first message", routing_key=rk)
        result = await send_message(slash_client, "/new", routing_key=rk)
        assert "已创建新会话" in result["reply"]

    @pytest.mark.no_llm
    async def test_new_resets_status(self, slash_client):
        rk = "p2p:ou_reset"
        await send_message(slash_client, "msg", routing_key=rk)
        r1 = await send_message(slash_client, "/status", routing_key=rk)
        await send_message(slash_client, "/new", routing_key=rk)
        r2 = await send_message(slash_client, "/status", routing_key=rk)
        assert r1["reply"] != r2["reply"]

    @pytest.mark.no_llm
    async def test_unknown_command_to_agent(self, slash_client):
        result = await send_message(slash_client, "/invalid")
        assert "Echo:" in result["reply"]


# ── Routing isolation ──────────────────────────────────────────────────


class TestRoutingIsolation:

    @pytest.mark.no_llm
    async def test_user_isolation(self, slash_client):
        """E2E-A-040: different users don't share sessions."""
        await send_message(slash_client, "I am Alice", routing_key="p2p:ou_alice")
        r_alice = await send_message(slash_client, "/status", routing_key="p2p:ou_alice")
        r_bob = await send_message(slash_client, "/status", routing_key="p2p:ou_bob")
        assert "消息数" in r_alice["reply"]
        assert "当前无活动会话" in r_bob["reply"]

    @pytest.mark.no_llm
    async def test_parallel_users(self, slash_client):
        """E2E-X-004: concurrent messages to different routing keys."""
        tasks = [
            send_message(slash_client, f"msg {i}", routing_key=f"p2p:ou_par_{i}")
            for i in range(3)
        ]
        results = await asyncio.gather(*tasks)
        for i, r in enumerate(results):
            assert f"msg {i}" in r["reply"]


# ── Edge cases ─────────────────────────────────────────────────────────


class TestEdgeCases:

    @pytest.mark.no_llm
    async def test_empty_message(self, slash_client):
        result = await send_message(slash_client, "")
        assert result["reply"] is not None

    @pytest.mark.no_llm
    async def test_special_characters(self, slash_client):
        msg = "Hello <>&\"' 你好 🎉"
        result = await send_message(slash_client, msg)
        assert msg in result["reply"]

    @pytest.mark.no_llm
    async def test_long_message(self, slash_client):
        result = await send_message(slash_client, "A" * 5000)
        assert "Echo:" in result["reply"]

    @pytest.mark.no_llm
    async def test_sql_injection_blocked_by_sandbox(self, slash_client):
        result = await send_message(slash_client, "'; DROP TABLE sessions; --")
        assert "安全策略拦截" in result["reply"]

    @pytest.mark.no_llm
    async def test_sql_injection_without_shell_chars(self, slash_client):
        result = await send_message(slash_client, "' OR 1=1 --")
        assert "Echo:" in result["reply"]


# ── Real LLM conversation + Langfuse verification ─────────────────────


class TestLLMConversation:
    """E2E-A-001~004: real Qwen LLM with Langfuse trace checks."""

    @pytest.mark.llm_dependent
    async def test_greeting_with_langfuse(self, llm_client, qwen_api_key, langfuse_available):
        """E2E-A-001: greeting produces reply + Langfuse trace."""
        result = await send_message(llm_client, "你好", timeout=300.0)
        reply = result["reply"]
        assert reply and len(reply) > 0
        assert llm_assert(reply, "回复是友好的问候或自我介绍", api_key=qwen_api_key)

        if langfuse_available:
            trace = await assert_langfuse_trace(result["trace_id"])
            await assert_langfuse_trace_quality(result["trace_id"])

    @pytest.mark.llm_dependent
    async def test_simple_question_with_langfuse(self, llm_client, langfuse_available):
        """E2E-A-002: factual Q&A with Langfuse observation."""
        result = await send_message(llm_client, "1+1等于几", timeout=300.0)
        assert "2" in result["reply"]

        if langfuse_available:
            await assert_langfuse_trace(result["trace_id"])
            await assert_langfuse_trace_quality(result["trace_id"])
            await assert_langfuse_has_generation(result["trace_id"])

    @pytest.mark.llm_dependent
    async def test_knowledge_question(self, llm_client, qwen_api_key, langfuse_available):
        """E2E-A-003: knowledge question with Langfuse trace."""
        result = await send_message(llm_client, "用一句话解释什么是RAG", timeout=300.0)
        assert llm_assert(
            result["reply"],
            "回复提到了检索增强生成或Retrieval-Augmented Generation的含义",
            api_key=qwen_api_key,
        )

        if langfuse_available:
            await assert_langfuse_trace_quality(result["trace_id"])


class TestMultiTurnWithLangfuse:
    """E2E-A-010~013: multi-turn context + Langfuse traces for each turn."""

    @pytest.mark.llm_dependent
    async def test_context_retention(self, llm_client, langfuse_available):
        """E2E-A-010: agent remembers prior turn, both turns have Langfuse traces."""
        rk = "p2p:ou_ctx_lf"
        r1 = await send_message(llm_client, "我叫张三", routing_key=rk, timeout=300.0)
        r2 = await send_message(llm_client, "我叫什么名字？", routing_key=rk, timeout=300.0)
        assert "张三" in r2["reply"]

        if langfuse_available:
            await assert_langfuse_trace_quality(r1["trace_id"])
            await assert_langfuse_trace_quality(r2["trace_id"])

    @pytest.mark.llm_dependent
    async def test_new_session_clears_context(self, llm_client, qwen_api_key, langfuse_available):
        """E2E-A-013: /new clears context, each stage has Langfuse trace."""
        rk = "p2p:ou_clear_lf"
        r1 = await send_message(llm_client, "记住密码是ABC123", routing_key=rk, timeout=300.0)
        await send_message(llm_client, "/new", routing_key=rk)
        r3 = await send_message(llm_client, "密码是什么？", routing_key=rk, timeout=300.0)
        assert llm_assert(r3["reply"], "回复表示不知道密码", api_key=qwen_api_key)

        if langfuse_available:
            await assert_langfuse_trace_quality(r1["trace_id"])
            await assert_langfuse_trace_quality(r3["trace_id"])
