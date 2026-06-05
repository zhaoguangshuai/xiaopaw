# SSOT｜Feature Flags 清单

> **Single Source of Truth**。所有文档提及 feature flag 时必须引用本清单。
> 版本：v2.1 | 最后更新：2026-04-19

---

## 1. Flag 清单（v2.1 终表）

| # | Flag | 类型 | 默认 | 对应缺陷 / 威胁 | 回滚风险 | 热重载 | 对应 metric |
|---|---|---|---|---|---|---|---|
| F1 | `token_counter_mode` | Literal["qwen_official", "hf_qwen", "rough"] | `qwen_official` | H3 token 计数精度 | 低 | ⚠️（清 tokenizer cache） | N/A |
| F2 | `enable_skill_timeout` | bool | `true` | H2 Sub-Crew 卡死 | 低（关闭=v1） | ✅ | `xiaopaw_skill_timeout_total` |
| F3 | `enable_cron_filelock` | bool | `true` | H8 Cron 跨进程 | 低（关闭=v1） | ❌（需重启 CronService） | `xiaopaw_cron_lock_timeout_total` |
| F4 | `enable_memory_save_filelock` | bool | `true` | H9 并发覆盖 | 低（关闭=v1） | ✅ | `xiaopaw_memory_save_timeout_total` |
| F5 | `enable_feishu_rate_limit_aware` | bool | `true` | H6 429 识别 | 低（关闭=v1） | ✅ | `xiaopaw_feishu_rate_limit_total` |
| F6 | `enable_trace_id` | bool | `true` | 横切（观测） | 低 | ❌（ContextVar 已嵌入各模块） | 全部指标的 exemplar |
| F7 | `enable_mcp_whitelist` | bool | `true`（prod） / `false`（dev 开发） | T1 Prompt Injection → sandbox 逃逸 | **高** | ❌（SkillLoaderTool 初始化时固化） | `xiaopaw_mcp_tool_filtered_total` |
| F8 | `enable_memory_save_filter` | bool | `true` | T2 Memory Poisoning | **中高** | ✅ | `xiaopaw_memory_save_blocked_total` |
| F9 | `enable_webhook_replay_cache` | bool | `true` | T3 飞书 webhook 重放（注：WS 模式 SDK 无验签，这是应用层防御） | **高** | ✅ | `xiaopaw_webhook_replay_hit_total` |
| F10 | `enable_inbound_rate_limit` | bool | `true` | T7 DoS | 中 | ✅ | `xiaopaw_rate_limited_total` |
| F11 | `enable_pgvector_rls` | bool | `false`（单租户） / `true`（多租户 prod） | T11 routing_key 泄露（DB 层兜底） | 低 | ❌（需 SQL 切换 + 重启） | N/A |
| F12 | `enable_pgvector_connection_pool` | bool | `true` | 性能（v2 新增） | 低 | ❌（需重启 indexer） | `xiaopaw_pgvector_pool_size` |

---

## 2. v2.1 Flag 变更（from review）

### 新增
- **F9 `enable_webhook_replay_cache`**：原 v2.0 的 `enable_webhook_signature` **改名**。原因：WebSocket 模式下 lark-oapi 不做验签（SDK 限制），v2.1 把 T3 防御聚焦到应用层 ReplayCache（event_id LRU+TTL）。

### 删除
- ~~`enable_webhook_signature`~~：不存在的 SDK API，整体移除。

### 语义变更
- **F1 `token_counter_mode`**：v2.0 列为二值（qwen_official / rough），v2.1 扩为三值，覆盖 HuggingFace fallback。
- **F12 `enable_pgvector_connection_pool`**：v2.0 的 03-data 列但 09-config 未列，v2.1 补进注册表。

---

## 3. 启动校验约束（prod 环境）

`xiaopaw/config/safety.py::assert_production_safe(cfg)` 在 `XIAOPAW_ENV=prod` 时强制以下约束：

