"""Trace ID propagation via contextvars."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import uuid
from typing import Any, Callable, TypeVar

T = TypeVar("T")

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_trace_id(trace_id: str) -> contextvars.Token[str]:
    return trace_id_var.set(trace_id)


async def run_in_executor_with_context(
    fn: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        None, functools.partial(ctx.run, fn, *args, **kwargs)
    )
