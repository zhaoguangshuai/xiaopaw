"""E2E-07: memory-save write + cross-session recall via Bootstrap.

Covers: L20 (memory-save 4-step admission, Sub-Crew isolated write),
        L16 (SkillLoaderTool progressive disclosure),
        L18 (Bootstrap loads user.md — verified via cross-session recall),
        L22 (three-layer memory integration)
Tier: T2 (LLM + Sandbox)

Architecture: sandbox-docker-compose.yaml mounts ./data/workspace:/workspace:rw.
memory-save writes to /workspace/user.md inside the sandbox, which IS the same
physical file as data/workspace/user.md on the host. After /new, Bootstrap
re-reads user.md and injects the saved memory into the new session's backstory.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import (
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

RK = "p2p:ou_mem07"


class TestMemorySaveE2E:
    """E2E-07: memory-save skill + cross-session recall via Bootstrap."""

    async def test_memory_save_cross_session_recall(
        self, sandbox_client, sandbox_workspace_dir, qwen_api_key, langfuse_available,
    ):
        """Full 4-step flow from design doc: save → verify file → /new → recall."""
        # Step 1: Ask memory-save to persist user info
        r_save = await send_message(
            sandbox_client,
            "请记住：我是一名 Python 后端工程师，偏好 FastAPI 框架，喜欢简洁的回复风格",
            routing_key=RK,
            timeout=300.0,
        )
        assert llm_assert(
            r_save["reply"],
            "回复确认已保存或记住了用户的信息（如'已记住'、'已保存'、'好的我记下了'等）",
            api_key=qwen_api_key,
        ), f"Save confirmation failed. Reply: {r_save['reply'][:500]}"

        # Verify workspace/user.md was written on the host
        user_md = sandbox_workspace_dir / "user.md"
        assert user_md.exists(), f"user.md not found at {user_md}"
        content = user_md.read_text(encoding="utf-8")
        assert any(kw in content for kw in ["Python", "FastAPI", "后端"]), (
            f"user.md missing expected keywords after save. Content:\n{content[:500]}"
        )

        # Step 2: Reset session — clears conversation history
        r_new = await send_message(
            sandbox_client,
            "/new",
            routing_key=RK,
            timeout=60.0,
        )
        assert "新会话" in r_new["reply"], f"/new failed: {r_new['reply']}"

        # Step 3: Cross-session recall — Bootstrap re-reads user.md
        r_recall = await send_message(
            sandbox_client,
            "我是做什么的？我有什么偏好？",
            routing_key=RK,
            timeout=300.0,
        )
        assert llm_assert(
            r_recall["reply"],
            "回复提到了 Python 或 FastAPI（说明从 Bootstrap 重新加载了 user.md 中保存的记忆）",
            api_key=qwen_api_key,
        ), f"Cross-session recall failed. Reply: {r_recall['reply'][:500]}"

        # Step 4: Langfuse verification (save turn)
        if langfuse_available:
            trace = await assert_langfuse_trace(r_save["trace_id"])
            obs = trace.get("observations", [])
            assert len(obs) >= 2, (
                f"Expected >= 2 observations for memory-save flow, got {len(obs)}. "
                f"Names: {[o.get('name') for o in obs]}"
            )