```python
REQUIRED_ON_IN_PROD = [
    "enable_skill_timeout",
    "enable_cron_filelock",
    "enable_memory_save_filelock",
    "enable_feishu_rate_limit_aware",
    "enable_mcp_whitelist",
    "enable_memory_save_filter",
    "enable_webhook_replay_cache",
    "enable_inbound_rate_limit",
    "enable_pgvector_connection_pool",
]

for name in REQUIRED_ON_IN_PROD:
    if not getattr(cfg.feature_flags, name):
        raise RuntimeError(f"prod 禁止关闭 {name}")
```

**允许 prod 关闭**：`enable_trace_id`（若性能有问题可临时关）；`enable_pgvector_rls`（单租户默认关）。

---

## 4. dataclass 定义（v2.1）

```python
# xiaopaw/config/flags.py
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class FeatureFlags:
    # Tokenizer（F1）
    token_counter_mode: Literal["qwen_official", "hf_qwen", "rough"] = "qwen_official"

    # 并发容错（F2-F5）
    enable_skill_timeout: bool = True
    enable_cron_filelock: bool = True
    enable_memory_save_filelock: bool = True
    enable_feishu_rate_limit_aware: bool = True

    # 观测（F6）
    enable_trace_id: bool = True

    # 安全（F7-F10）
    enable_mcp_whitelist: bool = True
    enable_memory_save_filter: bool = True
    enable_webhook_replay_cache: bool = True
    enable_inbound_rate_limit: bool = True

    # pgvector（F11-F12）
    enable_pgvector_rls: bool = False
    enable_pgvector_connection_pool: bool = True

    @classmethod
    def from_config(cls, cfg: dict) -> "FeatureFlags":
        raw = cfg.get("feature_flags", {})
        # 拒绝未知字段（防拼写错误）
        known = {f.name for f in fields(cls)}
        unknown = set(raw.keys()) - known
        if unknown:
            raise ValueError(f"未知 feature flag 字段: {unknown}")
        return cls(**raw)
```

---

## 5. config.yaml.example Feature Flag 节（v2.1 终版）

```yaml
feature_flags:
  # Tokenizer
  token_counter_mode: "qwen_official"       # qwen_official / hf_qwen / rough

  # 并发与容错
  enable_skill_timeout:            true
  enable_cron_filelock:            true
  enable_memory_save_filelock:     true
  enable_feishu_rate_limit_aware:  true

  # 可观测
  enable_trace_id:                 true

  # 安全
  enable_mcp_whitelist:            true   # 开发环境可改 false
  enable_memory_save_filter:       true
  enable_webhook_replay_cache:     true   # v2.1：原 enable_webhook_signature 改名
  enable_inbound_rate_limit:       true

  # pgvector
  enable_pgvector_rls:             false  # 多租户部署时开
  enable_pgvector_connection_pool: true   # v2.1 新增
```

---

## 6. Metric 暴露

启动时所有 flag 的当前值暴露为 metric：

```python
xiaopaw_feature_flag = Gauge(
    "xiaopaw_feature_flag",
    "Current value of a feature flag (1=on, 0=off).",
    ["name"],
)

for name, value in asdict(flags).items():
    if isinstance(value, bool):
        xiaopaw_feature_flag.labels(name=name).set(1 if value else 0)
    else:  # Literal 字符串
        xiaopaw_feature_flag.labels(name=f"{name}:{value}").set(1)
```

Grafana dashboard 可直接看每个 flag 的启停状态。

---

## 7. 测试锚点

- TC-P1-11-a｜FeatureFlags dataclass 字段数 == 12（防漂移）
- TC-P1-11-b｜config.yaml.example 字段与 dataclass 一一对应
- TC-P1-2-b｜prod 环境关闭 enable_mcp_whitelist → 启动失败
- TC-P1-2-c｜prod 环境关闭 enable_webhook_replay_cache → 启动失败
- TC-F1｜token_counter_mode=rough → 降级到 len//2
- TC-F11｜enable_pgvector_rls=true → SET LOCAL xiaopaw.current_routing_key 生效
