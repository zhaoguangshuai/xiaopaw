"""ED-FRZ, ED-CON, ED-SBX, ED-LPD, ED-CST, ED-LDR: Edge case and boundary tests."""

import time
from unittest.mock import MagicMock

import pytest

from xiaopaw.hook_framework.crew_adapter import CrewObservabilityAdapter
from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import (
    DenyReason,
    EventType,
    GuardrailDeny,
    HookContext,
    HookRegistry,
)

from shared_hooks.cost_guard import CostGuard
from shared_hooks.loop_detector import LoopDetector
from shared_hooks.sandbox_guard import SandboxGuard


# ── HookContext frozen ───────────────────────────────────────────────


class TestFrozen:
    def test_frz001_immutable_fields(self):
        ctx = HookContext(event_type=EventType.BEFORE_TURN, tool_name="test")
        with pytest.raises(AttributeError):
            ctx.tool_name = "new"

    def test_frz002_mutable_defaults_isolated(self):
        ctx1 = HookContext(event_type=EventType.BEFORE_TURN)
        ctx2 = HookContext(event_type=EventType.BEFORE_TURN)
        assert ctx1.metadata is not ctx2.metadata
        assert ctx1.tool_input is not ctx2.tool_input

    def test_frz003_dict_fields_immutable(self):
        ctx = HookContext(
            event_type=EventType.BEFORE_TURN,
            tool_input={"key": "value"},
            metadata={"m": 1},
        )
        with pytest.raises(TypeError):
            ctx.tool_input["new_key"] = "evil"
        with pytest.raises(TypeError):
            ctx.metadata["injected"] = "evil"


# ── Concurrency isolation ────────────────────────────────────────────


class TestConcurrency:
    def test_con001_registry_instance_isolation(self):
        r1 = HookRegistry()
        r2 = HookRegistry()
        from unittest.mock import MagicMock
        h1 = MagicMock()
        h2 = MagicMock()
        r1.register(EventType.BEFORE_TURN, h1)
        r2.register(EventType.BEFORE_TURN, h2)
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        r1.dispatch(EventType.BEFORE_TURN, ctx)
        h1.assert_called_once()
        h2.assert_not_called()


# ── SandboxGuard regex edge cases ────────────────────────────────────


class TestSandboxEdge:
    def test_sbx_ed001_double_url_encode_detected(self):
        """ADR-v3-005: iterative unquote (max 3 rounds) catches double encoding."""
        guard = SandboxGuard()
        with pytest.raises(GuardrailDeny, match="sandbox_violation"):
            guard.before_tool_handler(
                HookContext(
                    event_type=EventType.BEFORE_TOOL_CALL,
                    tool_name="test",
                    tool_input={"path": "%252e%252e%252f"},
                )
            )

    def test_sbx_ed002_unicode_fullwidth_detected(self):
        """ADR-v3-005: NFKC normalization catches fullwidth chars."""
        guard = SandboxGuard()
        with pytest.raises(GuardrailDeny, match="sandbox_violation"):
            guard.before_tool_handler(
                HookContext(
                    event_type=EventType.BEFORE_TOOL_CALL,
                    tool_name="test",
                    tool_input={"path": "．．/etc/passwd"},
                )
            )

    def test_sbx_ed003_long_input_performance(self):
        guard = SandboxGuard()
        long_text = "normal text " * 1000
        ctx = HookContext(
            event_type=EventType.BEFORE_TOOL_CALL,
            tool_name="test",
            tool_input={"query": long_text},
        )
        start = time.monotonic()
        guard.before_tool_handler(ctx)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50

    def test_sbx_ed004_none_values_safe(self):
        guard = SandboxGuard()
        ctx = HookContext(
            event_type=EventType.BEFORE_TOOL_CALL,
            tool_name="test",
            tool_input={},
        )
        guard.before_tool_handler(ctx)


# ── LoopDetector hash edge cases ─────────────────────────────────────


class TestLoopEdge:
    def test_lpd_ed002_hash_list_truncation(self):
        det = LoopDetector(threshold=3)
        for i in range(10):
            try:
                det.after_turn_handler(
                    HookContext(
                        event_type=EventType.AFTER_TURN,
                        metadata={"output": "same"},
                        turn_number=i + 1,
                    )
                )
            except GuardrailDeny:
                break
        assert len(det._turn_hashes) <= 6


# ── DenyReason enum ─────────────────────────────────────────────────


class TestDenyReasonEnum:
    def test_deny_reason_is_enum(self):
        assert isinstance(DenyReason.BUDGET_EXCEEDED, DenyReason)
        assert DenyReason.BUDGET_EXCEEDED.value == "budget_exceeded"

    def test_deny_reason_invalid_value(self):
        with pytest.raises(ValueError):
            DenyReason("nonexistent_reason")


# ── AFTER_TURN deny via adapter ────────────────────────────────────


class TestAfterTurnDeny:
    def test_after_turn_deny_stored_as_pending(self):
        registry = HookRegistry()

        def deny_after_turn(ctx):
            raise GuardrailDeny(DenyReason.BUDGET_EXCEEDED, "over budget")

        registry.register(EventType.AFTER_TURN, deny_after_turn)
        adapter = CrewObservabilityAdapter(registry, session_id="test")
        step_cb = adapter.make_step_callback()
        with pytest.raises(GuardrailDeny, match="budget_exceeded"):
            step_cb(MagicMock(output="result"))


# ── CostGuard precision ─────────────────────────────────────────────


class TestCostEdge:
    def test_cst_ed001_float_precision(self):
        guard = CostGuard(budget_usd=0.000001)
        with pytest.raises(GuardrailDeny):
            guard.after_turn_handler(
                HookContext(
                    event_type=EventType.AFTER_TURN,
                    input_tokens=10,
                    output_tokens=10,
                )
            )


# ── HookLoader error handling ────────────────────────────────────────


class TestLoaderEdge:
    def test_ldr_ed001_invalid_yaml_no_crash(self, tmp_path, capsys):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.yaml").write_text("{{{invalid yaml")
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0
        captured = capsys.readouterr()
        assert "error" in captured.err.lower() or "yaml" in captured.err.lower()

    def test_ldr_ed003_handler_ref_no_dot(self, tmp_path, capsys):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.yaml").write_text(
            "hooks:\n  BEFORE_TURN:\n    - handler: just_a_function\n"
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0
        captured = capsys.readouterr()
        assert "invalid handler ref" in captured.err.lower()

    def test_ldr_ed004_deps_missing_warning(self, tmp_path, capsys):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.yaml").write_text(
            "strategies:\n"
            "  - name: gate\n"
            "    class: gate_mod.Gate\n"
            "    config: {}\n"
            "    deps:\n"
            "      logger: nonexistent_dep\n"
            "    hooks: {}\n"
        )
        (hooks_dir / "gate_mod.py").write_text(
            "class Gate:\n"
            "    def __init__(self, logger=None):\n"
            "        self.logger = logger\n"
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower()
        assert "nonexistent_dep" in captured.err

    def test_ldr_ed002_strategy_bad_params(self, tmp_path, capsys):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.yaml").write_text(
            "strategies:\n"
            "  - name: bad\n"
            "    class: strat.Strict\n"
            "    config:\n"
            "      unknown_param: true\n"
            "    hooks: {}\n"
        )
        (hooks_dir / "strat.py").write_text(
            "class Strict:\n"
            "    def __init__(self):\n"
            "        pass\n"
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert "bad" not in loader.strategies
        captured = capsys.readouterr()
        assert "failed to instantiate" in captured.err.lower() or "error" in captured.err.lower()
