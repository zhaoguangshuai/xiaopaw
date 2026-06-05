"""Feature flags for progressive rollout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FeatureFlags:
    token_counter_mode: Literal["qwen_official", "hf_qwen", "rough"] = "qwen_official"
    enable_skill_timeout: bool = True
    enable_cron_filelock: bool = True
    enable_memory_save_filelock: bool = True
    enable_feishu_rate_limit_aware: bool = True
    enable_trace_id: bool = True
    enable_mcp_whitelist: bool = True
    enable_memory_save_filter: bool = True
    enable_webhook_replay_cache: bool = True
    enable_inbound_rate_limit: bool = True
    enable_pgvector_rls: bool = False
    enable_pgvector_connection_pool: bool = True
