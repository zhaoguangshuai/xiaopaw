"""UT-RTR-001 ~ UT-RTR-006: RetryTracker unit tests."""

import pytest

from xiaopaw.hook_framework.registry import EventType, HookContext

from shared_hooks.retry_tracker import RetryTracker


def _tool_ctx(tool_name="search", success=True):
    return HookContext(
        event_type=EventType.AFTER_TOOL_CALL,
        tool_name=tool_name,
        success=success,
    )


class TestRetryTracking:
    def test_rtr001_consecutive_failures(self):
        tracker = RetryTracker()
        for _ in range(3):
            tracker.after_tool_handler(_tool_ctx(success=False))
        m = tracker.get_metrics()
        assert m["active_failures"]["search"] == 3

    def test_rtr002_success_resets(self):
        tracker = RetryTracker()
        tracker.after_tool_handler(_tool_ctx(success=False))
        tracker.after_tool_handler(_tool_ctx(success=False))
        tracker.after_tool_handler(_tool_ctx(success=True))
        m = tracker.get_metrics()
        assert m["active_failures"].get("search", 0) == 0

    def test_rtr003_retry_success_rate(self):
        tracker = RetryTracker()
        tracker.after_tool_handler(_tool_ctx(success=False))
        tracker.after_tool_handler(_tool_ctx(success=False))
        tracker.after_tool_handler(_tool_ctx(success=False))
        tracker.after_tool_handler(_tool_ctx(success=True))
        m = tracker.get_metrics()
        assert m["total_retries"] == 2
        assert m["successful_retries"] == 1
        assert abs(m["retry_success_rate"] - 0.5) < 0.01

    def test_rtr004_independent_tools(self):
        tracker = RetryTracker()
        tracker.after_tool_handler(_tool_ctx("tool_a", success=False))
        tracker.after_tool_handler(_tool_ctx("tool_a", success=False))
        tracker.after_tool_handler(_tool_ctx("tool_b", success=False))
        m = tracker.get_metrics()
        assert m["active_failures"]["tool_a"] == 2
        assert m["active_failures"]["tool_b"] == 1

    def test_rtr005_empty_tool_name_skipped(self):
        tracker = RetryTracker()
        tracker.after_tool_handler(_tool_ctx("", success=False))
        m = tracker.get_metrics()
        assert m["active_failures"] == {}

    def test_rtr006_max_retries_warning(self, capsys):
        tracker = RetryTracker(max_retries=3)
        for _ in range(3):
            tracker.after_tool_handler(_tool_ctx(success=False))
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "warning" in captured.err.lower()
        assert "3" in captured.err
