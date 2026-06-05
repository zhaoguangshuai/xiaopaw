"""Prometheus metrics definitions."""

from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram

    inbound_total = Counter(
        "xiaopaw_inbound_total",
        "Total inbound messages",
        ["source", "routing_type"],
    )
    llm_calls_total = Counter(
        "xiaopaw_llm_calls_total",
        "Total LLM API calls",
        ["model", "status"],
    )
    agent_latency = Histogram(
        "xiaopaw_agent_latency_seconds",
        "Agent processing latency",
        ["routing_type"],
        buckets=(1, 5, 10, 30, 60, 120, 300),
    )
    llm_latency = Histogram(
        "xiaopaw_llm_latency_seconds",
        "LLM API latency",
        ["model"],
        buckets=(0.5, 1, 2, 5, 10, 30, 60),
    )
    external_api_retry_total = Counter(
        "xiaopaw_external_api_retry_total",
        "External API retries",
        ["api"],
    )
    skill_timeout_total = Counter(
        "xiaopaw_skill_timeout_total",
        "Skill execution timeouts",
        ["skill"],
    )
    feishu_rate_limit_total = Counter(
        "xiaopaw_feishu_rate_limit_total",
        "Feishu API rate limit hits",
    )
    cron_dlq_total = Counter(
        "xiaopaw_cron_dlq_total",
        "Cron jobs sent to dead-letter queue",
    )

except ImportError:
    # Stub metrics when prometheus_client is not installed
    class _Stub:
        def labels(self, *a, **kw):
            return self
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass

    inbound_total = _Stub()
    llm_calls_total = _Stub()
    agent_latency = _Stub()
    llm_latency = _Stub()
    external_api_retry_total = _Stub()
    skill_timeout_total = _Stub()
    feishu_rate_limit_total = _Stub()
    cron_dlq_total = _Stub()
