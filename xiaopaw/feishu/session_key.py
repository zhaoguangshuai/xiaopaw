"""Routing key resolution from Feishu events."""

from __future__ import annotations


def resolve_routing_key(
    chat_type: str,
    chat_id: str,
    open_id: str,
    thread_id: str = "",
) -> str:
    if thread_id:
        return f"thread:{chat_id}:{thread_id}"
    if chat_type == "p2p":
        return f"p2p:{open_id}"
    return f"group:{chat_id}"


def routing_type(routing_key: str) -> str:
    return routing_key.split(":")[0] if ":" in routing_key else "unknown"
