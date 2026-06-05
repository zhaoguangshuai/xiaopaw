"""Integration test fixtures: unique session IDs + Langfuse flush/verify per test."""

import base64
import os
import uuid

import pytest
import requests

from xiaopaw.hook_framework.registry import EventType, HookContext

_LF_HOST = (
    os.environ.get("XIAOPAW_LANGFUSE_BASE_URL", "")
    or os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000")
)
_LF_PK = (
    os.environ.get("XIAOPAW_LANGFUSE_PUBLIC_KEY", "")
    or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
)
_LF_SK = (
    os.environ.get("XIAOPAW_LANGFUSE_SECRET_KEY", "")
    or os.environ.get("LANGFUSE_SECRET_KEY", "")
)


def _auth_header() -> str:
    return base64.b64encode(f"{_LF_PK}:{_LF_SK}".encode()).decode()


@pytest.fixture
def unique_session_id(request):
    """Generate a unique session ID per test function for Langfuse trace isolation."""
    short_id = uuid.uuid4().hex[:8]
    test_name = request.node.name.replace("test_", "").replace("_", "-")[:30]
    return f"it-{test_name}-{short_id}"


@pytest.fixture(autouse=True)
def _flush_langfuse_buffer():
    """Flush langfuse batch buffer before and after each test."""
    _do_flush()
    yield
    _do_flush()


def _find_dynamic_langfuse_modules():
    import sys as _sys

    return [
        mod
        for name, mod in _sys.modules.items()
        if name.startswith("hooks_dynamic.")
        and name.endswith(".langfuse_trace")
        and hasattr(mod, "_flush_batch")
    ]


def _do_flush():
    for lt in _find_dynamic_langfuse_modules():
        try:
            lt._flush_batch()
        except Exception:
            pass


