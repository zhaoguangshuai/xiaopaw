"""UT-CST-001 ~ UT-CST-010: CostGuard unit tests."""

import pytest

from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext

from shared_hooks.cost_guard import CostGuard


def _turn_ctx(input_tokens=0, output_tokens=0, turn=1, model="qwen-plus"):
    return HookContext(
        event_type=EventType.AFTER_TURN,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        turn_number=turn,
        metadata={"model": model},
    )


def _tool_ctx(tool_name="knowledge_search"):
    return HookContext(
        event_type=EventType.BEFORE_TOOL_CALL,
        tool_name=tool_name,
    )


class TestTokenAccumulation:
    def test_cst001_accumulate_tokens(self):
        guard = CostGuard(budget_usd=100.0)
        guard.after_turn_handler(_turn_ctx(input_tokens=100, output_tokens=50))
        guard.after_turn_handler(_turn_ctx(input_tokens=100, output_tokens=50))
        m = guard.get_metrics()
        assert m["total_input_tokens"] == 200
        assert m["total_output_tokens"] == 100

    def test_cst002_cost_calculation(self):
        guard = CostGuard(budget_usd=100.0, model="qwen-plus")
        guard.after_turn_handler(
            _turn_ctx(input_tokens=100_000, output_tokens=50_000)
        )
        m = guard.get_metrics()
        expected = 100_000 * 0.80 / 1_000_000 + 50_000 * 2.00 / 1_000_000
        assert abs(m["estimated_cost_usd"] - expected) < 0.0001

    def test_cst003_unknown_model_fallback(self):
        guard = CostGuard(budget_usd=100.0, model="unknown-model")
        guard.after_turn_handler(
            _turn_ctx(input_tokens=1000, output_tokens=1000, model="unknown-model")
        )
        m = guard.get_metrics()
        assert m["estimated_cost_usd"] > 0


class TestBudgetFence:
    def test_cst004_after_turn_over_budget(self):
        guard = CostGuard(budget_usd=0.0001)
        with pytest.raises(GuardrailDeny, match="budget_exceeded"):
            guard.after_turn_handler(
                _turn_ctx(input_tokens=1_000_000, output_tokens=500_000)
            )

    def test_cst005_before_tool_after_overbudget(self):
        guard = CostGuard(budget_usd=0.0001)
        with pytest.raises(GuardrailDeny):
            guard.after_turn_handler(
                _turn_ctx(input_tokens=1_000_000, output_tokens=500_000)
            )
        with pytest.raises(GuardrailDeny, match="budget_exceeded"):
            guard.before_tool_handler(_tool_ctx())

    def test_cst006_within_budget_passes(self):
        guard = CostGuard(budget_usd=100.0)
        guard.after_turn_handler(_turn_ctx(input_tokens=100, output_tokens=50))
        guard.before_tool_handler(_tool_ctx())


class TestBoundary:
    def test_cst007_zero_budget_immediate_deny(self):
        guard = CostGuard(budget_usd=0.0)
        with pytest.raises(GuardrailDeny):
            guard.before_tool_handler(_tool_ctx())

    def test_cst008_negative_budget_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            CostGuard(budget_usd=-1.0)


class TestMetrics:
    def test_cst009_deny_count(self):
        guard = CostGuard(budget_usd=0.0)
        for _ in range(3):
            with pytest.raises(GuardrailDeny):
                guard.before_tool_handler(_tool_ctx())
        assert guard.get_metrics()["deny_count"] == 3

    def test_cst010_env_var_override(self, monkeypatch):
        monkeypatch.setenv("COST_GUARD_BUDGET", "0.5")
        guard = CostGuard(budget_usd=1.0)
        assert guard._budget == 0.5

    def test_cst011_env_var_invalid_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("COST_GUARD_BUDGET", "unlimited")
        guard = CostGuard(budget_usd=2.0)
        assert guard._budget == 2.0
        captured = capsys.readouterr()
        assert "invalid" in captured.err.lower()
