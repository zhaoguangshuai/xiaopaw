"""IT-CFG-001 ~ IT-CFG-004: Two-layer config integration tests.

These tests use tmp_path hooks (not real shared_hooks), so they don't
produce Langfuse traces. They verify config loading and merge behavior.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import EventType, HookContext, HookRegistry


def _write_layer(base_dir: Path, yaml_content: str, files: dict[str, str]):
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "hooks.yaml").write_text(yaml_content)
    for name, content in files.items():
        (base_dir / name).write_text(content)


@pytest.mark.integration
class TestTwoLayerConfig:
    def test_cfg001_merge(self, tmp_path):
        _write_layer(
            tmp_path / "global",
            "hooks:\n  TASK_COMPLETE:\n    - handler: g.on_complete\n",
            {"g.py": "def on_complete(ctx): pass\n"},
        )
        ws = tmp_path / "ws"
        ws.mkdir()
        _write_layer(
            ws / "hooks",
            "hooks:\n  TASK_COMPLETE:\n    - handler: w.on_complete\n",
            {"w.py": "def on_complete(ctx): pass\n"},
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=tmp_path / "global", workspace_dir=ws)
        assert registry.handler_count(EventType.TASK_COMPLETE) == 2

    def test_cfg002_global_before_workspace(self, tmp_path):
        call_order = []
        _write_layer(
            tmp_path / "global",
            "hooks:\n  BEFORE_TURN:\n    - handler: g.on_turn\n",
            {"g.py": f"import sys\ndef on_turn(ctx): sys.modules['{__name__}']._call_order.append('global')\n"},
        )
        ws = tmp_path / "ws"
        ws.mkdir()
        _write_layer(
            ws / "hooks",
            "hooks:\n  BEFORE_TURN:\n    - handler: w.on_turn\n",
            {"w.py": f"import sys\ndef on_turn(ctx): sys.modules['{__name__}']._call_order.append('ws')\n"},
        )
        import sys
        sys.modules[__name__]._call_order = call_order

        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=tmp_path / "global", workspace_dir=ws)
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        registry.dispatch(EventType.BEFORE_TURN, ctx)
        assert call_order == ["global", "ws"]

    def test_cfg003_only_global(self, tmp_path):
        _write_layer(
            tmp_path / "global",
            "hooks:\n  BEFORE_TURN:\n    - handler: g.on_turn\n",
            {"g.py": "def on_turn(ctx): pass\n"},
        )
        ws = tmp_path / "ws"
        ws.mkdir()
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=tmp_path / "global", workspace_dir=ws)
        assert registry.handler_count(EventType.BEFORE_TURN) == 1

    def test_cfg004_workspace_strategies(self, tmp_path):
        _write_layer(
            tmp_path / "global",
            "hooks: {}\n",
            {},
        )
        ws = tmp_path / "ws"
        ws.mkdir()
        _write_layer(
            ws / "hooks",
            (
                "strategies:\n"
                "  - name: counter\n"
                "    class: counter.Counter\n"
                "    config:\n"
                "      start: 5\n"
                "    hooks:\n"
                "      BEFORE_TURN: on_turn\n"
            ),
            {"counter.py": "class Counter:\n    def __init__(self, start=0):\n        self.count = start\n    def on_turn(self, ctx):\n        self.count += 1\n"},
        )
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_two_layers(global_dir=tmp_path / "global", workspace_dir=ws)
        assert "counter" in loader.strategies
        assert loader.strategies["counter"].count == 5
