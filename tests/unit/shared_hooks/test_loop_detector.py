"""UT-LPD-001 ~ UT-LPD-008: LoopDetector unit tests."""

import pytest

from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext

from shared_hooks.loop_detector import LoopDetector


def _tool_ctx(tool_name="search", output="same result", turn=1):
    return HookContext(
        event_type=EventType.AFTER_TOOL_CALL,
        tool_name=tool_name,
        turn_number=turn,
        metadata={"tool_output": output},
    )


def _turn_ctx(output="same output", turn=1):
    return HookContext(
        event_type=EventType.AFTER_TURN,
        turn_number=turn,
        metadata={"output": output},
    )


class TestBasicDetection:
    def test_lpd001_consecutive_same_state_detected(self):
        det = LoopDetector(threshold=3)
        for i in range(2):
            det.after_turn_handler(_turn_ctx(turn=i + 1))
        with pytest.raises(GuardrailDeny, match="loop_detected"):
            det.after_turn_handler(_turn_ctx(turn=3))

    def test_lpd002_different_state_no_detection(self):
        det = LoopDetector(threshold=3)
        for i in range(3):
            det.after_turn_handler(_turn_ctx(output=f"output_{i}", turn=i + 1))

    def test_lpd003_non_consecutive_no_detection(self):
        det = LoopDetector(threshold=3)
        det.after_turn_handler(_turn_ctx(output="A", turn=1))
        det.after_turn_handler(_turn_ctx(output="A", turn=2))
        det.after_turn_handler(_turn_ctx(output="B", turn=3))
        det.after_turn_handler(_turn_ctx(output="A", turn=4))


class TestThreshold:
    def test_lpd004_threshold_2(self):
        det = LoopDetector(threshold=2)
        det.after_turn_handler(_turn_ctx(turn=1))
        with pytest.raises(GuardrailDeny):
            det.after_turn_handler(_turn_ctx(turn=2))

    def test_lpd005_threshold_5_under(self):
        det = LoopDetector(threshold=5)
        for i in range(4):
            det.after_turn_handler(_turn_ctx(turn=i + 1))


class TestDualPath:
    def test_lpd006_tool_path_detection(self):
        det = LoopDetector(threshold=2)
        det.after_tool_handler(_tool_ctx(turn=1))
        with pytest.raises(GuardrailDeny):
            det.after_tool_handler(_tool_ctx(turn=2))

    def test_lpd007_paths_independent(self):
        det = LoopDetector(threshold=3)
        det.after_tool_handler(_tool_ctx(turn=1))
        det.after_tool_handler(_tool_ctx(turn=2))
        det.after_turn_handler(_turn_ctx(turn=1))
        det.after_turn_handler(_turn_ctx(turn=2))


class TestMetrics:
    def test_lpd008_metrics(self):
        det = LoopDetector(threshold=3)
        det.after_turn_handler(_turn_ctx(output="a", turn=1))
        det.after_turn_handler(_turn_ctx(output="b", turn=2))
        det.after_tool_handler(_tool_ctx(output="c", turn=1))
        m = det.get_metrics()
        assert m["total_turns"] == 2
        assert m["total_tool_calls"] == 1
