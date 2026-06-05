"""UT-LDR-001 ~ UT-LDR-014: HookLoader unit tests."""

from pathlib import Path

import pytest

from tests.fixtures.hook_yaml_samples import (
    COUNTER_STRATEGY_PY,
    DEPS_YAML,
    GATE_MOD_PY,
    INVALID_YAML,
    LOGGER_MOD_PY,
    MISSING_FUNC_YAML,
    MISSING_MODULE_YAML,
    PATH_TRAVERSAL_YAML,
    STRATEGIES_YAML,
    VALID_HANDLER_PY,
    VALID_HOOKS_YAML,
)
from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import EventType, HookContext, HookRegistry


def _setup_hooks_dir(tmp_path: Path, yaml_content: str, files: dict[str, str] = None):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.yaml").write_text(yaml_content)
    for name, content in (files or {}).items():
        (hooks_dir / name).write_text(content)
    return hooks_dir


# ── YAML Parsing ──────────────────────────────────────────────────────


class TestYamlParsing:
    def test_ldr001_load_handler_from_yaml(self, tmp_path):
        hooks_dir = _setup_hooks_dir(
            tmp_path, VALID_HOOKS_YAML, {"my_handler.py": VALID_HANDLER_PY}
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 1
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        registry.dispatch(EventType.BEFORE_TURN, ctx)

    def test_ldr002_missing_yaml_no_error(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(empty_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0

    def test_ldr003_missing_module_skipped(self, tmp_path, capsys):
        hooks_dir = _setup_hooks_dir(tmp_path, MISSING_MODULE_YAML)
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_ldr004_missing_function_skipped(self, tmp_path, capsys):
        hooks_dir = _setup_hooks_dir(
            tmp_path, MISSING_FUNC_YAML, {"my_handler.py": VALID_HANDLER_PY}
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_ldr005_path_traversal_blocked(self, tmp_path, capsys):
        hooks_dir = _setup_hooks_dir(tmp_path, PATH_TRAVERSAL_YAML)
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0
        captured = capsys.readouterr()
        assert "path traversal" in captured.err.lower()


# ── Two-layer loading ─────────────────────────────────────────────────


class TestTwoLayers:
    def test_ldr006_merge_global_and_workspace(self, tmp_path):
        global_dir = _setup_hooks_dir(
            tmp_path / "global",
            "hooks:\n  TASK_COMPLETE:\n    - handler: g.on_complete\n",
            {"g.py": "def on_complete(ctx): pass\n"},
        )
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws_hooks = ws_dir / "hooks"
        ws_hooks.mkdir()
        (ws_hooks / "hooks.yaml").write_text(
            "hooks:\n  TASK_COMPLETE:\n    - handler: w.on_complete\n"
        )
        (ws_hooks / "w.py").write_text("def on_complete(ctx): pass\n")

        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=global_dir, workspace_dir=ws_dir)
        assert registry.handler_count(EventType.TASK_COMPLETE) == 2

    def test_ldr007_workspace_missing_only_global(self, tmp_path):
        global_dir = _setup_hooks_dir(
            tmp_path / "global",
            VALID_HOOKS_YAML,
            {"my_handler.py": VALID_HANDLER_PY},
        )
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=global_dir, workspace_dir=ws_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) >= 1

    def test_ldr008_global_missing_only_workspace(self, tmp_path):
        global_dir = tmp_path / "empty_global"
        global_dir.mkdir()
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws_hooks = ws_dir / "hooks"
        ws_hooks.mkdir()
        (ws_hooks / "hooks.yaml").write_text(VALID_HOOKS_YAML)
        (ws_hooks / "my_handler.py").write_text(VALID_HANDLER_PY)

        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=global_dir, workspace_dir=ws_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) >= 1


# ── Strategies ────────────────────────────────────────────────────────


class TestStrategies:
    def test_ldr009_strategy_instantiation_and_handler(self, tmp_path):
        hooks_dir = _setup_hooks_dir(
            tmp_path,
            STRATEGIES_YAML,
            {"counter_strategy.py": COUNTER_STRATEGY_PY},
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert "counter" in loader.strategies
        assert loader.strategies["counter"].count == 10
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        registry.dispatch(EventType.BEFORE_TURN, ctx)
        assert loader.strategies["counter"].count == 11

    def test_ldr010_strategy_missing_module(self, tmp_path, capsys):
        yaml_content = (
            "strategies:\n"
            "  - name: bad\n"
            "    class: missing_mod.Foo\n"
            "    config: {}\n"
            "    hooks: {}\n"
        )
        hooks_dir = _setup_hooks_dir(tmp_path, yaml_content)
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert "bad" not in loader.strategies
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "error" in captured.err.lower()

    def test_ldr011_strategy_missing_class(self, tmp_path, capsys):
        yaml_content = (
            "strategies:\n"
            "  - name: bad\n"
            "    class: my_handler.NonExistentClass\n"
            "    config: {}\n"
            "    hooks: {}\n"
        )
        hooks_dir = _setup_hooks_dir(
            tmp_path, yaml_content, {"my_handler.py": VALID_HANDLER_PY}
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert "bad" not in loader.strategies
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "error" in captured.err.lower()

    def test_ldr012_strategy_config_passed(self, tmp_path):
        hooks_dir = _setup_hooks_dir(
            tmp_path,
            STRATEGIES_YAML,
            {"counter_strategy.py": COUNTER_STRATEGY_PY},
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert loader.strategies["counter"].count == 10


# ── Dependency Injection ──────────────────────────────────────────────


class TestDeps:
    def test_ldr013_deps_injection(self, tmp_path):
        hooks_dir = _setup_hooks_dir(
            tmp_path,
            DEPS_YAML,
            {"logger_mod.py": LOGGER_MOD_PY, "gate_mod.py": GATE_MOD_PY},
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        gate = loader.strategies["gate"]
        logger = loader.strategies["logger"]
        assert gate.logger is logger
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        registry.dispatch(EventType.BEFORE_TOOL_CALL, ctx)
        assert "checked" in logger.events

    def test_ldr_strategy_missing_method(self, tmp_path, capsys):
        yaml_content = (
            "strategies:\n"
            "  - name: counter\n"
            "    class: counter_strategy.Counter\n"
            "    config:\n"
            "      start: 0\n"
            "    hooks:\n"
            "      BEFORE_TURN: nonexistent_method\n"
        )
        hooks_dir = _setup_hooks_dir(
            tmp_path, yaml_content, {"counter_strategy.py": COUNTER_STRATEGY_PY}
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert "counter" in loader.strategies
        assert registry.handler_count(EventType.BEFORE_TURN) == 0
        captured = capsys.readouterr()
        assert "method not found" in captured.err.lower() or "not found" in captured.err.lower()

    def test_ldr_strategy_path_traversal(self, tmp_path, capsys):
        yaml_content = (
            "strategies:\n"
            "  - name: evil\n"
            "    class: ../../../etc/evil.Bad\n"
            "    config: {}\n"
            "    hooks: {}\n"
        )
        hooks_dir = _setup_hooks_dir(tmp_path, yaml_content)
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert "evil" not in loader.strategies
        captured = capsys.readouterr()
        assert "path traversal" in captured.err.lower()

    def test_ldr_non_dict_yaml(self, tmp_path):
        hooks_dir = _setup_hooks_dir(tmp_path, "just a string\n")
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert registry.handler_count(EventType.BEFORE_TURN) == 0

    def test_ldr014_deps_missing_key_injects_none(self, tmp_path):
        yaml_content = (
            "strategies:\n"
            "  - name: gate\n"
            "    class: gate_mod.Gate\n"
            "    config: {}\n"
            "    deps:\n"
            "      logger: nonexistent_strategy\n"
            "    hooks:\n"
            "      BEFORE_TOOL_CALL: check\n"
        )
        hooks_dir = _setup_hooks_dir(
            tmp_path, yaml_content, {"gate_mod.py": GATE_MOD_PY}
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(hooks_dir)
        assert loader.strategies["gate"].logger is None
