"""Async turn indexer for pgvector semantic search."""

from __future__ import annotations

import hashlib
import logging
from functools import cache

logger = logging.getLogger(__name__)


@cache
def _get_llm_client():
    """Singleton OpenAI-compatible client for embeddings + summarization."""
    try:
        from openai import OpenAI
        import os
        return OpenAI(
            api_key=os.environ.get("QWEN_API_KEY", ""),
            base_url=os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
    except ImportError:
        logger.warning("openai package not installed, indexing disabled")
        return None


def _content_hash(session_id: str, turn_ts: int) -> str:
    raw = f"{session_id}:{turn_ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def async_index_turn(
    session_id: str,
    routing_key: str,
    user_message: str,
    assistant_reply: str,
    turn_ts: int,
    db_dsn: str,
) -> None:
    """Extract summary, embed, and upsert into pgvector. Fire-and-forget safe."""
    if not db_dsn:
        return

    client = _get_llm_client()
    if client is None:
        return

    try:
        content_id = _content_hash(session_id, turn_ts)

        summary_resp = client.chat.completions.create(
            model="qwen-turbo",
            messages=[
                {"role": "system", "content": "用一句中文总结以下对话的核心内容，提取关键实体和主题标签。"},
                {"role": "user", "content": f"用户：{user_message}\n助手：{assistant_reply[:500]}"},
            ],
            max_tokens=200,
        )
        summary = summary_resp.choices[0].message.content or ""

        embed_resp = client.embeddings.create(
            model="text-embedding-v3",
            input=[summary, user_message],
            dimensions=1024,
        )
        summary_vec = embed_resp.data[0].embedding
        message_vec = embed_resp.data[1].embedding

        search_text = f"{user_message} {summary}"

        import psycopg2
        conn = psycopg2.connect(db_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO memories
                       (id, session_id, routing_key, user_message, assistant_reply,
                        summary, tags, turn_ts, summary_vec, message_vec, search_text)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (
                        content_id, session_id, routing_key,
                        user_message, assistant_reply[:2000],
                        summary, [], turn_ts,
                        str(summary_vec), str(message_vec), search_text,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        logger.info("indexed turn %s for session %s", content_id, session_id)

    except Exception:
        logger.exception("index_turn failed for session %s", session_id)
