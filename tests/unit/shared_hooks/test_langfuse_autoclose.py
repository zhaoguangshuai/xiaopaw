"""Tests for langfuse_trace auto-close logic in before_llm_handler and after_turn_handler.

Verifies that open tool spans are selectively closed when matching tool results
appear in prompt_messages, and that intermediate AFTER_TURN events are no-ops.
"""

import os
import unittest
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _enable_langfuse(monkeypatch):
    monkeypatch.setenv("TRACE_TO_LANGFUSE", "1")
    import shared_hooks.langfuse_trace as mod

    mod._ENABLED = True
    mod._trace_id_var.set("test-trace")
    mod._root_span_id_var.set("test-root")
    mod._gen_id_var.set("")
    mod._gen_count_var.set(0)
    mod._tool_count_var.set(0)
    mod._span_stack_var.set(())
    mod._closed_spans_var.set({})
    mod._batch_buffer.clear()
    monkeypatch.setattr(mod, "_flush_batch", lambda: None)
    yield
    mod._ENABLED = False
    mod._trace_id_var.set("")
    mod._root_span_id_var.set("")
    mod._gen_id_var.set("")
    mod._gen_count_var.set(0)
    mod._tool_count_var.set(0)
    mod._span_stack_var.set(())
    mod._closed_spans_var.set({})
    mod._batch_buffer.clear()


def _make_ctx(event_type, **kwargs):
    from xiaopaw.hook_framework.registry import EventType, HookContext

    return HookContext(event_type=event_type, session_id="test-sess", **kwargs)


def _get_events_of_type(event_type: str):
    import shared_hooks.langfuse_trace as mod

    return [e for e in mod._batch_buffer if e.type == event_type]


class TestExtractRecentToolResults:
    def test_extracts_tool_messages_after_last_assistant(self):
        from shared_hooks.langfuse_trace import _extract_recent_tool_results

        messages = [
            {"role": "user", "content": "search python"},
            {"role": "assistant", "content": "I'll search for you"},
            {"role": "tool", "name": "baidu_search", "content": "result A"},
            {"role": "tool", "name": "file_read", "content": "result B"},
        ]
        results = _extract_recent_tool_results(messages)
        assert results == [("baidu_search", "result A"), ("file_read", "result B")]

    def test_stops_at_assistant_boundary(self):
        from shared_hooks.langfuse_trace import _extract_recent_tool_results

        messages = [
            {"role": "assistant", "content": "old call"},
            {"role": "tool", "name": "old_tool", "content": "old result"},
            {"role": "assistant", "content": "new call"},
            {"role": "tool", "name": "new_tool", "content": "new result"},
        ]
        results = _extract_recent_tool_results(messages)
        assert results == [("new_tool", "new result")]

    def test_empty_messages(self):
        from shared_hooks.langfuse_trace import _extract_recent_tool_results

        assert _extract_recent_tool_results([]) == []
        assert _extract_recent_tool_results(None) == []

    def test_no_tool_messages(self):
        from shared_hooks.langfuse_trace import _extract_recent_tool_results

        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        assert _extract_recent_tool_results(messages) == []

    def test_duplicate_names_preserves_both(self):
        from shared_hooks.langfuse_trace import _extract_recent_tool_results

        messages = [
            {"role": "assistant", "content": "running bash twice"},
            {"role": "tool", "name": "bash", "content": "first"},
            {"role": "tool", "name": "bash", "content": "second"},
        ]
        results = _extract_recent_tool_results(messages)
        assert results == [("bash", "first"), ("bash", "second")]


