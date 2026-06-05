"""Pydantic configuration schemas with startup validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from xiaopaw.config.flags import FeatureFlags


class FeishuConfig(BaseModel):
    app_id: str = Field(min_length=8)
    app_secret: str = Field(min_length=8)
    allowed_chats: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model: str = "qwen3-max"
    max_iter: int = Field(default=50, ge=1, le=200)
    max_input_tokens: int = Field(default=30000, ge=1000, le=128000)
    sub_agent_model: str = "qwen3-max"
    sub_agent_max_iter: int = Field(default=20, ge=1, le=100)
    timeout_s: int = Field(default=300, ge=30, le=3600)
    llm_timeout_s: int = Field(default=120, ge=10, le=600)


class SandboxConfig(BaseModel):
    url: str = "http://localhost:8030/mcp"
    timeout_s: int = Field(default=120, ge=10, le=600)


class MemoryConfig(BaseModel):
    db_dsn: str = ""
    hard_limit_lines: int = 250
    max_save_length: int = 2000
    compress_threshold: float = 0.45
    context_window_tokens: int = 32000
    fresh_keep_turns: int = 10


class SessionConfig(BaseModel):
    max_active_sessions: int = Field(default=1000, ge=1)
    max_history_turns: int = Field(default=20, ge=1)


class RunnerConfig(BaseModel):
    max_queue_size: int = Field(default=10, ge=1, le=100)
    idle_timeout_s: float = Field(default=300.0, ge=10)


class SenderConfig(BaseModel):
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_backoff: list[float] = Field(default_factory=lambda: [1.0, 2.0, 4.0])
    max_concurrent: int = Field(default=5, ge=1, le=20)


class DebugConfig(BaseModel):
    enable_test_api: bool = False
    test_api_host: str = "127.0.0.1"
    test_api_port: int = Field(default=9090, ge=1024, le=65535)
    test_api_token: str = ""


class ObservabilityConfig(BaseModel):
    metrics_host: str = "0.0.0.0"
    metrics_port: int = Field(default=8090, ge=1024, le=65535)
    log_json: bool = True


class RateLimitConfig(BaseModel):
    per_user_per_minute: int = Field(default=20, ge=1)


class ReplayCacheConfig(BaseModel):
    maxsize: int = Field(default=10000, ge=100)
    ttl_sec: float = Field(default=300.0, ge=10)


class CronConfig(BaseModel):
    enabled: bool = True
    check_interval_s: float = Field(default=30.0, ge=5)
    filelock_timeout_s: float = Field(default=10.0, ge=1)
    max_dlq_retries: int = Field(default=3, ge=0)


class CleanupConfig(BaseModel):
    enabled: bool = True
    session_ttl_days: int = Field(default=180, ge=1)
    trace_ttl_days: int = Field(default=30, ge=1)
    raw_ttl_days: int = Field(default=30, ge=1)
    run_hour_utc: int = Field(default=3, ge=0, le=23)


class AppConfig(BaseModel):
    workspace: str = "data/workspace"
    data_dir: str = "data"
    feishu: FeishuConfig = Field(default_factory=lambda: FeishuConfig(app_id="placeholder", app_secret="placeholder"))
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    sender: SenderConfig = Field(default_factory=SenderConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    replay_cache: ReplayCacheConfig = Field(default_factory=ReplayCacheConfig)
    cron: CronConfig = Field(default_factory=CronConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)


def load_config(path: Path) -> AppConfig:
    """Load and validate configuration from YAML file."""
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig(**raw)
