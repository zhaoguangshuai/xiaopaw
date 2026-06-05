"""Cron job data models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CronJob(BaseModel, extra="forbid"):
    id: str
    routing_key: str
    cron_expr: str
    content: str
    enabled: bool = True
    description: str = ""
    fail_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