class TestBeforeLlmAutoCloseSpans:
    def test_selective_close_with_matching_tool_results(self):
        """Only spans with matching tool results in prompt_messages get closed."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (
                ("span-1", "baidu_search", 1, {"q": "test"}),
                ("span-2", "file_read", 1, {"path": "/tmp"}),
            )
        )

        prompt_messages = [
            {"role": "assistant", "content": "I'll use tools"},
            {"role": "tool", "name": "baidu_search", "content": "search results here"},
            {"role": "tool", "name": "file_read", "content": "file contents here"},
        ]
        ctx = _make_ctx(
            EventType.BEFORE_LLM,
            metadata={"model": "qwen3-max", "prompt_messages": prompt_messages},
        )
        mod.before_llm_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        closed_ids = {e.body.id for e in span_updates}
        assert "span-1" in closed_ids
        assert "span-2" in closed_ids

        assert mod._span_stack_var.get(()) == ()

        closed = mod._closed_spans_var.get({})
        assert closed[("baidu_search", 1)] == "span-1"
        assert closed[("file_read", 1)] == "span-2"

    def test_selective_close_keeps_unmatched_spans(self):
        """Spans without matching tool results (like agent_execution) stay in stack."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (
                ("span-ae", "agent_execution", 1, {"content": "hello"}),
                ("span-sl", "skill_loader", 1, {"skill_name": "memory-save"}),
            )
        )

        prompt_messages = [
            {"role": "assistant", "content": "calling skill_loader"},
            {"role": "tool", "name": "skill_loader", "content": "saved to user.md"},
        ]
        ctx = _make_ctx(
            EventType.BEFORE_LLM,
            metadata={"model": "qwen3-max", "prompt_messages": prompt_messages},
        )
        mod.before_llm_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        closed_ids = {e.body.id for e in span_updates}
        assert "span-sl" in closed_ids
        assert "span-ae" not in closed_ids

        remaining = mod._span_stack_var.get(())
        assert len(remaining) == 1
        assert remaining[0][0] == "span-ae"

    def test_no_close_when_no_prompt_messages(self):
        """Without prompt_messages, no tool results → no spans closed."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (("span-1", "baidu_search", 1, {"q": "test"}),)
        )

        ctx = _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        mod.before_llm_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        assert len(span_updates) == 0

        assert len(mod._span_stack_var.get(())) == 1

    def test_auto_close_extracts_tool_output_from_prompt_messages(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (
                ("span-1", "baidu_search", 1, {"q": "test"}),
                ("span-2", "file_read", 1, {"path": "/tmp"}),
            )
        )

        prompt_messages = [
            {"role": "assistant", "content": "I'll use tools"},
            {"role": "tool", "name": "baidu_search", "content": "search results here"},
            {"role": "tool", "name": "file_read", "content": "file contents here"},
        ]
        ctx = _make_ctx(
            EventType.BEFORE_LLM,
            metadata={"model": "qwen3-max", "prompt_messages": prompt_messages},
        )
        mod.before_llm_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        span1 = [e for e in span_updates if e.body.id == "span-1"][0]
        span2 = [e for e in span_updates if e.body.id == "span-2"][0]
        assert span1.body.output == {"result": "search results here"}
        assert span2.body.output == {"result": "file contents here"}

    def test_sets_gen_output_with_tool_details(self):
        """Gen output includes tool name and input for closed spans."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (("span-1", "baidu_search", 1, {"q": "test"}),)
        )

        prompt_messages = [
            {"role": "assistant", "content": "searching"},
            {"role": "tool", "name": "baidu_search", "content": "results"},
        ]
        ctx = _make_ctx(
            EventType.BEFORE_LLM,
            metadata={"model": "qwen3-max", "prompt_messages": prompt_messages},
        )
        mod.before_llm_handler(ctx)

        gen_updates = _get_events_of_type("generation-update")
        prev_update = [e for e in gen_updates if e.body.id == "prev-gen-id"]
        assert len(prev_update) == 1
        assert prev_update[0].body.output == {
            "action": "tool_calls",
            "tools": [{"name": "baidu_search", "input": {"q": "test"}}],
        }

    def test_gen_output_none_when_no_spans_closed(self):
        """Gen output is None when no spans are closed (no matching results)."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (("span-ae", "agent_execution", 1, {"content": "hello"}),)
        )

        ctx = _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        mod.before_llm_handler(ctx)

        gen_updates = _get_events_of_type("generation-update")
        prev_update = [e for e in gen_updates if e.body.id == "prev-gen-id"]
        assert len(prev_update) == 1
        assert prev_update[0].body.output is None

    def test_no_output_when_no_open_spans(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(())

        ctx = _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        mod.before_llm_handler(ctx)

        gen_updates = _get_events_of_type("generation-update")
        prev_update = [e for e in gen_updates if e.body.id == "prev-gen-id"]
        assert len(prev_update) == 1
        assert prev_update[0].body.output is None

    def test_noop_when_no_previous_gen(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("")
        mod._span_stack_var.set(
            (("span-1", "baidu_search", 1, {"q": "test"}),)
        )

        ctx = _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        mod.before_llm_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        assert len(span_updates) == 0
        assert len(mod._span_stack_var.get(())) == 1

    def test_duplicate_tool_names_positional_matching(self):
        """Two spans with same tool name match results positionally."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("prev-gen-id")
        mod._span_stack_var.set(
            (
                ("span-bash1", "bash", 1, {"cmd": "ls"}),
                ("span-bash2", "bash", 1, {"cmd": "cat file.txt"}),
            )
        )

        prompt_messages = [
            {"role": "assistant", "content": "running two bash commands"},
            {"role": "tool", "name": "bash", "content": "file1.txt"},
            {"role": "tool", "name": "bash", "content": "hello world"},
        ]
        ctx = _make_ctx(
            EventType.BEFORE_LLM,
            metadata={"model": "qwen3-max", "prompt_messages": prompt_messages},
        )
        mod.before_llm_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        bash1 = [e for e in span_updates if e.body.id == "span-bash1"][0]
        bash2 = [e for e in span_updates if e.body.id == "span-bash2"][0]
        assert bash1.body.output == {"result": "file1.txt"}
        assert bash2.body.output == {"result": "hello world"}

        assert mod._span_stack_var.get(()) == ()


