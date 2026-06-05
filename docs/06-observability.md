# 06 可观测性设计

> 本文是 [DESIGN.md](../DESIGN.md) §8 的详细展开。
> 读者：SRE、值班工程师、实现工程师（埋点落地）。
> 最后更新：2026-04-19（v2.1）——修正 `to_thread` vs `run_in_executor` 事实、/metrics+/health 统一到 8090、LLM status 扩到 7 值、`agent_latency` 加 `routing_type` label；端口权威见 [ssot/ports.md](ssot/ports.md)。

---

## 目录

1. [可观测性三大支柱](#1-可观测性三大支柱)
2. [trace_id 贯穿设计](#2-trace_id-贯穿设计)
3. [结构化日志](#3-结构化日志)
4. [Prometheus 指标](#4-prometheus-指标)
5. [/metrics HTTP 服务与 /health 端点](#5-metrics-http-服务与-health-端点)
6. [告警规则](#6-告警规则)
7. [监控 Dashboard 设计](#7-监控-dashboard-设计)
8. [Trace 写入磁盘](#8-trace-写入磁盘)
9. [采样策略](#9-采样策略)
10. [日志落盘与轮转](#10-日志落盘与轮转)
11. [异常追踪](#11-异常追踪)
12. [验收标准（G3）](#12-验收标准g3)
13. [运维手册（排障 decision tree）](#13-运维手册排障-decision-tree)

---

## 1. 可观测性三大支柱

可观测性（Observability）≠ 监控（Monitoring）。监控回答"系统出问题了吗"，可观测性回答"为什么出问题、在哪里"。XiaoPaw v2 按业界通用的 **Logs / Metrics / Traces** 三支柱组织：

| 支柱 | 承载信息 | 存储位置 | 主要用途 | v2 相对 v1 |
|---|---|---|---|---|
| **结构化日志**（Logs） | 离散事件、异常栈、上下文字段 | `data/logs/xiaopaw.log`（JSON 行） | 事后排障、审计 | 加入 `trace_id` / `caller` / `stacktrace` / PII mask |
| **指标**（Metrics） | 数值时间序列、聚合统计 | Prometheus（拉取 `/metrics`） | 告警、容量规划、SLO | 从 13 精简到 8；`/metrics` Bearer Token |
| **Trace**（分布式追踪） | 一次请求的跨组件调用链 | `data/traces/{sid}/{ts}_{msg_id}/` + 日志中的 `trace_id` 关联 | 慢请求分析、单次调用复盘 | `ContextVar` 贯穿 ≥85%；executor helper |

**三者如何协同**：

1. **从告警出发**：Grafana 告警 "p95 > 60s" 触发 → 点进 Dashboard 查看流量/LLM 异常 → 用 `trace_id` 跳到对应的 Trace 目录与日志片段。
2. **从用户反馈出发**：用户报 "飞书 msg_id=om_xxx 无回复" → 日志 grep `feishu_msg_id=om_xxx` 取出 `trace_id` → 查看 `data/traces/{sid}/{ts}_om_xxx/main.jsonl` 还原完整执行路径。
3. **从指标异常出发**：`xiaopaw_skill_timeout_total{skill="web_browse"}` 突然上涨 → Dashboard Skills 面板定位到具体 Skill → 日志 grep `skill_name="web_browse"` AND `level=ERROR` 看原因。

**设计原则**：

- **采集在边缘、聚合在中心**：每个模块只负责产出结构化日志 + 指标，Prometheus / 日志平台负责聚合；不在应用内做统计。
- **高基数字段不进 label**：`routing_key` / `session_id` / `trace_id` 不作为 Prometheus label（会爆掉时间序列），只作为日志字段。
- **单一事实来源**：`trace_id` 同时写入日志、trace 文件、出站 HTTP header，所有信号可反查同一次调用。

---

## 2. trace_id 贯穿设计

### 2.1 生成位置

trace_id 在**入站边界**统一生成，入站后只透传不重生成：

| 入口 | 生成方式 | 写入位置 |
|---|---|---|
| `FeishuListener.on_event` | `new_trace_id()` = `uuid.uuid4().hex[:16]` | `InboundMessage.trace_id` |
| `TestAPI POST /api/test/message` | 同上 | `InboundMessage.trace_id`，同时写入响应体 `{"trace_id": "..."}` |
| `CronService` 触发的 fake 消息 | `f"cron-{job_id}-{uuid.uuid4().hex[:8]}"` | `InboundMessage.trace_id`（便于 grep 区分 cron 驱动） |

**为什么 16 字符**：`uuid.hex[:16]` = 64 bit 随机，冲突概率 < 1e-14（足以覆盖日均百万级调用 30 天）；比完整 UUID 节省日志空间 50%，肉眼可读。

### 2.2 ContextVar 贯穿（Python 原生）

```python
# xiaopaw/observability/trace.py
import contextvars
import uuid

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_trace_id(trace_id: str) -> contextvars.Token:
    """在 Runner._handle 入口调用；返回 Token 供 finally reset。"""
    return trace_id_var.set(trace_id)
```

**Runner 使用范式**：

```python
async def _handle(self, inbound: InboundMessage) -> None:
    token = bind_trace_id(inbound.trace_id)
    try:
        # ... 全部业务逻辑 ...
    finally:
        trace_id_var.reset(token)
```

**为什么用 ContextVar 而非参数穿透**：
- asyncio 原生支持 ContextVar 随 Task 拷贝（`asyncio.create_task` 会自动 `copy_context`）
- 避免所有模块（logging、metrics helper、LLM 调用）都加 `trace_id` 参数，侵入性过强
- 日志 Formatter 可直接 `trace_id_var.get()` 获取，无需 record.extra 层层透传

### 2.3 线程池边界：to_thread vs run_in_executor

**事实**（修正 v2.0 草稿的误述）：

| API | copy_context 行为 | Python 版本 |
|---|---|---|
| `asyncio.to_thread(fn, *args)` | **自动** copy_context（内部 `copy_context().run(fn, ...)`） | **3.9+** |
| `loop.run_in_executor(None, fn, *args)` | **不自动** copy_context（API 设计如此，不是 bug） | 所有版本 |

这不是 "Python ≤3.13 的某个坑"——两个 API 设计意图不同：`to_thread` 是 3.9 新加的**便捷包装**，专门解决 context 透传；`run_in_executor` 是**更底层**的原语，把是否传 context 交给调用方决定。

**因此**：

- 项目中 `tiktoken.encode` / `chunk_by_tokens` / `SessionManager._iter_lines_reverse_sync` / pgvector 同步写入都走 `asyncio.to_thread`，**trace_id 自动透传**，不需要 helper
- 只有在**直接调 `loop.run_in_executor`** 或需要**自定义 executor**（`ProcessPoolExecutor` / 专用 `ThreadPoolExecutor`）时，才需要下面的 helper

```python
# xiaopaw/observability/trace.py
import asyncio
import contextvars
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")


async def run_in_executor_with_context(
    fn: Callable[..., T], *args, **kwargs
) -> T:
    """
    在线程池运行同步函数，且保留当前 ContextVar（含 trace_id）。

    关键点：
    - copy_context() 必须在事件循环线程调用（它捕获的是"调用点"的 context）
    - 线程池里执行的是 ctx.run(fn, ...)，它以捕获的 ctx 作为环境运行 fn
    - 若直接在 executor 里 copy_context()，拿到的是 executor 线程的空 context

    使用场景（本项目绝大多数情况用 asyncio.to_thread 即可）：
    - 需要自定义 executor（ProcessPoolExecutor 等，to_thread 不支持）
    - 遗留代码直接调 run_in_executor（逐步迁移到 to_thread）
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = partial(fn, *args, **kwargs)
    return await loop.run_in_executor(None, ctx.run, call)
```

### 2.4 出站 HTTP header 透传

`trace_id` 必须从 XiaoPaw 透传到下游，便于在外部 API 后台（DashScope / 飞书开放平台）按 trace_id 反查：

| 出站客户端 | Header | 位置 |
|---|---|---|
| `AliyunLLM.call` | `X-DashScope-RequestId: {trace_id}` | `xiaopaw/llm/aliyun_llm.py`（`_request` 统一注入） |
| `FeishuSender` | `X-Request-Id: {trace_id}` | `xiaopaw/feishu/sender.py`（所有 aiohttp 请求） |
| `BaiduSearchTool` | `X-Request-Id: {trace_id}` | `xiaopaw/tools/baidu_search_tool.py` |
| `memory/indexer.upsert_memory` | `application_name=xiaopaw:{trace_id}` | asyncpg 连接参数 |

**统一封装**：出站 HTTP 建议统一在 `xiaopaw/utils/http.py` 提供 `make_traced_headers()`，避免散落。

### 2.5 覆盖率目标与测量

**覆盖率定义**：在 `xiaopaw.*` logger 产出的日志条目中，`trace_id` 字段值**不等于** `"-"` 的条目占比。

- **强校验点**（CI 硬性 100% —— 这三处缺一条都算回归）：
  - Runner / `_handle` 入口日志
  - 出站 HTTP header（DashScope / 飞书 / pgvector 连接 application_name）
  - LLM 请求 payload 相关日志
- **整体门槛**：`xiaopaw.*` 全量 ≥85%
- **尽力而为点**：CrewAI 内部 ThreadPoolExecutor、tenacity 重试回调、aiohttp cb 等第三方生态
- **CI gate**：`scripts/verify_trace_coverage.py` 解析 JSON 日志，整体 ≥85% + `--require-loggers` 列表 100%（[ADR-008](01-architecture.md#6-关键架构决策记录adr)）

---

## 3. 结构化日志

### 3.1 日志后端配置

```python
# xiaopaw/observability/logging_config.py（要点摘录）
from logging.handlers import RotatingFileHandler

def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    json_file: bool = True,
    console_human: bool = True,
) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    # 清理已有 handler，幂等
    for h in list(root.handlers):
        root.removeHandler(h)

    if console_human:
        ch = logging.StreamHandler()
        ch.setFormatter(HumanFormatter())  # [ts] [LVL] [logger] msg trace_id=...
        root.addHandler(ch)

    if json_file:
        fh = RotatingFileHandler(
            log_dir / "xiaopaw.log",
            maxBytes=50 * 1024 * 1024,  # 50 MB
            backupCount=10,
            encoding="utf-8",
        )
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
```

### 3.2 JsonFormatter 完整实现

```python
# xiaopaw/observability/logging_config.py
import json
import logging
from datetime import datetime, timezone
from .trace import trace_id_var
from .pii_mask import mask_pii


class JsonFormatter(logging.Formatter):
    """JSON 行日志，一条日志一行，强制字段齐全。"""

    CONTEXT_FIELDS = (
        "routing_key", "session_id", "feishu_msg_id", "event_type",
        "chat_type", "skill_name", "is_sub_crew",
        "http_method", "http_path", "http_status", "latency_ms",
    )

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_pii(record.getMessage()),
            "caller": f"{record.filename}:{record.lineno}:{record.funcName}",
            "trace_id": trace_id_var.get(),
        }
        for field in self.CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["stacktrace"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info

        return json.dumps(payload, ensure_ascii=False, default=str)
```

### 3.3 JSON 日志字段规范表

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ts` | str ISO8601 | ✅ | UTC 毫秒精度 |
| `level` | str | ✅ | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `logger` | str | ✅ | logger 名（通常 `xiaopaw.runner` 这类点分） |
| `msg` | str | ✅ | 已经过 `mask_pii` 处理的消息文本 |
| `caller` | str | ✅ | `filename:lineno:funcName`（v2 新增，便于定位源码） |
| `stacktrace` | str | 仅异常时 | `formatException` 输出的完整调用栈（v2 新增） |
| `trace_id` | str | ✅ | 当前 ContextVar 值；无值时为 `-` |
| `routing_key` | str | 业务上下文 | `p2p:ou_xxx` / `group:oc_yyy` / `thread:oc_yyy:t_zzz` |
| `session_id` | str | 业务上下文 | `s-abc123` |
| `feishu_msg_id` | str | 业务上下文 | 飞书 `om_xxx` |
| `event_type` | str | FeishuListener | `im.message.receive_v1` 等 |
| `chat_type` | str | FeishuListener | `p2p` / `group` |
| `skill_name` | str | Agent/Skill | 触发的 Skill |
| `is_sub_crew` | bool | Agent | 是否 Sub-Crew 调用 |
| `http_method` / `http_path` / `http_status` / `latency_ms` | 混合 | HTTP 边界 | TestAPI / 出站请求 |

**注入方式**：`logger.info("msg", extra={"routing_key": rk, "session_id": sid})`。禁止 `logger.info(f"... {sid}")` 硬拼（无法结构化过滤）。

### 3.4 日志级别规范

| 级别 | 用途 | 示例 |
|---|---|---|
| `DEBUG` | 调试细节，生产默认关闭 | `SessionManager.append` 写入完成、每条 LLM payload |
| `INFO` | 业务正常关键节点 | `Runner.dispatch` 入队、`MemoryAwareCrew.run_and_index` 完成、Skill 启动/完成 |
| `WARNING` | 可恢复异常或降级 | tenacity 重试中、Qwen tokenizer 降级到 cl100k_base、LRU 淘汰 session |
| `ERROR` | 业务失败但进程存活 | Skill 超时、飞书 429 超过最大重试、pgvector upsert 失败 |
| `CRITICAL` | 需立即人工介入 | 启动凭证校验失败、未捕获异常进入 top-level handler |

**不要**在业务路径上 `logger.error` 然后 `raise`（日志+告警双重噪音），只在真正的"终点"（入口异常处理器 / Runner 顶层 catch）记 ERROR。

### 3.5 PII mask 集成

```python
# xiaopaw/observability/pii_mask.py
import re

PII_PATTERNS = [
    # 中国大陆手机号：1[3-9]xxxxxxxx
    (re.compile(r"\b1[3-9]\d{9}\b"),
     lambda m: m.group()[:3] + "****" + m.group()[-4:]),
    # 邮箱
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
     lambda _m: "***@***"),
    # 身份证号（18 位，末位可 X）
    (re.compile(r"\b\d{17}[\dXx]\b"),
     lambda m: m.group()[:6] + "********" + m.group()[-4:]),
]


def mask_pii(text: str) -> str:
    if not text:
        return text
    for pat, repl in PII_PATTERNS:
        text = pat.sub(repl, text)
    return text
```

**mask 规则**：
- **在 Formatter 层 mask**（而非业务层）—— 保证所有日志出站前都经过一次
- **只 mask 消息正文**（`record.getMessage()`），不动字段值（`feishu_msg_id` 等本就不含 PII）
- **Trace 落盘走独立 mask**：`data/traces/` 也经同一函数处理（§8）

**为什么不在入口 mask**：业务逻辑（搜索、memory-save）需要真实值做匹配；日志层 mask 是"落盘前的最后一步"。

**局限性声明**（必须在 compliance-baseline 标注）：
- 不覆盖银行卡号 / 地址 / 姓名（Qwen 输出高度非结构化，正则召回率低于 30%）
- 不覆盖自定义昵称含手机号的特殊情况（如 "张三_13812345678"）
- CI 脚本 `scripts/verify_pii_masking.py` 扫 50 条合成含 PII 日志，召回率应 ≥95%

---

## 4. Prometheus 指标

### 4.1 命名与 label 规范

- **前缀**：所有指标统一 `xiaopaw_` 前缀
- **单位后缀**：`_seconds` / `_bytes` / `_total`（counter 必须以 `_total` 结尾）
- **label 基数**：所有 label 值集合基数 <100；绝不把 `trace_id` / `session_id` / `routing_key` 写成 label
- **label 取值白名单**：`routing_type ∈ {p2p, group, thread}`；`status ∈ {ok, timeout, rate_limited, 4xx, 5xx, cancelled, network_error}`；`model ∈ {qwen3-max, qwen-turbo, text-embedding-v3}`

### 4.2 8 个核心指标（v2 精简）

v1 有 13 个指标，观测面散（HTTP / Session / Runner / 错误……）。v2 按 SRE 关注的"**服务可用性 / 成本 / 延迟 / 外部依赖健康**"四个维度精简到 8 个，其余交给日志检索。

#### 指标 1：`xiaopaw_inbound_total`

```python
xiaopaw_inbound_total = Counter(
    "xiaopaw_inbound_total",
    "Inbound messages entered Runner dispatch (post rate-limit & replay filter).",
    ["source", "routing_type"],
    registry=REGISTRY,
)
```

- **类型**：Counter
- **label**：`source ∈ {feishu, test_api, cron}`，`routing_type ∈ {p2p, group, thread}`
- **埋点**：`Runner.dispatch` 入口（通过速率限制后、入队前）
- **业务含义**：分 routing_type 统计真实流量，用于容量规划

#### 指标 2：`xiaopaw_llm_calls_total`

```python
xiaopaw_llm_calls_total = Counter(
    "xiaopaw_llm_calls_total",
    "LLM API calls, partitioned by model and status.",
    ["model", "status"],
    registry=REGISTRY,
)
```

- **label**：`model ∈ {qwen3-max, qwen-turbo, text-embedding-v3}`；`status ∈ {ok, timeout, rate_limited, 4xx, 5xx, cancelled, network_error}`
- **埋点**：`AliyunLLM._request` finally 分支；status 含义与打点位置：
  - `ok`：2xx 返回
  - `timeout`：`asyncio.TimeoutError`（包括 tenacity 最后一次重试超时）
  - `rate_limited`：HTTP 429 或 DashScope errcode 映射
  - `4xx` / `5xx`：非 429 的 HTTP 错误
  - `cancelled`：`asyncio.CancelledError`（shutdown 或上游取消）
  - `network_error`：`aiohttp.ClientConnectorError` / DNS 失败 / TLS 握手错误等底层连接问题
- **业务含义**：成本估算（调用次数 × 单价）；status != ok 用于告警

#### 指标 3：`xiaopaw_agent_latency_seconds`

```python
xiaopaw_agent_latency_seconds = Histogram(
    "xiaopaw_agent_latency_seconds",
    "End-to-end Runner._handle latency (enqueue → reply sent).",
    ["routing_type"],  # p2p / group / thread（低基数，3 值）
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
    registry=REGISTRY,
)
```

- **label**：`routing_type ∈ {p2p, group, thread}`。DESIGN §13 运维手册要求"回复慢"排障需按 routing_type 切片（群聊的 LLM context 通常更长，延迟基线不同）
- **埋点**：`Runner._handle` try/finally
- **SLO**：p95 <60s（整体；告警 PromQL 对 `le` 聚合时**不带 routing_type**，即汇总全部类型）
- **注意**：3 值 label 只把时间序列数乘 3，未达到 label 基数爆炸的程度；相比"无 label"带来的可排障性提升，收益明显

#### 指标 4：`xiaopaw_llm_latency_seconds`

```python
xiaopaw_llm_latency_seconds = Histogram(
    "xiaopaw_llm_latency_seconds",
    "LLM API call latency per model.",
    ["model"],
    buckets=(0.1, 0.3, 1, 3, 5, 10, 20, 30),
    registry=REGISTRY,
)
```

- **label**：`model`
- **用途**：区分 "LLM 慢 / Skill 慢 / 网络慢"

#### 指标 5：`xiaopaw_external_api_retry_total`

```python
xiaopaw_external_api_retry_total = Counter(
    "xiaopaw_external_api_retry_total",
    "Retries triggered by external API failures.",
    ["api"],
    registry=REGISTRY,
)
```

- **label**：`api ∈ {dashscope, feishu, baidu, pgvector}`
- **埋点**：`tenacity` `before_sleep` 回调
- **用途**：重试次数飙升 → 下游不稳定

#### 指标 6：`xiaopaw_skill_timeout_total`

```python
xiaopaw_skill_timeout_total = Counter(
    "xiaopaw_skill_timeout_total",
    "Skill executions killed by asyncio.wait_for timeout.",
    ["skill"],
    registry=REGISTRY,
)
```

- **label**：`skill`
- **埋点**：`SkillLoaderTool` 捕获 `asyncio.TimeoutError` 时

#### 指标 7：`xiaopaw_feishu_rate_limit_total`

```python
xiaopaw_feishu_rate_limit_total = Counter(
    "xiaopaw_feishu_rate_limit_total",
    "Feishu API rate-limit responses hit (errcode 99991663/99991672 or HTTP 429).",
    registry=REGISTRY,
)
```

- **埋点**：`FeishuSender._request` 识别限流时

#### 指标 8：`xiaopaw_cron_dlq_total`

```python
xiaopaw_cron_dlq_total = Counter(
    "xiaopaw_cron_dlq_total",
    "Cron jobs moved to dead-letter queue (after 3 failed retries).",
    registry=REGISTRY,
)
```

- **埋点**：`CronService._on_task_fail` 入 DLQ 时
- **告警**：必告警（P1）

### 4.3 helper 函数与使用模板

```python
# xiaopaw/observability/metrics_helpers.py
import time
from contextlib import contextmanager
from functools import wraps
from typing import Callable
from . import metrics as M


@contextmanager
def observe_latency(hist, **labels):
    """with observe_latency(M.xiaopaw_agent_latency_seconds): ..."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        (hist.labels(**labels) if labels else hist).observe(
            time.monotonic() - t0
        )


def record_llm_call(model: str, status: str, latency_sec: float) -> None:
    M.xiaopaw_llm_calls_total.labels(model=model, status=status).inc()
    if status == "ok":
        M.xiaopaw_llm_latency_seconds.labels(model=model).observe(latency_sec)


def record_inbound(source: str, routing_key: str) -> None:
    from .trace import routing_key_type
    M.xiaopaw_inbound_total.labels(
        source=source, routing_type=routing_key_type(routing_key)
    ).inc()
```

**典型使用**：

```python
# Runner._handle
async def _handle(self, inbound):
    with observe_latency(M.xiaopaw_agent_latency_seconds):
        record_inbound("feishu", inbound.routing_key)
        ...

# AliyunLLM._request
t0 = time.monotonic()
try:
    resp = await self._http.post(...)
    record_llm_call(self.model, "ok", time.monotonic() - t0)
except asyncio.TimeoutError:
    record_llm_call(self.model, "timeout", time.monotonic() - t0)
    raise
```

---

## 5. /metrics HTTP 服务与 /health 端点

端口：**8090**（引用 [ssot/ports.md](ssot/ports.md)）。`/metrics` 和 `/health` 共用同一个 aiohttp Application，通过**子路由**区分鉴权策略：`/health` 无 Bearer（容器 healthcheck），`/metrics` 走 Bearer middleware。

v2.1 删除原 `observability.health_port` 配置字段；统一配置 `observability.metrics_port: 8090`。

### 5.1 /metrics 鉴权

v1 无鉴权，任意内网/外网机器都能读指标（含飞书 chat_id、错误信息等敏感信息）。v2 强制 Bearer Token：

```python
# xiaopaw/observability/metrics_server.py
import hmac
import os
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from .metrics import REGISTRY


def _constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@web.middleware
async def bearer_middleware(request: web.Request, handler):
    """仅作用于挂在此 middleware 下的子 app（/metrics）。"""
    expected = os.getenv("XIAOPAW_METRICS_TOKEN", "")
    if not expected:
        if os.getenv("XIAOPAW_ENV") == "prod":
            return web.Response(
                status=500, text="XIAOPAW_METRICS_TOKEN missing in prod"
            )
        return web.Response(status=403, text="token not configured")
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not _constant_time_equals(token, expected):
        return web.Response(status=401)
    return await handler(request)


async def _metrics_handler(request: web.Request) -> web.Response:
    body = generate_latest(REGISTRY)
    return web.Response(body=body, content_type=CONTENT_TYPE_LATEST)


def build_app() -> web.Application:
    # 主 app 挂 /health（无鉴权）
    app = web.Application()
    app.router.add_get("/health", _health_handler)

    # 子 app 只挂 /metrics，带 bearer middleware
    metrics_app = web.Application(middlewares=[bearer_middleware])
    metrics_app.router.add_get("/metrics", _metrics_handler)
    app.add_subapp("/", metrics_app)
    return app
```

**关键点**：
- **`hmac.compare_digest`** 防时序攻击
- **prod 强制存在 token**：启动 check 已在 `config/safety.py` 卡住
- **同端口 8090**，不同子 app 区分鉴权；`/health` 不会被 bearer middleware 拦截

### 5.2 /health 端点

```python
# xiaopaw/observability/metrics_server.py
import os
from datetime import datetime, timezone

_STARTED_AT = datetime.now(timezone.utc)


def _git_sha() -> str:
    return os.getenv("XIAOPAW_GIT_SHA", "unknown")  # Dockerfile ARG 注入


async def _health_handler(request: web.Request) -> web.Response:
    uptime_sec = int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds())
    return web.json_response({
        "status": "ok",
        "git_sha": _git_sha(),
        "uptime_sec": uptime_sec,
        "started_at": _STARTED_AT.isoformat(),
    })
```

**/health 规范**：
- **无鉴权**（容器健康检查需要；只暴露 git sha + uptime，无业务数据）
- **同端口 8090**，与 /metrics 共用 aiohttp Application（`observability.health_port` 配置字段已删除）
- **返回 200** 代表"进程在"、但不代表"业务健康"（业务健康由指标 + 告警覆盖）
- **git_sha** 从 Docker 构建 ARG 注入（`docker build --build-arg GIT_SHA=$(git rev-parse HEAD)`）

**Dockerfile**：`EXPOSE 8090` + `HEALTHCHECK CMD curl -f http://localhost:8090/health`。

---

## 6. 告警规则

存放于 `prometheus/rules/xiaopaw.yml`（运维仓库维护，不在本 repo 内）。严重级别约定：

- **P1 critical**：立即页出（on-call 分钟级响应）
- **P2 warning**：工作时间处理（小时级响应）
- **P3 info**：仅记录，周度复盘

```yaml
groups:
- name: xiaopaw.p1
  rules:
  - alert: XiaopawDown
    expr: up{job="xiaopaw"} == 0
    for: 1m
    labels: { severity: critical }
    annotations:
      summary: "XiaoPaw 主进程失联（{{ $labels.instance }}）"

  - alert: XiaopawCronDLQ
    expr: increase(xiaopaw_cron_dlq_total[5m]) > 0
    for: 0m
    labels: { severity: critical }
    annotations:
      summary: "Cron 死信队列出现新条目"
      description: "5 分钟内 {{ $value }} 个定时任务进入 DLQ"

  - alert: XiaopawP95LatencyHigh
    # 聚合时只按 le 分桶，汇总所有 routing_type；细分看 Dashboard
    expr: histogram_quantile(0.95, sum by (le) (rate(xiaopaw_agent_latency_seconds_bucket[5m]))) > 60
    for: 10m
    labels: { severity: critical }

  # 可选：按 routing_type 细分的子告警（日常先观察趋势，门槛调优后再启用）
  # - alert: XiaopawP95LatencyHighByRoutingType
  #   expr: histogram_quantile(0.95, sum by (routing_type, le) (rate(xiaopaw_agent_latency_seconds_bucket[5m]))) > 60
  #   for: 15m
  #   labels: { severity: warning }

- name: xiaopaw.p2
  rules:
  - alert: XiaopawLLMErrorRateHigh
    expr: |
      sum(rate(xiaopaw_llm_calls_total{status!="ok"}[5m]))
      / sum(rate(xiaopaw_llm_calls_total[5m])) > 0.05
    for: 10m
    labels: { severity: warning }

  - alert: XiaopawSkillTimeoutSpike
    expr: rate(xiaopaw_skill_timeout_total[5m]) > 0.1
    for: 5m
    labels: { severity: warning }

  - alert: XiaopawFeishuRateLimited
    expr: increase(xiaopaw_feishu_rate_limit_total[5m]) > 10
    for: 0m
    labels: { severity: warning }

  - alert: XiaopawExternalRetryStorm
    expr: sum by (api) (rate(xiaopaw_external_api_retry_total[5m])) > 1
    for: 10m
    labels: { severity: warning }

- name: xiaopaw.p3
  rules:
  - alert: XiaopawInboundTrafficDrop
    expr: |
      sum(rate(xiaopaw_inbound_total[10m]))
      < 0.2 * sum(rate(xiaopaw_inbound_total[1h] offset 1d))
    for: 30m
    labels: { severity: info }
```

**阈值由来**：
- p95 > 60s：对话类产品用户忍耐极限
- LLM 错误率 > 5%：Qwen 官方 SLA 99% 上的两倍抖动
- Skill 超时 > 0.1/s：单节点场景每分钟 >6 次已明显异常
- 飞书限流 > 10/5min：正常请求不应触发

---

## 7. 监控 Dashboard 设计

Grafana dashboard 建议分 **6 个面板组**，一屏一视图。

| 面板组 | 关键图表 | 对应指标 |
|---|---|---|
| **Overview** | 总 RPM、p50/p95/p99 延迟、错误率、健康状态 | `inbound_total` / `agent_latency_seconds` / `up` |
| **LLM** | 调用量（按 model）、错误率、p95 延迟、估算成本 | `llm_calls_total` / `llm_latency_seconds` |
| **Skills** | 超时次数 Top-N、Skill 调用分布 | `skill_timeout_total` |
| **Memory** | pgvector upsert QPS、召回命中率（需额外埋点）、重试次数 | `external_api_retry_total{api="pgvector"}` |
| **飞书** | 入站 RPS（分 routing_type）、限流次数、Sender 并发 | `inbound_total` / `feishu_rate_limit_total` |
| **存储** | 磁盘占用（sessions / traces / logs）、LRU 淘汰速率、Cron DLQ 数 | `cron_dlq_total` + node_exporter |

**面板层级**：
1. **Overview** 放最上方（80% 场景只看这里）
2. **LLM** / **飞书** 并排第二行
3. **Skills** / **Memory** 第三行
4. **存储** 单独一页（周度巡检）

**Dashboard 命名**：`xiaopaw-overview` / `xiaopaw-llm` …。所有 panel 的 `drilldown_link` 带 `trace_id` 模板变量，点击直接跳日志平台过滤。

---

## 8. Trace 写入磁盘

### 8.1 目录结构（参见 [03-data.md §6](03-data.md)）

```
data/traces/
└── {sid}/
    └── {ts}_{msg_id}/
        ├── meta.json
        ├── main.jsonl
        └── skills/
            ├── web_browse_1.jsonl
            └── web_browse_2.jsonl
```

### 8.2 meta.json 示例

```json
{
  "trace_id": "a1b2c3d4e5f60718",
  "session_id": "s-abc123",
  "routing_key": "p2p:ou_xxx",
  "feishu_msg_id": "om_xxx",
  "source": "feishu",
  "started_at": "2026-04-19T14:23:01.234Z",
  "ended_at": "2026-04-19T14:23:18.901Z",
  "latency_ms": 17667,
  "status": "ok",
  "user_message_preview": "帮我查一下今天的...",
  "assistant_reply_preview": "已为你...",
  "skills_invoked": ["web_browse", "memory-save"],
  "llm_calls": 5,
  "error": null,
  "git_sha": "e3a7b12"
}
```

- `*_preview` 为头 120 字符 + `mask_pii` 处理
- `status ∈ {ok, timeout, error, cancelled}`
- `git_sha` 标记产出此 trace 的代码版本

### 8.3 事件类型

| `type` | 说明 | 关键字段 |
|---|---|---|
| `llm_request` | 发 LLM 前 | `model` / `messages[]` 摘要 / `tools[]` |
| `llm_response` | 收 LLM 后 | `content` / `tool_calls[]` / `usage` / `latency_ms` |
| `tool_call` | 调用 SkillLoader | `skill_name` / `inputs` |
| `tool_result` | SkillLoader 返回 | `errcode` / `result` 摘要 |
| `exception` | 捕获异常 | `error_type` / `msg` / `stacktrace` |

### 8.4 写入策略

- **异步写**：`asyncio.to_thread(write_line)` 避免阻塞事件循环
- **失败降级**：写盘失败记 `logger.warning` 但不中断业务
- **PII mask**：与日志共享 `mask_pii` 函数

---

## 9. 采样策略

**默认**：100% 采样（XiaoPaw 单节点单租户，日均对话量 <10K，落盘成本可控）。

可配置采样：

| 策略 | 适用场景 | 实现 |
|---|---|---|
| **全采样** | 默认 / 故障调查窗口 | `trace_sample_rate: 1.0` |
| **按请求路径采样** | 压测 / slash 命令免 trace | slash 命令 `/status` 这类 `sample_rate = 0` |
| **按 trace_id hash 采样** | 规模上来后（>100K/d） | `int(trace_id[:8], 16) % 10000 < rate_bps` |

配置示例：

```yaml
observability:
  trace:
    enabled: true
    sample_rate: 1.0
    always_sample_on_error: true
    slash_commands_sample: 0.0
```

**错误必采**：任何 `status != ok` 的 trace 一律落盘。

---

## 10. 日志落盘与轮转

### 10.1 轮转策略

采用 **size-based 为主 + 每日归档** 双层：

| 类别 | 策略 | 参数 |
|---|---|---|
| **在线日志** `data/logs/xiaopaw.log` | RotatingFileHandler，size-based | `maxBytes=50MB`，`backupCount=10`（≈500MB） |
| **按天归档** | cron 03:00 `mv *.log.1..10 /archive/YYYY-MM-DD/` + gzip | 留 180 天 |
| **冷存储** | 180 天后转对象存储 | 留 1 年 |

### 10.2 留存分级

| 数据 | 在线 | 冷存储 |
|---|---|---|
| session JSONL | 180 天 | 1 年 |
| ctx.json | 随 session 删 | - |
| raw.jsonl | 30 天 | - |
| traces/ | 30 天 | - |
| xiaopaw.log | 500MB 滚动 | 按天归档 180 天 |
| Cron DLQ | 永久 | - |

---

## 11. 异常追踪

### 11.1 Uncaught exception handler

所有 Task 默认异常会被 asyncio 吃掉。v2 强制全局捕获：

```python
# xiaopaw/main.py
import asyncio
import logging
import sys

log = logging.getLogger(__name__)


def _handle_asyncio_exception(loop, context):
    exc = context.get("exception")
    msg = context.get("message", "unhandled asyncio error")
    if exc:
        log.critical(
            "unhandled_asyncio_exception: %s", msg,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        log.critical("unhandled_asyncio_error: %s | context=%s", msg, context)


def _sys_excepthook(exctype, value, tb):
    log.critical("unhandled_sync_exception", exc_info=(exctype, value, tb))


def install_exception_hooks(loop):
    loop.set_exception_handler(_handle_asyncio_exception)
    sys.excepthook = _sys_excepthook
```

### 11.2 Sentry 集成方案（可选）

```python
# xiaopaw/observability/sentry_init.py（可选）
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import LoggingIntegration


def init_sentry(dsn: str, env: str, release: str) -> None:
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=release,
        integrations=[
            AsyncioIntegration(),
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        traces_sample_rate=0.1,
        send_default_pii=False,
        before_send=lambda event, hint: _scrub(event),
    )
```

- **feature flag 控制**：`observability.sentry.enabled: false`（默认）
- **release**：用 git sha 标记

---

## 12. 验收标准（G3）

| 项 | 目标 | 验证方式 |
|---|---|---|
| trace_id 覆盖率 | 整体 ≥85%；`xiaopaw.runner` / `xiaopaw.llm.aliyun_llm` / `xiaopaw.feishu.sender` 各 100% | `scripts/verify_trace_coverage.py` |
| 8 核心指标齐全 | 都能从 `:8090/metrics` scrape 到 | `scripts/verify_metrics.py` |
| LLM status 枚举 | `status` label 覆盖 {ok, timeout, rate_limited, 4xx, 5xx, cancelled, network_error} 共 7 值 | 单元测试 `test_llm_status_enum.py` |
| PII mask 召回率 | ≥95%（合成样本集） | `scripts/verify_pii_masking.py` |
| /metrics Bearer | prod 无 token 启动失败；无/错 token 返回 401 | 集成测试 |
| /health 返回 git_sha | Dockerfile ARG 注入；同端口 8090 可访问且无鉴权 | smoke test |
| Cron DLQ 告警联动 | DLQ 写入 → 告警 | 集成测试 |

### 12.1 CI 脚本结构

**`scripts/verify_trace_coverage.py`**：

```python
"""Verify trace_id coverage from stdout JSON logs."""
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", type=Path)
    ap.add_argument("--min-ratio", type=float, default=0.85)
    ap.add_argument(
        "--require-loggers", nargs="*",
        default=["xiaopaw.runner", "xiaopaw.llm.aliyun_llm", "xiaopaw.feishu.sender"],
    )
    args = ap.parse_args()

    total, with_trace = 0, 0
    per_logger = {}
    with args.logfile.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            has = rec.get("trace_id", "-") != "-"
            with_trace += int(has)
            lg = rec.get("logger", "")
            if lg in args.require_loggers:
                d = per_logger.setdefault(lg, [0, 0])
                d[0] += 1
                d[1] += int(has)

    ratio = with_trace / total if total else 0.0
    print(f"trace_id ratio: {ratio:.2%} ({with_trace}/{total})")

    fail = ratio < args.min_ratio
    for lg, (t, w) in per_logger.items():
        r = w / t if t else 1.0
        print(f"  [{lg}] {r:.2%} ({w}/{t}) required=100%")
        if r < 1.0:
            fail = True
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
```

**CI gate**：

```yaml
- name: trace coverage
  run: |
    python -m pytest tests/integration/test_trace_smoke.py --log-file=/tmp/logs.jsonl
    python scripts/verify_trace_coverage.py /tmp/logs.jsonl --min-ratio 0.85

- name: metrics coverage
  run: python scripts/verify_metrics.py

- name: pii mask
  run: python scripts/verify_pii_masking.py tests/fixtures/pii_samples.jsonl
```

---

## 13. 运维手册（排障 decision tree）

### 13.1 何时看哪个指标

| 告警名 / 用户反馈 | 第一眼看 | 第二眼看 | 定位方式 |
|---|---|---|---|
| "机器人无响应" | `up{job=xiaopaw}` | `xiaopaw_inbound_total` 有无增长 | 看 `/health` → WebSocket 日志 |
| "回复慢" | `xiaopaw_agent_latency_seconds` p95 | `xiaopaw_llm_latency_seconds` + `skill_timeout_total` | 取慢 trace_id → 翻 `data/traces/` |
| "飞书限流" | `xiaopaw_feishu_rate_limit_total` | `FeishuSender._sem` 占用 | 是否外部账号共享配额 |
| "Cron 任务没触发" | `xiaopaw_cron_dlq_total` | 日志 `logger=xiaopaw.cron` | 看 `data/cron/tasks.dlq.jsonl` |
| "pgvector 错误" | `xiaopaw_external_api_retry_total{api=pgvector}` | DB 侧监控 | asyncpg 日志 + 连接池 |
| "LLM 报错率上升" | `xiaopaw_llm_calls_total{status!="ok"}` rate | 区分 model / status | 日志 grep `logger=xiaopaw.llm.aliyun_llm` AND ERROR |

### 13.2 排障 decision tree

```
告警：XiaopawP95LatencyHigh
 ├─ Q1：LLM latency 也高？
 │   ├─ 是 → LLM 侧问题
 │   │      ├─ status=timeout 多 → DashScope 侧抖动
 │   │      └─ status=ok 但慢   → context 爆了（看 prune/compress 日志）
 │   └─ 否 → 继续 Q2
 ├─ Q2：Skill timeout 多？
 │   ├─ 是 → 查 {skill}，看 sandbox 健康 / 脚本死循环
 │   └─ 否 → 继续 Q3
 └─ Q3：队列积压？
     ├─ 日志 grep "queue size=" > 10 → Runner worker 数不足
     └─ 否 → 看 trace 最慢一条，翻 main.jsonl

告警：XiaopawDown
 ├─ Q1：/health 能访问？
 │   ├─ 否 → 进程挂了，看 systemd / docker logs 最后 100 行
 │   └─ 是 → Prometheus 到 XiaoPaw 的网络问题
 └─ 确认进程存活后：
     ├─ 查 CRITICAL 日志 grep unhandled_asyncio_exception
     └─ 查飞书 WebSocket 连接日志

告警：XiaopawCronDLQ
 └─ 1. 立刻看 data/cron/tasks.dlq.jsonl 最后一条
    2. 判断是任务本身错还是系统错
    3. 修复根因后人工 replay
```

### 13.3 常见排障命令

```bash
# 按 trace_id 翻日志
jq 'select(.trace_id == "a1b2c3d4e5f60718")' data/logs/xiaopaw.log

# 按飞书 msg_id 找 trace
jq 'select(.feishu_msg_id == "om_xxx")' data/logs/xiaopaw.log | jq -r '.trace_id' | head -1

# 看最慢的 10 个 trace
find data/traces -name meta.json -mmin -60 | \
  xargs -I{} jq '. | [.latency_ms, .feishu_msg_id, .session_id] | @tsv' {} | \
  sort -rn | head -10

# 查 Skill 调用分布（近 1h）
find data/traces -name meta.json -mmin -60 | \
  xargs jq -r '.skills_invoked[]' | sort | uniq -c | sort -rn
```

---

## 下一步阅读

- 模块级实现细节 → [02-modules.md §8.3](02-modules.md)
- 信任边界（PII、鉴权为何必须） → [01-architecture.md §4](01-architecture.md)
- 威胁模型（为什么 /metrics 要鉴权） → [07-security.md](07-security.md)
- 部署时 `/health` / metrics 端口开放 → [08-deployment.md](08-deployment.md)
- feature flag 控制观测开关 → [09-config.md](09-config.md)
- 可观测性相关测试用例 → [10-testing.md](10-testing.md)
