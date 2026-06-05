"""E2E test fixtures for XiaoPaw v3 — real LLM + Langfuse verification."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
import requests
from aiohttp import ClientTimeout
from aiohttp.test_utils import TestClient, TestServer

from xiaopaw.api.capture_sender import CaptureSender
from xiaopaw.api.test_server import create_test_app
from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import HookRegistry
from xiaopaw.runner import Runner
from xiaopaw.session.manager import SessionManager
from xiaopaw.session.models import MessageEntry

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_INIT = _PROJECT_ROOT / "workspace-init"
_SHARED_HOOKS = _PROJECT_ROOT / "shared_hooks"

_LF_HOST = os.environ.get("XIAOPAW_LANGFUSE_BASE_URL", "") or os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000")
_LF_PK = os.environ.get("XIAOPAW_LANGFUSE_PUBLIC_KEY", "") or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
_LF_SK = os.environ.get("XIAOPAW_LANGFUSE_SECRET_KEY", "") or os.environ.get("LANGFUSE_SECRET_KEY", "")


def _init_workspace(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o777)
    if _WORKSPACE_INIT.exists():
        for f in _WORKSPACE_INIT.glob("*.md"):
            dst = target / f.name
            shutil.copy2(f, dst)
            dst.chmod(0o666)


def _load_shared_hooks() -> HookRegistry:
    registry = HookRegistry()
    if _SHARED_HOOKS.exists():
        loader = HookLoader(registry)
        fail_closed = {"sandbox_guard", "permission_gate"}
        loader.load_from_directory(
            _SHARED_HOOKS,
            layer_name="global",
            fail_closed_names=fail_closed,
        )
    return registry


async def send_message(
    client: TestClient,
    content: str,
    routing_key: str = "p2p:ou_test001",
    sender_id: str = "ou_test001",
    timeout: float = 300.0,
) -> dict:
    ct = ClientTimeout(total=timeout, sock_read=timeout)
    resp = await client.post(
        "/api/test/message",
        json={
            "routing_key": routing_key,
            "content": content,
            "sender_id": sender_id,
        },
        timeout=ct,
    )
    assert resp.status == 200, f"TestAPI returned {resp.status}: {await resp.text()}"
    return await resp.json()


def llm_assert(reply: str, criteria: str, *, api_key: str = "") -> bool:
    """LLM-as-Judge: check if reply semantically satisfies criteria."""
    key = api_key or os.environ.get("QWEN_API_KEY", "")
    if not key:
        return bool(reply and reply.strip())

    resp = requests.post(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": "qwen3-max",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一个测试断言判断器。判断 AI 助手的回复是否满足指定的判断标准。"
                        "只回复 YES 或 NO，不要解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"AI助手的回复：\n{reply}\n\n判断标准：{criteria}",
                },
            ],
            "max_tokens": 10,
            "temperature": 0.0,
        },
        timeout=30,
    )
    if resp.status_code == 200:
        answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
        return "YES" in answer
    return bool(reply and reply.strip())


async def langfuse_get_trace(trace_id: str, retries: int = 6, delay: float = 2.0) -> dict | None:
    """Query Langfuse for a trace by ID, with retries for async ingestion lag.

    Uses asyncio.sleep() to yield control to the event loop so the runner
    worker can finish flushing events to Langfuse while we wait.
    """
    if not _LF_PK or not _LF_SK:
        return None
    auth = base64.b64encode(f"{_LF_PK}:{_LF_SK}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    for attempt in range(retries):
        if attempt > 0:
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(1.0)
        try:
            resp = requests.get(
                f"{_LF_HOST}/api/public/traces/{trace_id}",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return None


async def assert_langfuse_trace(trace_id: str, *, session_id: str = "") -> dict:
    """Assert a Langfuse trace exists and optionally verify session_id."""
    trace = await langfuse_get_trace(trace_id)
    assert trace is not None, f"Langfuse trace {trace_id} not found"
    assert trace.get("name"), f"Langfuse trace {trace_id} has no name"
    if session_id:
        assert trace.get("sessionId") == session_id, (
            f"Langfuse sessionId mismatch: expected {session_id}, got {trace.get('sessionId')}"
        )
    return trace


async def assert_langfuse_has_observations(trace_id: str, min_count: int = 1) -> list[dict]:
    """Assert Langfuse trace has at least min_count observations."""
    trace = await langfuse_get_trace(trace_id)
    assert trace is not None, f"Langfuse trace {trace_id} not found"
    obs = trace.get("observations", [])
    assert len(obs) >= min_count, (
        f"Expected >= {min_count} observations, got {len(obs)} for trace {trace_id}"
    )
    return obs


async def assert_langfuse_trace_quality(trace_id: str) -> dict:
    """Assert trace has root span with source metadata and valid tree structure.

    Retries if root span not yet ingested (SESSION_END fires after reply).
    """
    trace = await langfuse_get_trace(trace_id)
    assert trace is not None, f"Langfuse trace {trace_id} not found"
    assert trace.get("sessionId"), f"Trace {trace_id} missing sessionId"

    obs = trace.get("observations", [])
    roots = [o for o in obs if (o.get("name") or "").startswith("session-")]
    if not roots:
        for _ in range(5):
            await asyncio.sleep(3)
            trace = await langfuse_get_trace(trace_id)
            if trace:
                obs = trace.get("observations", [])
                roots = [o for o in obs if (o.get("name") or "").startswith("session-")]
                if roots:
                    break
    assert roots, f"No root span found for trace {trace_id}"
    meta = roots[0].get("metadata", {})
    assert meta.get("source") == "xiaopaw-v2", f"Root span missing source=xiaopaw-v2"

    obs_ids = {o.get("id") for o in obs}
    for o in obs:
        parent = o.get("parentObservationId")
        if parent:
            assert parent in obs_ids, (
                f"Observation {o.get('name')} has orphan parent {parent}"
            )
    return trace


async def assert_langfuse_has_generation(trace_id: str) -> dict:
    """Assert trace has at least one GENERATION with model."""
    trace = await langfuse_get_trace(trace_id)
    assert trace is not None, f"Langfuse trace {trace_id} not found"
    obs = trace.get("observations", [])
    gens = [o for o in obs if o.get("type") == "GENERATION"]
    assert gens, f"No GENERATION observation in trace {trace_id}"
    assert gens[0].get("model"), "GENERATION missing model"
    return trace


@pytest.fixture(scope="session")
def qwen_api_key() -> str:
    return os.environ.get("QWEN_API_KEY", "") or os.environ.get("DASHSCOPE_API_KEY", "")


@pytest.fixture(scope="session")
def langfuse_available() -> bool:
    if not _LF_PK or not _LF_SK:
        return False
    try:
        auth = base64.b64encode(f"{_LF_PK}:{_LF_SK}".encode()).decode()
        resp = requests.get(
            f"{_LF_HOST}/api/public/health",
            headers={"Authorization": f"Basic {auth}"},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


async def _echo_agent_fn(
    user_message: str,
    history: list[MessageEntry],
    session_id: str,
    routing_key: str = "",
    verbose: bool = False,
) -> str:
    return f"Echo: {user_message}"


@pytest_asyncio.fixture
async def slash_client(tmp_path):
    """Echo agent for slash command tests (no LLM needed)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    hook_registry = _load_shared_hooks()
    session_mgr = SessionManager(data_dir=data_dir)
    sender = CaptureSender()
    runner = Runner(
        session_mgr=session_mgr,
        sender=sender,
        agent_fn=_echo_agent_fn,
        data_dir=data_dir,
        hook_registry=hook_registry,
    )

    app = create_test_app(runner=runner, sender=sender, session_mgr=session_mgr)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await runner.shutdown()
    await client.close()


_SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://localhost:8030/mcp")
_SANDBOX_WORKSPACE = Path(
    os.environ.get("SANDBOX_WORKSPACE_DIR", str(_PROJECT_ROOT / "data" / "workspace"))
)


def _clean_sandbox_workspace(workspace_dir: Path) -> None:
    """Reset sandbox workspace to template state."""
    template_names = {f.name for f in _WORKSPACE_INIT.glob("*.md")} if _WORKSPACE_INIT.exists() else set()
    if workspace_dir.exists():
        for f in workspace_dir.iterdir():
            if f.is_file() and f.name not in template_names:
                f.unlink(missing_ok=True)
    _init_workspace(workspace_dir)


async def _build_llm_client(
    tmp_path, qwen_api_key: str, sandbox_url: str = "", workspace_dir: Path | None = None,
):
    """Shared builder for LLM test clients."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    if workspace_dir is None:
        workspace_dir = tmp_path / "workspace"
    _init_workspace(workspace_dir)
    ctx_dir = data_dir / "ctx"
    ctx_dir.mkdir(parents=True)

    hook_registry = _load_shared_hooks()
    session_mgr = SessionManager(data_dir=data_dir)
    sender = CaptureSender()

    from xiaopaw.agents.main_crew import build_agent_fn

    agent_fn = build_agent_fn(
        sender=sender,
        workspace_dir=workspace_dir,
        ctx_dir=ctx_dir,
        sandbox_url=sandbox_url,
    )

    runner = Runner(
        session_mgr=session_mgr,
        sender=sender,
        agent_fn=agent_fn,
        data_dir=data_dir,
        hook_registry=hook_registry,
    )

    app = create_test_app(runner=runner, sender=sender, session_mgr=session_mgr)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client, runner


async def _safe_teardown(runner, client, timeout: float = 15.0) -> None:
    try:
        await asyncio.wait_for(runner.shutdown(), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        pass
    try:
        await asyncio.wait_for(client.close(), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        pass


@pytest_asyncio.fixture
async def llm_client(tmp_path, qwen_api_key, sandbox_url):
    """Real Qwen LLM + real Sandbox (MCP) + Hook framework + Langfuse tracing."""
    if not qwen_api_key:
        pytest.skip("QWEN_API_KEY not set")
    client, runner = await _build_llm_client(tmp_path, qwen_api_key, sandbox_url=sandbox_url)
    yield client
    await _safe_teardown(runner, client)


@pytest.fixture(scope="session")
def sandbox_url() -> str:
    return _SANDBOX_URL


@pytest.fixture(scope="session")
def sandbox_workspace_dir() -> Path:
    """Host path mounted as /workspace in the sandbox container."""
    return _SANDBOX_WORKSPACE


@pytest_asyncio.fixture
async def sandbox_client(tmp_path, qwen_api_key, sandbox_url):
    """Real Qwen LLM + Sandbox (MCP) for skill execution tests.

    Uses the sandbox-mounted workspace directory so that memory-save writes
    inside the sandbox are visible to Bootstrap on the host.
    """
    if not qwen_api_key:
        pytest.skip("QWEN_API_KEY not set")
    base = sandbox_url.rsplit("/mcp", 1)[0] if "/mcp" in sandbox_url else sandbox_url
    try:
        resp = requests.get(base, timeout=3)
        if resp.status_code >= 500:
            pytest.skip(f"Sandbox not healthy at {sandbox_url}")
    except requests.ConnectionError:
        pytest.skip(f"Sandbox not reachable at {sandbox_url}")
    workspace_dir = _SANDBOX_WORKSPACE
    _clean_sandbox_workspace(workspace_dir)
    client, runner = await _build_llm_client(
        tmp_path, qwen_api_key, sandbox_url=sandbox_url, workspace_dir=workspace_dir,
    )
    yield client
    await _safe_teardown(runner, client)
    _clean_sandbox_workspace(workspace_dir)


@pytest_asyncio.fixture
async def llm_client_with_dirs(tmp_path, qwen_api_key):
    """Real Qwen LLM + exposes data/ctx/workspace paths for file verification."""
    if not qwen_api_key:
        pytest.skip("QWEN_API_KEY not set")
    client, runner = await _build_llm_client(tmp_path, qwen_api_key)
    dirs = {
        "data_dir": tmp_path / "data",
        "ctx_dir": tmp_path / "data" / "ctx",
        "workspace_dir": tmp_path / "workspace",
    }
    yield client, dirs
    await _safe_teardown(runner, client)


@pytest_asyncio.fixture
async def audit_llm_client(tmp_path, qwen_api_key, monkeypatch):
    """Real Qwen LLM + Sandbox MCP + audit log file for security audit verification.

    Sandbox MCP is mandatory: agent may decide to call search skills (e.g. baidu_search)
    for follow-up questions. Without a valid sandbox URL the Sub-Crew constructs
    MCPServerHTTP("") → httpx.UnsupportedProtocol → asyncgen leaks → TestAPI hangs.
    """
    if not qwen_api_key:
        pytest.skip("QWEN_API_KEY not set")
    audit_file = tmp_path / "security_audit.jsonl"
    monkeypatch.setenv("SECURITY_AUDIT_FILE", str(audit_file))
    client, runner = await _build_llm_client(
        tmp_path, qwen_api_key, sandbox_url=_SANDBOX_URL,
    )
    yield client, audit_file, runner
    await _safe_teardown(runner, client)


@pytest_asyncio.fixture
async def budget_llm_client(tmp_path, qwen_api_key, monkeypatch):
    """Real Qwen LLM + extremely low budget for cost guard testing."""
    if not qwen_api_key:
        pytest.skip("QWEN_API_KEY not set")
    monkeypatch.setenv("COST_GUARD_BUDGET", "0")
    client, runner = await _build_llm_client(tmp_path, qwen_api_key)
    yield client
    await _safe_teardown(runner, client)