class TestAfterTurnAutoCloseSpans:
    def test_intermediate_after_turn_is_noop(self):
        """Intermediate AFTER_TURN does nothing in langfuse handler."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("gen-active")
        mod._span_stack_var.set(
            (("span-1", "baidu_search", 1, {"q": "test"}),)
        )

        ctx = _make_ctx(
            EventType.AFTER_TURN,
            metadata={"output": "step output", "is_intermediate": True},
        )
        mod.after_turn_handler(ctx)

        assert mod._gen_id_var.get("") == "gen-active"
        assert len(mod._span_stack_var.get(())) == 1

        assert len(_get_events_of_type("span-update")) == 0
        assert len(_get_events_of_type("generation-update")) == 0

    def test_final_after_turn_closes_remaining_spans(self):
        """Final (non-intermediate) AFTER_TURN closes remaining spans."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("gen-final")
        mod._span_stack_var.set(
            (("span-last", "code_execute", 1, {"script": "ls"}),)
        )

        ctx = _make_ctx(
            EventType.AFTER_TURN, metadata={"output": "final answer"}
        )
        mod.after_turn_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        auto_closed = [e for e in span_updates if e.body.id == "span-last"]
        assert len(auto_closed) == 1
        assert auto_closed[0].body.end_time is not None

        assert mod._span_stack_var.get(()) == ()

    def test_no_span_close_when_stack_empty(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("gen-final")
        mod._span_stack_var.set(())

        ctx = _make_ctx(
            EventType.AFTER_TURN, metadata={"output": "final answer"}
        )
        mod.after_turn_handler(ctx)

        span_updates = _get_events_of_type("span-update")
        non_root = [e for e in span_updates if e.body.id != "test-root"]
        assert len(non_root) == 0


class TestAfterToolRecovery:
    """after_tool_handler recovers auto-closed spans from _closed_spans_var."""

    def test_after_tool_updates_auto_closed_span(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._gen_id_var.set("gen-1")
        mod._span_stack_var.set(
            (("span-1", "baidu_search", 1, {"q": "test"}),)
        )

        prompt_messages = [
            {"role": "assistant", "content": "searching"},
            {"role": "tool", "name": "baidu_search", "content": "auto-close result"},
        ]
        mod.before_llm_handler(
            _make_ctx(
                EventType.BEFORE_LLM,
                metadata={"model": "qwen3-max", "prompt_messages": prompt_messages},
            )
        )
        assert mod._closed_spans_var.get({}).get(("baidu_search", 1)) == "span-1"

        mod._batch_buffer.clear()

        mod.after_tool_handler(
            _make_ctx(
                EventType.AFTER_TOOL_CALL,
                tool_name="baidu_search",
                turn_number=1,
                tool_input={"q": "test"},
                metadata={"tool_output": "search results here"},
            )
        )

        span_updates = _get_events_of_type("span-update")
        assert len(span_updates) == 1
        assert span_updates[0].body.id == "span-1"
        assert span_updates[0].body.output["result"] == "search results here"
        assert span_updates[0].body.metadata["phase"] == "completed"

        span_creates = _get_events_of_type("span-create")
        assert len(span_creates) == 0

        assert ("baidu_search", 1) not in mod._closed_spans_var.get({})

    def test_after_tool_falls_back_to_new_span_if_not_found(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._closed_spans_var.set({})

        mod.after_tool_handler(
            _make_ctx(
                EventType.AFTER_TOOL_CALL,
                tool_name="unknown_tool",
                tool_input={"x": 1},
                metadata={"tool_output": "result"},
            )
        )

        span_creates = _get_events_of_type("span-create")
        assert len(span_creates) == 1
        assert span_creates[0].body.name == "tool-unknown_tool"


class TestParentHierarchy:
    """Verify tool spans are children of current gen, gens are children of stack top."""

    def test_tool_span_parent_is_current_gen(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod.before_llm_handler(
            _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        )
        gen_id = mod._gen_id_var.get("")
        assert gen_id

        mod._batch_buffer.clear()

        mod.before_tool_handler(
            _make_ctx(
                EventType.BEFORE_TOOL_CALL,
                tool_name="baidu_search",
                tool_input={"q": "test"},
            )
        )

        span_creates = _get_events_of_type("span-create")
        assert len(span_creates) == 1
        assert span_creates[0].body.parent_observation_id == gen_id

    def test_gen_parent_is_stack_top(self):
        """New gen's parent is the stack top (e.g. agent_execution), not current gen."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._span_stack_var.set(
            (("span-ae", "agent_execution", 1, {"content": "hello"}),)
        )

        mod.before_llm_handler(
            _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        )

        gen_creates = _get_events_of_type("generation-create")
        assert len(gen_creates) == 1
        assert gen_creates[0].body.parent_observation_id == "span-ae"

    def test_gen_parent_falls_back_to_root(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._span_stack_var.set(())

        mod.before_llm_handler(
            _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        )

        gen_creates = _get_events_of_type("generation-create")
        assert len(gen_creates) == 1
        assert gen_creates[0].body.parent_observation_id == "test-root"


class TestSubcrewCleanup:
    def test_closes_remaining_gen_and_spans(self):
        import shared_hooks.langfuse_trace as mod

        mod._gen_id_var.set("subcrew-gen")
        mod._span_stack_var.set(
            (("subcrew-span", "sandbox_bash", 1, {"cmd": "ls"}),)
        )

        mod.subcrew_cleanup()

        gen_updates = _get_events_of_type("generation-update")
        assert len(gen_updates) == 1
        assert gen_updates[0].body.id == "subcrew-gen"
        assert gen_updates[0].body.end_time is not None

        span_updates = _get_events_of_type("span-update")
        assert len(span_updates) == 1
        assert span_updates[0].body.id == "subcrew-span"
        assert span_updates[0].body.metadata["phase"] == "subcrew-cleanup"

        assert mod._gen_id_var.get("") == ""
        assert mod._span_stack_var.get(()) == ()

    def test_noop_when_nothing_open(self):
        import shared_hooks.langfuse_trace as mod

        mod._gen_id_var.set("")
        mod._span_stack_var.set(())

        mod.subcrew_cleanup()

        assert len(_get_events_of_type("generation-update")) == 0
        assert len(_get_events_of_type("span-update")) == 0


class TestTaskCompleteParent:
    def test_task_complete_parent_is_stack_top(self):
        """task-complete span nests under agent_execution, not root."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._span_stack_var.set(
            (("span-ae", "agent_execution", 1, {"content": "hello"}),)
        )

        mod.task_complete_handler(
            _make_ctx(
                EventType.TASK_COMPLETE,
                metadata={"task_description": "main task", "raw_output": "done"},
            )
        )

        span_creates = _get_events_of_type("span-create")
        tc = [e for e in span_creates if e.body.name == "task-complete"]
        assert len(tc) == 1
        assert tc[0].body.parent_observation_id == "span-ae"

    def test_task_complete_does_not_update_root_span(self):
        """task_complete_handler no longer updates root span output."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod.task_complete_handler(
            _make_ctx(
                EventType.TASK_COMPLETE,
                metadata={"task_description": "main task", "raw_output": "done"},
            )
        )

        span_updates = _get_events_of_type("span-update")
        root_updates = [e for e in span_updates if e.body.id == "test-root"]
        assert len(root_updates) == 0


class TestSubCrewFlowSimulation:
    """Simulates the sub-crew flow: multiple LLM calls with tool calls in between,
    where step_callback doesn't fire for intermediate steps."""

    def test_full_subcrew_flow(self):
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        ctx_llm = lambda msgs=None: _make_ctx(
            EventType.BEFORE_LLM,
            metadata={"model": "qwen3-max", "prompt_messages": msgs or []},
        )
        ctx_tool = lambda name: _make_ctx(
            EventType.BEFORE_TOOL_CALL, tool_name=name, tool_input={"q": "test"}
        )

        mod.before_llm_handler(ctx_llm())
        gen1_id = mod._gen_id_var.get("")
        assert gen1_id

        mod.before_tool_handler(ctx_tool("baidu_search"))
        assert len(mod._span_stack_var.get(())) == 1
        tool1_span_id = mod._span_stack_var.get(())[0][0]

        mod.before_llm_handler(ctx_llm([
            {"role": "assistant", "content": "calling search"},
            {"role": "tool", "name": "baidu_search", "content": "search output"},
        ]))
        gen2_id = mod._gen_id_var.get("")
        assert gen2_id != gen1_id

        gen1_updates = [
            e for e in _get_events_of_type("generation-update")
            if e.body.id == gen1_id
        ]
        assert len(gen1_updates) == 1
        assert gen1_updates[0].body.output == {
            "action": "tool_calls",
            "tools": [{"name": "baidu_search", "input": {"q": "test"}}],
        }

        tool1_updates = [
            e for e in _get_events_of_type("span-update")
            if e.body.id == tool1_span_id
        ]
        assert len(tool1_updates) == 1
        assert tool1_updates[0].body.end_time is not None
        assert tool1_updates[0].body.output == {"result": "search output"}

        assert mod._span_stack_var.get(()) == ()

        mod.before_tool_handler(ctx_tool("code_execute"))
        tool2_span_id = mod._span_stack_var.get(())[0][0]

        ctx_turn = _make_ctx(
            EventType.AFTER_TURN, metadata={"output": "final result"}
        )
        mod.after_turn_handler(ctx_turn)

        gen2_updates = [
            e for e in _get_events_of_type("generation-update")
            if e.body.id == gen2_id
        ]
        assert len(gen2_updates) == 1
        assert gen2_updates[0].body.output == "final result"

        tool2_updates = [
            e for e in _get_events_of_type("span-update")
            if e.body.id == tool2_span_id
        ]
        assert len(tool2_updates) == 1
        assert tool2_updates[0].body.end_time is not None

    def test_full_flow_no_duplicate_spans(self):
        """before_tool → before_llm (auto-close) → after_tool should produce
        exactly ONE span (updated, not duplicated)."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod.before_llm_handler(
            _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        )

        mod.before_tool_handler(
            _make_ctx(
                EventType.BEFORE_TOOL_CALL,
                tool_name="sandbox_bash",
                tool_input={"cmd": "ls"},
            )
        )
        span_id = mod._span_stack_var.get(())[0][0]

        mod.before_llm_handler(
            _make_ctx(
                EventType.BEFORE_LLM,
                metadata={
                    "model": "qwen3-max",
                    "prompt_messages": [
                        {"role": "assistant", "content": "running bash"},
                        {"role": "tool", "name": "sandbox_bash", "content": "auto-close output"},
                    ],
                },
            )
        )

        mod._batch_buffer.clear()

        mod.after_tool_handler(
            _make_ctx(
                EventType.AFTER_TOOL_CALL,
                tool_name="sandbox_bash",
                tool_input={"cmd": "ls"},
                metadata={"tool_output": "file1.txt\nfile2.txt"},
            )
        )

        span_updates = _get_events_of_type("span-update")
        span_creates = _get_events_of_type("span-create")

        assert len(span_updates) == 1
        assert span_updates[0].body.id == span_id
        assert span_updates[0].body.output["result"] == "file1.txt\nfile2.txt"

        assert len(span_creates) == 0

    def test_agent_execution_survives_autoclosure(self):
        """agent_execution span remains in stack through selective auto-close."""
        import shared_hooks.langfuse_trace as mod
        from xiaopaw.hook_framework.registry import EventType

        mod._span_stack_var.set(
            (("span-ae", "agent_execution", 1, {"content": "hello"}),)
        )

        mod.before_llm_handler(
            _make_ctx(EventType.BEFORE_LLM, metadata={"model": "qwen3-max"})
        )
        gen1_id = mod._gen_id_var.get("")

        mod.before_tool_handler(
            _make_ctx(
                EventType.BEFORE_TOOL_CALL,
                tool_name="skill_loader",
                tool_input={"skill_name": "memory-save"},
            )
        )

        mod.before_llm_handler(
            _make_ctx(
                EventType.BEFORE_LLM,
                metadata={
                    "model": "qwen3-max",
                    "prompt_messages": [
                        {"role": "assistant", "content": "calling skill"},
                        {"role": "tool", "name": "skill_loader", "content": "saved"},
                    ],
                },
            )
        )

        remaining = mod._span_stack_var.get(())
        assert len(remaining) == 1
        assert remaining[0][0] == "span-ae"

        gen2_creates = [
            e for e in _get_events_of_type("generation-create")
            if e.body.id != gen1_id
        ]
        assert len(gen2_creates) == 1
        assert gen2_creates[0].body.parent_observation_id == "span-ae"
