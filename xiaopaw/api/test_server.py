"""TestAPI server: simulate Feishu events via HTTP for development/testing."""

from __future__ import annotations

import logging
import time
import uuid

from aiohttp import web

from xiaopaw.api.capture_sender import CaptureSender
from xiaopaw.api.schemas import TestRequest
from xiaopaw.models import InboundMessage
from xiaopaw.observability.trace import new_trace_id

logger = logging.getLogger(__name__)


def create_test_app(
    runner=None,
    sender: CaptureSender | None = None,
    session_mgr=None,
    token: str = "",
) -> web.Application:
    app = web.Application()
    app["runner"] = runner
    app["sender"] = sender or CaptureSender()
    app["session_mgr"] = session_mgr
    app["token"] = token

    app.router.add_post("/api/test/message", _handle_message)
    app.router.add_delete("/api/test/sessions", _handle_clear)

    return app


def _check_auth(request: web.Request) -> bool:
    token = request.app.get("token", "")
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {token}"


async def _handle_message(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
        req = TestRequest(**body)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=422)

    runner = request.app["runner"]
    capture: CaptureSender = request.app["sender"]
    session_mgr = request.app.get("session_mgr")

    msg_id = req.msg_id or f"test_{uuid.uuid4().hex[:12]}"
    future = capture.register(msg_id)

    inbound = InboundMessage(
        routing_key=req.routing_key,
        content=req.content,
        msg_id=msg_id,
        sender_id=req.sender_id,
        ts=int(time.time() * 1000),
        trace_id=new_trace_id(),
    )

    start = time.monotonic()
    await runner.dispatch(inbound)

    try:
        reply = await capture.wait_for_reply(msg_id, timeout=550.0)
    except asyncio.TimeoutError:
        reply = "[timeout]"

    duration_ms = int((time.monotonic() - start) * 1000)

    session_id = ""
    if session_mgr:
        session = await session_mgr.get_or_create(req.routing_key)
        session_id = session.id

    return web.json_response({
        "msg_id": msg_id,
        "reply": reply,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "trace_id": inbound.trace_id,
    })


async def _handle_clear(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    session_mgr = request.app.get("session_mgr")
    if session_mgr:
        await session_mgr.clear_all()

    return web.json_response({"status": "ok"})


import asyncio  # noqa: E402 (needed for TimeoutError in _handle_message)
