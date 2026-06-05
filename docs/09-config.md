# 09 配置规范与 Feature Flags

> 本文是 [DESIGN.md](../DESIGN.md) §13 的详细展开。
> 读者：运维 / 实现工程师。
> 最后更新：2026-04-19（v2.1）
>
> **权威清单（SSOT）**：
> - Feature Flags 以 [`ssot/feature-flags.md`](ssot/feature-flags.md) 为准（本文 §4 与之对齐）
> - 端口以 [`ssot/ports.md`](ssot/ports.md) 为准（本文 §2 引用）
> - 威胁 / 启动校验见 [`07-security.md §9.3`](07-security.md)
>
> **v2.1 修订重点**：
> - 删除 `observability.health_port`（与 `metrics_port` 同端口，v2.0 就是冗余字段）
> - `enable_webhook_signature` → `enable_webhook_replay_cache`（SDK 不存在该参数，原 flag 名误导）
> - `sandbox.url` 端口 8022 → 8080（8022 是 v1 host 映射，容器间调用应走内部 8080）
> - 启动校验合并为 `assert_all_production_safe(cfg)` 单入口
> - Feature flag dataclass 加"未知字段拒绝"逻辑

---

## 目录

1. [配置分层与优先级](#1-配置分层与优先级)
2. [config.yaml 完整字段](#2-configyaml-完整字段)
3. [.env 字段清单](#3-env-字段清单)
4. [Feature Flags registry](#4-feature-flags-registry)
5. [启动校验 config.safety](#5-启动校验-configsafety)
6. [热重载（SIGHUP）](#6-热重载sighup)
7. [配置变更管理](#7-配置变更管理)
8. [不同环境的配置差异](#8-不同环境的配置差异)
9. [v1 → v2 配置迁移](#9-v1--v2-配置迁移)

---

## 1. 配置分层与优先级

### 1.1 分层

XiaoPaw v2 的配置按"公开度 / 敏感度"分四层：

| 层 | 内容 | 提交到 git | 生命周期 |
|---|---|---|---|
| L0 模板 | `config.yaml.example` / `.env.example` | ✅ | 永久 |
| L1 运维配置 | `config.yaml`（不含密钥） | ❌（`.gitignore`） | 环境生命周期 |
| L2 凭证 | `.env`（所有密钥） | ❌（`.gitignore` + `chmod 0400`） | 凭证轮换周期（90 天） |
| L3 命令行 | `--config /path/to/config.yaml` | N/A | 单次运行 |

### 1.2 优先级（高 → 低）

```
命令行参数 --config
    ↓ (覆盖)
环境变量 XIAOPAW_*
    ↓ (覆盖)
config.yaml
    ↓ (fallback)
代码默认值（xiaopaw/config/validator.py 里 Pydantic Field default）
```

### 1.3 加载顺序

```python
# xiaopaw/main.py
def load_config() -> Config:
    # 1. 命令行解析
    args = _parse_argv()
    config_path = args.config or os.getenv("XIAOPAW_CONFIG") or "config.yaml"
    # 2. 读 YAML
    raw = yaml.safe_load(Path(config_path).read_text())
    # 3. 环境变量替换（${VAR} / ${VAR:-default}）
    raw = _expand_env_vars(raw)
    # 4. Pydantic 校验
    cfg = Config.model_validate(raw)
    # 5. safety 额外校验（v2.1 单入口，见 §5 和 07-security §9.3）
    assert_all_production_safe(cfg)
    return cfg
```

---

## 2. config.yaml 完整字段

`config.yaml.example` 提供模板；运维复制一份 `config.yaml` 并按需修改。

```yaml
# ──────────────────────────────────────────────────────────
# Workspace
# ──────────────────────────────────────────────────────────
workspace:
  id: "xiaopaw-default"
  name: "XiaoPaw 工作助手"

# ──────────────────────────────────────────────────────────
# 飞书接入（凭证见 .env）
# ──────────────────────────────────────────────────────────
feishu:
  app_id: "${FEISHU_APP_ID}"                  # 必填
  app_secret: "${FEISHU_APP_SECRET}"          # 必填
  encrypt_key: "${FEISHU_ENCRYPT_KEY}"        # v2 required
  verification_token: "${FEISHU_VERIFICATION_TOKEN}"  # v2 required
  allowed_chats: []                            # 空 = 允许所有群；否则白名单

bot:
  loading_message: "⏳ 思考中..."
  prefix: ""                                   # 消息前缀（可选）

# ──────────────────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────────────────
agent:
  model: "qwen3-max"
  max_iter: 50
  max_input_tokens: 30000
  sub_agent_model: "qwen3-max"
  sub_agent_max_iter: 20
  timeout_s: 300
  llm_timeout_s: 120

# ──────────────────────────────────────────────────────────
# Skills
# ──────────────────────────────────────────────────────────
skills:
  global_dir: "../skills"                      # 可选的全局 skill 目录
  local_dir: "./skills"                         # 项目内 skill 目录

# ──────────────────────────────────────────────────────────
# AIO-Sandbox MCP
# ──────────────────────────────────────────────────────────
sandbox:
  url: "http://aio-sandbox:8080/mcp"          # docker network 内部地址；端口见 ssot/ports.md
                                               # v2.1 修正：v2.0 曾写 8022（v1 host 映射），容器间应走 8080
  workspace_dir: "/workspace"                   # 沙盒内 mount 根
  timeout_s: 120                                # Skill 单次超时
  max_retries: 2

# ──────────────────────────────────────────────────────────
# 三层记忆
# ──────────────────────────────────────────────────────────
memory:
  workspace_dir: "./data/workspace"
  ctx_dir: "./data/ctx"
  db_dsn: "${MEMORY_DB_DSN}"                  # 空串 = 禁用搜索记忆
  max_memory_file_lines: 250                    # memory.md 硬上限
  memory_notify_lines: 150                      # 告警阈值
  memory_warn_lines: 180                        # 警告阈值

# ──────────────────────────────────────────────────────────
# Session
# ──────────────────────────────────────────────────────────
session:
  max_history_turns: 20
  max_active_sessions: 1000                     # LRUCache 上限

# ──────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────
runner:
  queue_idle_timeout_s: 300
  max_queue_size: 10

# ──────────────────────────────────────────────────────────
# FeishuSender
# ──────────────────────────────────────────────────────────
sender:
  max_retries: 3
  retry_backoff: [1, 2, 4]
  max_concurrent: 5                            # Semaphore 并发上限

# ──────────────────────────────────────────────────────────
# 百度搜索（可选）
# ──────────────────────────────────────────────────────────
baidu:
  api_key: "${BAIDU_API_KEY:-}"                # 空 = 禁用 baidu_search Skill

# ──────────────────────────────────────────────────────────
# 数据目录
# ──────────────────────────────────────────────────────────
data_dir: "./data"

# ──────────────────────────────────────────────────────────
# Debug / TestAPI
# ──────────────────────────────────────────────────────────
debug:
  enable_test_api: false                       # prod 必须 false
  test_api_host: "127.0.0.1"                   # 仅 loopback
  test_api_port: 9090

# ──────────────────────────────────────────────────────────
# 可观测性
# ──────────────────────────────────────────────────────────
observability:
  log_dir: "./data/logs"
  log_level: "INFO"                            # DEBUG / INFO / WARNING / ERROR
  log_json_file: true
  log_console_human: true
  metrics_port: 8090                           # v2.1：对齐 ssot/ports.md（同端口承载 /metrics + /health）
  # health_port: 已删除（v2.0 该字段与 metrics_port 同值，纯冗余；同一 aiohttp Application 分路由暴露）
  trace:
    enabled: true
    sample_rate: 1.0
    always_sample_on_error: true
    slash_commands_sample: 0.0

# ──────────────────────────────────────────────────────────
# Rate Limiter（入站）
# ──────────────────────────────────────────────────────────
rate_limit:
  per_user_per_minute: 20
  global_per_minute: 1000

# ──────────────────────────────────────────────────────────
# Replay Cache（webhook 去重）
# ──────────────────────────────────────────────────────────
replay_cache:
  maxsize: 10000
  ttl_sec: 300

# ──────────────────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────────────────
cleanup:
  session_retention_days: 180
  trace_retention_days: 30
  raw_audit_retention_days: 30
  memory_retention_days: 180
  sweep_hour_utc: 19                           # 北京时间 03:00 运行

# ──────────────────────────────────────────────────────────
# Cron
# ──────────────────────────────────────────────────────────
cron:
  tasks_file: "./data/cron/tasks.json"
  dlq_file: "./data/cron/tasks.dlq.jsonl"
  max_retries: 3
  tick_interval_s: 5
  filelock_timeout_s: 10

# ──────────────────────────────────────────────────────────
# Feature Flags（v2 核心新增；SSOT: ssot/feature-flags.md）
# ──────────────────────────────────────────────────────────
feature_flags:
  # Tokenizer（F1）
  token_counter_mode: "qwen_official"          # qwen_official / hf_qwen / rough
  # 并发容错（F2-F5）
  enable_skill_timeout: true
  enable_cron_filelock: true
  enable_memory_save_filelock: true
  enable_feishu_rate_limit_aware: true
  # 观测（F6）
  enable_trace_id: true
  # 安全（F7-F10）
  enable_mcp_whitelist: true                    # 开发环境可改 false
  enable_memory_save_filter: true
  enable_webhook_replay_cache: true             # v2.1：原 enable_webhook_signature 改名
                                                 # 原字段 SDK 不存在（sdk-verification-report.md 已证实）
  enable_inbound_rate_limit: true
  # pgvector（F11-F12）
  enable_pgvector_rls: false                    # 多租户部署时开
  enable_pgvector_connection_pool: true         # v2.1 正式入 registry
```

### 2.1 Pydantic Schema（`xiaopaw/config/validator.py`）

```python
from pydantic import BaseModel, Field, field_validator, HttpUrl

class FeishuConfig(BaseModel):
    app_id: str = Field(min_length=8)
    app_secret: str = Field(min_length=8)
    encrypt_key: str = Field(min_length=16)   # v2 required
    verification_token: str = Field(min_length=8)
    allowed_chats: list[str] = Field(default_factory=list)

class AgentConfig(BaseModel):
    model: str = "qwen3-max"
    max_iter: int = Field(default=50, ge=1, le=200)
    max_input_tokens: int = Field(default=30000, ge=1000, le=128000)
    sub_agent_model: str = "qwen3-max"
    sub_agent_max_iter: int = Field(default=20, ge=1, le=100)
    timeout_s: int = Field(default=300, ge=30, le=3600)
    llm_timeout_s: int = Field(default=120, ge=10, le=600)

class MemoryConfig(BaseModel):
    workspace_dir: str
    ctx_dir: str
    db_dsn: str = ""   # 空 = 禁用
    max_memory_file_lines: int = Field(default=250, ge=100, le=5000)
    memory_notify_lines: int = Field(default=150)
    memory_warn_lines: int = Field(default=180)

    @field_validator("memory_warn_lines")
    @classmethod
    def warn_lt_max(cls, v, info):
        if v >= info.data.get("max_memory_file_lines", 250):
            raise ValueError("memory_warn_lines must < max_memory_file_lines")
        return v

class DebugConfig(BaseModel):
    enable_test_api: bool = False
    test_api_host: str = "127.0.0.1"
    test_api_port: int = Field(default=9090, ge=1024, le=65535)

    @field_validator("test_api_host")
    @classmethod
    def loopback_only(cls, v):
        if v not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(f"test_api_host 必须是 loopback: {v}")
        return v

class Config(BaseModel):
    workspace: dict
    feishu: FeishuConfig
    agent: AgentConfig
    memory: MemoryConfig
    # ... 其他子段 ...
    feature_flags: FeatureFlags
```

---

## 3. .env 字段清单

`.env.example` 只列 key 名，无任何值：

```bash
# ──────────────────────────────────────────────────────────
# 环境标识
# ──────────────────────────────────────────────────────────
XIAOPAW_ENV=dev            # dev / canary / prod
XIAOPAW_GIT_SHA=            # Dockerfile build --build-arg 注入

# ──────────────────────────────────────────────────────────
# 飞书
# ──────────────────────────────────────────────────────────
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_ENCRYPT_KEY=
FEISHU_VERIFICATION_TOKEN=

# ──────────────────────────────────────────────────────────
# Qwen
# ──────────────────────────────────────────────────────────
QWEN_API_KEY=
QWEN_DEBUG_PAYLOAD=0        # 1 = 输出完整 LLM payload

# ──────────────────────────────────────────────────────────
# pgvector
# ──────────────────────────────────────────────────────────
MEMORY_DB_DSN=              # postgresql://xiaopaw_app:xxx@pgvector:5432/xiaopaw_memory?sslmode=require

# ──────────────────────────────────────────────────────────
# 百度搜索（可选）
# ──────────────────────────────────────────────────────────
BAIDU_API_KEY=

# ──────────────────────────────────────────────────────────
# TestAPI（仅 dev）
# ──────────────────────────────────────────────────────────
XIAOPAW_TESTAPI_TOKEN=       # ≥32 字符

# ──────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────
XIAOPAW_METRICS_TOKEN=       # prod 必填，≥32 字符

# ──────────────────────────────────────────────────────────
# Sentry（可选）
# ──────────────────────────────────────────────────────────
XIAOPAW_SENTRY_DSN=
```

### 3.1 凭证强度要求

| 变量 | 最小长度 | 字符集 |
|---|---|---|
| `XIAOPAW_TESTAPI_TOKEN` | 32 | 字母+数字+符号 |
| `XIAOPAW_METRICS_TOKEN` | 32 | 同上 |
| `FEISHU_ENCRYPT_KEY` | 16 | 飞书生成的 16 位 hex |
| `MEMORY_DB_DSN` 密码部分 | 16 | 强密码 |

### 3.2 不允许的值（`FORBIDDEN_DEFAULTS`）

启动时正则 + SHA256 hash 双层匹配拒绝：

```python
_WEAK_PASSWORDS = [
    "xiaopaw123", "postgres", "admin", "root", "123456", "password",
    "dummy_secret_for_local_test", "cli_test_dummy",
]
_WEAK_PATTERN = re.compile(
    r"(xiaopaw|dummy|test|admin|root|password)\s*[\d!@#$%^&*]*$", re.I
)
```

检测到 → 启动失败（prod 环境）。

---

## 4. Feature Flags registry

### 4.1 注册表实现（v2.1 — 对齐 [`ssot/feature-flags.md`](ssot/feature-flags.md)）

```python
# xiaopaw/config/flags.py
from dataclasses import dataclass, fields
from typing import Literal

@dataclass(frozen=True)
class FeatureFlags:
    # Tokenizer（F1）
    token_counter_mode: Literal["qwen_official", "hf_qwen", "rough"] = "qwen_official"

    # 并发容错（F2-F5 / H 系列）
    enable_skill_timeout: bool = True
    enable_cron_filelock: bool = True
    enable_memory_save_filelock: bool = True
    enable_feishu_rate_limit_aware: bool = True

    # 观测（F6 横切）
    enable_trace_id: bool = True

    # 安全（F7-F10 / T 系列）
    enable_mcp_whitelist: bool = True
    enable_memory_save_filter: bool = True
    enable_webhook_replay_cache: bool = True   # v2.1：原 enable_webhook_signature 改名
    enable_inbound_rate_limit: bool = True

    # pgvector（F11-F12）
    enable_pgvector_rls: bool = False
    enable_pgvector_connection_pool: bool = True

    @classmethod
    def from_config(cls, cfg: dict) -> "FeatureFlags":
        """
        v2.1：未知 feature flag 字段直接 raise，防拼写错误或历史字段残留
        （如 config.yaml 里还写着 `enable_webhook_signature` 会被立即发现）。
        """
        raw = cfg.get("feature_flags", {})
        known = {f.name for f in fields(cls)}
        unknown = set(raw.keys()) - known
        if unknown:
            raise ValueError(
                f"未知 feature flag 字段: {unknown}。"
                f"合法字段见 ssot/feature-flags.md。"
            )
        return cls(**raw)
```

### 4.2 Flag ↔ 缺陷编号映射

每个 flag 都对应 [v1 Review 报告](../../../multi-agent/review_L22/01_Review报告_xiaopaw-with-memory.md) 里的一个缺陷编号：

| Flag (SSOT 编号) | 对应缺陷 / 威胁 | 默认值 | 开发环境推荐值 |
|---|---|---|---|
| F2 `enable_skill_timeout` | H2 Sub-Crew 卡死 | true | true |
| F3 `enable_cron_filelock` | H8 Cron 跨进程 | true | true（单节点也需要） |
| F4 `enable_memory_save_filelock` | H9 并发覆盖 | true | true |
| F5 `enable_feishu_rate_limit_aware` | H6 429 识别 | true | true |
| F7 `enable_mcp_whitelist` | T1 Prompt Injection | true | **false**（开发环境需全 MCP） |
| F8 `enable_memory_save_filter` | T2 Memory Poisoning + T8 Cron 注入 | true | true |
| F9 `enable_webhook_replay_cache` | T3 WS 重放（SDK 不做 dedup） | true | true |
| F10 `enable_inbound_rate_limit` | T7 DoS | true | false（本地测试方便） |
| F11 `enable_pgvector_rls` | T11 routing_key 泄露（DB 层兜底） | false | false（单用户不需要） |
| F12 `enable_pgvector_connection_pool` | 性能（v2 新增） | true | true |
| F1 `token_counter_mode` | H3 token 估算 | qwen_official | rough（无网环境兼容） |

### 4.3 Flag 影响面 matrix

| Flag | 影响模块 | 影响行为 | 回滚风险 |
|---|---|---|---|
| `enable_skill_timeout` | `SkillLoaderTool` | 超时杀 / 不杀 | 低：关闭后 v1 等价 |
| `enable_cron_filelock` | `CronStorage` | filelock 进/出 | 低：关闭后单进程原子写（v1 等价） |
| `enable_memory_save_filelock` | `memory-save` Skill | topic 锁 | 低：关闭后可能覆盖（v1 等价） |
| `enable_feishu_rate_limit_aware` | `FeishuSender` | 真 429 识别 | 低：关闭后固定退避（v1 等价） |
| `enable_mcp_whitelist` | `build_skill_crew` | 白名单 vs 全量 | **高**：从严→宽可能暴露 prompt injection 路径 |
| `enable_memory_save_filter` | `memory-save` Skill + CronService dispatch | BLOCKED_PATTERNS | 中：关闭后可能污染 memory.md / Cron 载荷 |
| `enable_webhook_replay_cache` | `FeishuListener` | event_id 去重 on/off | **高**：关闭后重放事件被重复派发 |
| `enable_inbound_rate_limit` | `FeishuListener` | RateLimiter on/off | 中 |
| `enable_pgvector_rls` | search_memory SQL | DB 层隔离 | 低：已有应用层防护 |
| `enable_pgvector_connection_pool` | `MemoryIndexer` | 连接池 on/off | 低：关闭后每次新建连接（性能下降） |
| `token_counter_mode` | `context_mgmt.maybe_compress` | 精算 / 粗估 / HF fallback | 低：compress 阈值调整 |

### 4.4 Metric 埋点

```python
# xiaopaw/observability/metrics.py
xiaopaw_feature_flag = Gauge(
    "xiaopaw_feature_flag",
    "Current value of a feature flag (1=on, 0=off).",
    ["name"],
)

# 启动时
for name, value in asdict(flags).items():
    xiaopaw_feature_flag.labels(name=name).set(1 if value else 0)
```

Grafana dashboard 可直接看到当前每个 flag 的状态。

---

## 5. 启动校验 config.safety

### 5.1 两层校验（v2.1 单入口）

**第一层**：Pydantic schema（类型、范围、必填）—— 见 §2.1。

**第二层**：`assert_all_production_safe(cfg)` —— 环境敏感性检查。v2.1 把原来散落的 `assert_production_safe` / `assert_production_flags` / `assert_production_credentials` 等多入口合并为单入口，避免漏调：

```python
# xiaopaw/config/safety.py

def assert_all_production_safe(cfg: Config) -> None:
    """XIAOPAW_ENV=prod 时启动强制调用。与 07-security.md §9.3 保持一致。"""
    _assert_credentials_strength(cfg)          # 飞书 / qwen / db / metrics 凭证强度
    _assert_testapi_constraints(cfg)           # TestAPI 必须 loopback + prod 禁用
    _assert_production_flags(cfg.feature_flags)  # REQUIRED_ON_IN_PROD（见下）
    _assert_network_constraints(cfg)           # sandbox.url 内网、metrics_token 必填 etc.
    if cfg.feature_flags.enable_mcp_whitelist:
        _validate_all_skills_have_allowed_tools(cfg.skills.local_dir)


# REQUIRED_ON_IN_PROD：SSOT 是 ssot/feature-flags.md §3
REQUIRED_ON_IN_PROD = [
    "enable_skill_timeout",
    "enable_cron_filelock",
    "enable_memory_save_filelock",
    "enable_feishu_rate_limit_aware",
    "enable_mcp_whitelist",
    "enable_memory_save_filter",
    "enable_webhook_replay_cache",        # v2.1 从 enable_webhook_signature 改名
    "enable_inbound_rate_limit",
    "enable_pgvector_connection_pool",
]


def _assert_production_flags(flags: FeatureFlags) -> None:
    env = os.getenv("XIAOPAW_ENV", "dev")
    if env != "prod":
        return
    for name in REQUIRED_ON_IN_PROD:
        if not getattr(flags, name):
            raise RuntimeError(
                f"prod 禁止关闭 {name}（见 ssot/feature-flags.md）"
            )


def _assert_testapi_constraints(cfg: Config) -> None:
    env = os.getenv("XIAOPAW_ENV", "dev")
    if cfg.debug.enable_test_api:
        if env == "prod":
            raise RuntimeError("prod 环境禁用 TestAPI")
        token = os.getenv("XIAOPAW_TESTAPI_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("XIAOPAW_TESTAPI_TOKEN 必填且长度 ≥32")


def _assert_credentials_strength(cfg: Config) -> None:
    pwd = _extract_password_from_dsn(cfg.memory.db_dsn)
    if pwd and is_weak_credential(pwd)[0]:
        raise RuntimeError("DB 密码为弱密码，拒绝启动")
    if is_weak_credential(cfg.feishu.app_secret)[0]:
        raise RuntimeError("飞书 app_secret 过弱或为 dummy")


def _assert_network_constraints(cfg: Config) -> None:
    env = os.getenv("XIAOPAW_ENV", "dev")
    if env == "prod" and not os.getenv("XIAOPAW_METRICS_TOKEN"):
        raise RuntimeError("prod 环境必须配置 XIAOPAW_METRICS_TOKEN")
    # sandbox.url 必须是容器间地址（不是宿主 localhost）
    if "localhost" in cfg.sandbox.url or "127.0.0.1" in cfg.sandbox.url:
        raise RuntimeError(
            f"sandbox.url 不应指向宿主 loopback: {cfg.sandbox.url}。"
            f"应为 http://aio-sandbox:8080/mcp（Docker DNS）"
        )
```

### 5.2 弱凭证检测（v2.1：命名统一）

v2.1 起**统一命名**：公开入口名为 `is_weak_credential`（定义在 [`07-security.md §6`](07-security.md)），`_is_weak_password` 作为内部别名指向同一实现。原因：v2.0 这两个名字散落在 07 / 09 两份文档里，容易让读者以为是两套不同判断逻辑，实际上是同一个函数。

```python
# xiaopaw/config/safety.py
from xiaopaw.config._weak_credential import is_weak_credential

# 兼容老代码的内部别名（指向同一实现）
def _is_weak_password(raw: str) -> bool:
    is_weak, _ = is_weak_credential(raw)
    return is_weak
```

**正则 + hash 双层检测**保持不变（详见 [`07-security.md §6`](07-security.md)）。

**重要补充**：`is_weak_credential` 只能拦截**常见弱值变体**（dummy / test / 明文字典 / 短长度）；生产环境还需要：
- 密码强度策略（建议 ≥12 字符、至少 4 类字符：大小写字母+数字+符号）
- 定期轮换（§17 定义 90 天周期）
- Secret manager 托管（阿里云 KMS / Vault），不以环境变量形式落盘

---

## 6. 热重载（SIGHUP）

### 6.1 支持热重载的配置

| 配置 | 热重载支持 | 原因 |
|---|---|---|
| `rate_limit.*` | ✅ | RateLimiter 下次 check 时生效 |
| `sender.max_concurrent` | ⚠️ | 需要重建 Semaphore；老请求仍用旧值 |
| `observability.log_level` | ✅ | root logger.setLevel() |
| `observability.trace.sample_rate` | ✅ | 下次采样判断 |
| `feature_flags.enable_*`（多数） | ✅ | 下次执行分支时生效 |
| `feature_flags.enable_mcp_whitelist` | ❌ | SkillLoaderTool `__init__` 时固化，需重启 |
| `feature_flags.enable_cron_filelock` | ❌ | CronService 启动时固化，需重启 |
| `feature_flags.enable_webhook_replay_cache` | ✅ | 下次 event 处理生效（ReplayCache 实例热替换） |
| `feature_flags.enable_pgvector_connection_pool` | ❌ | Indexer 启动时固化，需重启 |
| `feature_flags.token_counter_mode` | ⚠️ | 已加载的 tokenizer 缓存需清 |
| `agent.model` | ❌ | MemoryAwareCrew `__init__` 时固化 |
| `cleanup.*` | ✅ | 下次 daily sweep 生效 |
| 凭证（`.env`） | ❌ | 必须重启 |

### 6.2 热重载实现

```python
# xiaopaw/main.py
import signal

async def main():
    cfg = load_config()
    # ... 启动所有服务 ...

    def _handle_sighup(signum, frame):
        logger.info("SIGHUP received; reloading config")
        try:
            new_cfg = load_config()
            _apply_reloadable_config(new_cfg)
            logger.info("config reloaded successfully")
        except Exception:
            logger.exception("config reload failed; keeping old config")

    loop.add_signal_handler(signal.SIGHUP, _handle_sighup, 0, None)


def _apply_reloadable_config(new_cfg: Config) -> None:
    # 只更新热重载安全的字段
    global rate_limiter
    rate_limiter.update(new_cfg.rate_limit)
    logging.getLogger().setLevel(new_cfg.observability.log_level)
    feature_flags_ctx.set(new_cfg.feature_flags)
    # 不支持热重载的字段：静默忽略并打 WARNING
    if new_cfg.agent.model != cfg.agent.model:
        logger.warning("agent.model 变更需要重启才生效")
```

### 6.3 命令行触发

```bash
# 修改 config.yaml 后
docker compose exec xiaopaw kill -HUP 1
```

---

## 7. 配置变更管理

### 7.1 修改流程

1. **修改 `config.yaml`**（不进 git）
2. **本地启动校验**：`python -m xiaopaw.config.safety --config config.yaml`
3. **dev 环境测试**：跑集成测试 smoke
4. **审核 PR**：`config.yaml.example` 变更必须 PR 审核
5. **生产发布**：蓝绿部署 / SIGHUP 热重载

### 7.2 关键字段变更影响

| 字段 | 变更影响 | 建议方式 |
|---|---|---|
| `feishu.allowed_chats` | 立即生效 | SIGHUP |
| `agent.max_iter` | 下次对话生效 | 重启（MemoryAwareCrew 新实例） |
| `memory.db_dsn` | 需重启 | 蓝绿 |
| `feature_flags.enable_mcp_whitelist` | 需重启 | 蓝绿 |
| `sandbox.url` | 需重启 | 蓝绿 |

### 7.3 Feature Flag 字段改名对照（v1 / v2.0 → v2.1）

配置变更管理中需特别关注历史改名：

| v1 / v2.0 字段 | v2.1 字段 | 说明 |
|---|---|---|
| `observability.health_port` | **删除** | 与 `metrics_port` 同端口，同 aiohttp Application 分路由暴露 |
| `feature_flags.enable_webhook_signature` | `feature_flags.enable_webhook_replay_cache` | SDK 不存在 signature 参数；v2.1 改为 event_id 去重的实际含义 |
| `sandbox.url: http://aio-sandbox:8022/mcp` | `http://aio-sandbox:8080/mcp` | 8022 是 v1 host 映射；容器间 DNS 走内部 8080 |

升级时：
1. 删除 `observability.health_port`（保留亦不报错，但 `FeatureFlags.from_config` 会对 feature_flags 节的未知字段 raise）
2. 重命名 `enable_webhook_signature` → `enable_webhook_replay_cache`（若遗留会被 `from_config` 的 extra=forbid 拦截）
3. `sandbox.url` 端口 8022 → 8080

---

## 8. 不同环境的配置差异

### 8.1 dev

```yaml
debug:
  enable_test_api: true
  test_api_host: "127.0.0.1"           # 严格 loopback（启动校验强制）

feishu:
  allowed_chats: ["oc_dev_chat_id_xxx"]

feature_flags:
  enable_mcp_whitelist: false            # 开发环境探索 sandbox 全部工具
  enable_inbound_rate_limit: false       # 本地测试不限流
  token_counter_mode: "rough"            # 无网环境兼容（F1: qwen_official/hf_qwen/rough）

observability:
  log_level: "DEBUG"
  log_console_human: true
  metrics_port: 8090                     # dev 也统一 8090
```

### 8.2 canary

```yaml
debug:
  enable_test_api: false

feishu:
  allowed_chats: ["oc_canary_chat_id"]

feature_flags:
  # 与 prod 保持完全一致（REQUIRED_ON_IN_PROD 全开）
  enable_mcp_whitelist: true
  enable_memory_save_filter: true
  enable_webhook_replay_cache: true      # v2.1 重命名
  enable_inbound_rate_limit: true
  enable_pgvector_connection_pool: true
  token_counter_mode: "qwen_official"

observability:
  log_level: "INFO"
  log_console_human: false
  log_json_file: true
  metrics_port: 8090
```

### 8.3 prod

```yaml
debug:
  enable_test_api: false       # 启动校验强制检查

feishu:
  allowed_chats: []            # 空列表 = 明示"允许所有群"（语义与 07-security §7.4 对齐）
                                # 若为 None 则启动 warn：建议显式填 [] 表达意图

feature_flags:
  # prod 必开（对齐 ssot/feature-flags.md §3 REQUIRED_ON_IN_PROD）
  enable_skill_timeout: true              # F2
  enable_cron_filelock: true              # F3
  enable_memory_save_filelock: true       # F4
  enable_feishu_rate_limit_aware: true    # F5
  enable_mcp_whitelist: true              # F7
  enable_memory_save_filter: true         # F8
  enable_webhook_replay_cache: true       # F9 (v2.1 重命名)
  enable_inbound_rate_limit: true         # F10
  enable_pgvector_connection_pool: true   # F12

  # prod 允许关（按实际场景）
  # enable_trace_id: true                 # F6（性能有问题可临时关）
  # enable_pgvector_rls: false            # F11（单租户 prod 默认关；多租户 prod 开）
  # token_counter_mode: "qwen_official"   # F1

observability:
  log_level: "INFO"
  log_json_file: true
  metrics_port: 8090
  trace:
    sample_rate: 1.0

# XIAOPAW_ENV=prod 触发 assert_all_production_safe(cfg) 强制校验（见 §5）
```

### 8.4 环境变量 XIAOPAW_ENV

```bash
XIAOPAW_ENV=dev        # 宽松模式
XIAOPAW_ENV=canary     # 严格模式（与 prod 一致）
XIAOPAW_ENV=prod       # 严格模式 + 强制 metrics token
```

---

## 9. v1 → v2 配置迁移

### 9.1 必须做的 diff

| 变更 | v1 | v2.1 |
|---|---|---|
| 凭证 | `config.yaml` 含 dummy 值 | 全部移到 `.env` |
| `encrypt_key` / `verification_token` | optional（v1 实际未被 WS SDK 使用） | **非 HTTP 回调模式下不强制**（WS 由 app_secret 建连；详见 07-security §8.1） |
| `debug.enable_test_api` | 默认 true | 默认 false + prod 强制 |
| `memory.db_dsn` 默认 | `postgresql://xiaopaw:xiaopaw123@...` | 空串或从 env |
| `session.max_active_sessions` | 不存在 | 新增，默认 1000 |
| `sender.max_concurrent` | 不存在 | 新增，默认 5 |
| `rate_limit` | 不存在 | 新增节 |
| `replay_cache` | 不存在 | 新增节 |
| `cleanup` | 不存在 | 新增节 |
| `observability.trace` | 不存在 | 新增节 |
| `observability.health_port` | 存在 | **删除**（与 `metrics_port` 同端口） |
| `observability.metrics_port` | 9091 | **8090**（对齐 `ssot/ports.md`） |
| `sandbox.url` | `http://aio-sandbox:8022/mcp` | `http://aio-sandbox:8080/mcp` |
| `feature_flags` | 不存在 | **全新节**（12 个 flag，见 ssot/feature-flags.md） |
| `feature_flags.enable_webhook_signature` | — | 改名 `enable_webhook_replay_cache` |
| SKILL.md `allowed_tools` | 不存在 | **v2 新增字段** |

### 9.2 迁移脚本片段

```bash
#!/bin/bash
# scripts/migrate_config.sh  —— v2.1

set -euo pipefail

# 1. 备份原 config
cp config.yaml config.yaml.v1.bak

# 2. 生成 .env（避免把凭证经过 shell 变量暴露到进程表；写入临时文件再 mv）
umask 077
TMPENV=$(mktemp)
trap 'rm -f "$TMPENV"' EXIT

# 生成随机 token（openssl 输出直接写入文件，不经过 shell 变量）
{
  echo "XIAOPAW_ENV=prod"
  echo "FEISHU_APP_ID=$(yq e '.feishu.app_id' config.yaml)"
  echo "FEISHU_APP_SECRET=$(yq e '.feishu.app_secret' config.yaml)"
  echo "FEISHU_ENCRYPT_KEY=# WS 模式不使用，仅在 HTTP 回调模式下必填"
  echo "FEISHU_VERIFICATION_TOKEN=# 同上"
  echo "QWEN_API_KEY=$(yq e '.agent.qwen_api_key // \"\"' config.yaml)"
  echo "MEMORY_DB_DSN=请填入强密码版"
  printf 'XIAOPAW_METRICS_TOKEN='; openssl rand -hex 32
  printf 'XIAOPAW_TESTAPI_TOKEN='; openssl rand -hex 32
} > "$TMPENV"
mv "$TMPENV" .env
trap - EXIT
chmod 0400 .env

# 3. 更新 config.yaml（去掉凭证，加 feature_flags，改端口）
yq e '.feishu.app_id = "${FEISHU_APP_ID}" |
      .feishu.app_secret = "${FEISHU_APP_SECRET}" |
      .feishu.encrypt_key = "${FEISHU_ENCRYPT_KEY}" |
      .feishu.verification_token = "${FEISHU_VERIFICATION_TOKEN}" |
      .memory.db_dsn = "${MEMORY_DB_DSN}" |
      .debug.enable_test_api = false |
      .sandbox.url = "http://aio-sandbox:8080/mcp" |
      .observability.metrics_port = 8090 |
      del(.observability.health_port) |
      .feature_flags.enable_skill_timeout = true |
      .feature_flags.enable_cron_filelock = true |
      .feature_flags.enable_memory_save_filelock = true |
      .feature_flags.enable_feishu_rate_limit_aware = true |
      .feature_flags.enable_mcp_whitelist = true |
      .feature_flags.enable_memory_save_filter = true |
      .feature_flags.enable_webhook_replay_cache = true |
      del(.feature_flags.enable_webhook_signature) |
      .feature_flags.enable_inbound_rate_limit = true |
      .feature_flags.enable_pgvector_connection_pool = true
     ' -i config.yaml

# 4. 启动校验（v2.1 单入口）
python -m xiaopaw.config.safety --config config.yaml || exit 1

echo "✅ 配置迁移完成，请检查 .env 中的占位符"
```

### 9.3 SKILL.md 补齐 allowed_tools

```bash
#!/bin/bash
# scripts/augment_skill_manifests.sh

declare -A SKILL_TOOLS=(
  [pdf]="sandbox_file_operations sandbox_execute_code"
  [docx]="sandbox_file_operations sandbox_execute_code"
  [pptx]="sandbox_file_operations sandbox_execute_code"
  [xlsx]="sandbox_file_operations sandbox_execute_code"
  [feishu_ops]="sandbox_execute_code sandbox_file_operations"
  [baidu_search]="sandbox_execute_code"
  [web_browse]="sandbox_convert_to_markdown browser_navigate browser_get_markdown browser_screenshot browser_get_clickable_elements"
  [scheduler_mgr]="sandbox_file_operations sandbox_execute_code"
  [memory-save]="sandbox_file_operations"
  [memory-governance]="sandbox_file_operations"
  [skill-creator]="sandbox_file_operations"
  [search_memory]="sandbox_execute_code"
)

for skill in "${!SKILL_TOOLS[@]}"; do
  skill_file="xiaopaw/skills/$skill/SKILL.md"
  if ! grep -q "^allowed_tools:" "$skill_file"; then
    # 在 frontmatter 结束前插入
    sed -i "/^---$/{ h; s/.*/allowed_tools:/; x; :a; N; s/$/\n/; b; }" "$skill_file"
    for tool in ${SKILL_TOOLS[$skill]}; do
      echo "Appending $tool to $skill"
    done
  fi
done
```

---

## 10. 验收清单

配置正确的标准（CI 可验证）：

- [ ] `config.yaml` 不在 git 中（`.gitignore` 生效）
- [ ] `.env` 不在 git 中，mode 0400
- [ ] `config.yaml.example` 无真实凭证（grep `xiaopaw123` 为空）
- [ ] `config.yaml` 不含 `observability.health_port`（已删除）
- [ ] `config.yaml` 不含 `feature_flags.enable_webhook_signature`（已改名）
- [ ] `config.yaml` 中 `sandbox.url` 端口为 8080（非 8022）
- [ ] `assert_all_production_safe(cfg)` 在 prod 环境通过
- [ ] 所有 SKILL.md 有 `allowed_tools` 字段（或 `enable_mcp_whitelist=false`）
- [ ] `XIAOPAW_METRICS_TOKEN` 在 prod 不为空
- [ ] Pydantic schema 校验通过
- [ ] `FeatureFlags.from_config` 能正确拒绝未知字段（单测 TC-P1-11-a）
- [ ] feature_flags 字段数 == 12（对齐 `ssot/feature-flags.md`）

---

## 下一步阅读

- **配置字段对应的实现** → [02-modules.md](02-modules.md)
- **配置的安全含义** → [07-security.md](07-security.md)
- **部署时的配置管理** → [08-deployment.md](08-deployment.md)
- **配置变更的测试策略** → [10-testing.md](10-testing.md)
- **v1 到 v2 迁移的完整步骤** → [11-migration-v1-to-v2.md](11-migration-v1-to-v2.md)
