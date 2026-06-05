"""TestAPI request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TestAttachment(BaseModel):
    file_path: str
    file_name: str | None = None


class TestRequest(BaseModel):
    routing_key: str
    content: str = ""
    msg_id: str | None = None
    sender_id: str = "ou_test001"
    attachment: TestAttachment | None = None


class TestResponse(BaseModel):
    msg_id: str
    reply: str
    session_id: str
    duration_ms: int
    skills_called: list[str] = Field(default_factory=list)