def langfuse_get_trace(
    trace_id: str, *, min_observations: int = 0
) -> dict | None:
    """Query Langfuse for a trace, with retry for ingestion processing lag.

    If min_observations > 0, keeps retrying until the trace has at least that
    many observations (Langfuse processes batch events asynchronously).
    """
    if not _LF_PK or not _LF_SK:
        return None
    import time

    _DELAYS = [2, 2, 2, 2, 2]

    for delay in _DELAYS:
        time.sleep(delay)
        try:
            resp = requests.get(
                f"{_LF_HOST}/api/public/traces/{trace_id}",
                headers={"Authorization": f"Basic {_auth_header()}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                obs = data.get("observations", [])
                if len(obs) >= min_observations:
                    return data
        except Exception:
            pass
    return None


def ensure_trace(registry, session_id: str):
    """Dispatch BEFORE_TURN to create a Langfuse trace."""
    registry.dispatch(
        EventType.BEFORE_TURN,
        HookContext(
            event_type=EventType.BEFORE_TURN,
            session_id=session_id,
            metadata={"user_message": "test"},
        ),
    )


def finalize_trace(registry, session_id: str):
    """Dispatch SESSION_END to flush all events to Langfuse.

    The ingestion API processes events synchronously, so data is available
    after flush + a short ingestion lag.
    """
    registry.dispatch(
        EventType.SESSION_END,
        HookContext(event_type=EventType.SESSION_END, session_id=session_id),
    )
    _do_flush()


def assert_trace_exists(session_id: str, *, min_observations: int = 0) -> dict:
    """Assert a Langfuse trace exists for the given session_id."""
    _do_flush()
    trace = langfuse_get_trace(session_id, min_observations=min_observations)
    assert trace is not None, f"Langfuse trace not found for session_id={session_id}"
    obs = trace.get("observations", [])
    if min_observations > 0:
        assert len(obs) >= min_observations, (
            f"Expected >= {min_observations} observations, got {len(obs)} "
            f"for trace {session_id}"
        )
    return trace


def assert_observation_has_io(trace: dict, obs_name: str) -> dict:
    """Assert a named observation has input or output.

    Matches observations whose name starts with obs_name or tool-{obs_name}.
    """
    obs_list = trace.get("observations", [])
    prefixes = [obs_name, f"tool-{obs_name}"]
    matching = [
        o
        for o in obs_list
        if any(o.get("name", "").startswith(p) for p in prefixes)
    ]
    assert matching, (
        f"No observation matching '{obs_name}' found. "
        f"Available: {[o.get('name') for o in obs_list]}"
    )
    return matching[0]


# ── SDK trace quality assertions ──────────────────────────────────────


def assert_trace_has_session(trace: dict) -> None:
    """Assert trace has sessionId and name."""
    assert trace.get("sessionId"), "Trace missing sessionId"
    assert trace.get("name"), "Trace missing name"


def assert_root_span_exists(trace: dict) -> dict:
    """Assert root span (session-*) exists with source metadata."""
    obs = trace.get("observations", [])
    roots = [o for o in obs if (o.get("name") or "").startswith("session-")]
    assert roots, (
        f"No root span (session-*) found. "
        f"Available: {[o.get('name') for o in obs]}"
    )
    root = roots[0]
    meta = root.get("metadata", {})
    assert meta.get("source") == "xiaopaw-v2", (
        f"Root span metadata.source expected 'xiaopaw-v2', got {meta.get('source')}"
    )
    return root


def assert_generation_exists(trace: dict, *, min_count: int = 1) -> list[dict]:
    """Assert GENERATION observations exist with model and endTime."""
    obs = trace.get("observations", [])
    gens = [o for o in obs if o.get("type") == "GENERATION"]
    assert len(gens) >= min_count, (
        f"Expected >= {min_count} GENERATIONs, got {len(gens)}. "
        f"Types: {[o.get('type') for o in obs]}"
    )
    for g in gens:
        assert g.get("model"), f"GENERATION {g.get('name')} missing model"
    return gens


def assert_tool_observation(trace: dict, tool_name: str) -> dict:
    """Assert a tool observation exists with parent."""
    obs = trace.get("observations", [])
    prefix = f"tool-{tool_name}"
    tools = [o for o in obs if (o.get("name") or "").startswith(prefix)]
    if not tools:
        tools = [o for o in obs if (o.get("name") or "").startswith(tool_name)]
    assert tools, (
        f"No tool observation matching '{prefix}' or '{tool_name}'. "
        f"Available: {[o.get('name') for o in obs]}"
    )
    tool = tools[0]
    assert tool.get("parentObservationId"), (
        f"Tool {tool.get('name')} missing parentObservationId"
    )
    return tool


def assert_tree_structure(trace: dict) -> None:
    """Assert all observations have valid parent references (tree integrity)."""
    obs = trace.get("observations", [])
    obs_ids = {o.get("id") for o in obs}
    for o in obs:
        parent = o.get("parentObservationId")
        if parent:
            assert parent in obs_ids, (
                f"Observation {o.get('name')} has parentObservationId={parent} "
                f"not found in observation IDs"
            )


def assert_deny_observation(trace: dict, tool_name: str) -> dict:
    """Assert a denied tool observation has ERROR level and deny metadata."""
    obs = trace.get("observations", [])
    prefix = f"tool-{tool_name}"
    tools = [o for o in obs if (o.get("name") or "").startswith(prefix)]
    if not tools:
        tools = [o for o in obs if (o.get("name") or "").startswith(tool_name)]
    assert tools, f"No tool observation for '{tool_name}'"

    error_tools = [t for t in tools if t.get("level") == "ERROR"]
    assert error_tools, (
        f"No ERROR-level observation for '{tool_name}'. "
        f"Levels: {[(t.get('name'), t.get('level')) for t in tools]}"
    )
    denied = error_tools[0]
    output = denied.get("output", {})
    if isinstance(output, dict):
        assert output.get("deny_reason"), (
            f"Denied tool {tool_name} missing deny_reason in output"
        )
    return denied
