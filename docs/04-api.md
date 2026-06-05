# 04 接口设计

> 本文是 [DESIGN.md](../DESIGN.md) §6 的详细展开。
> 读者：集成方 / 运维 / 实现工程师。
> 最后更新：2026-04-19（v2.1）
>
> 接口按方向分三类：**入站**（飞书 Webhook / TestAPI）/ **出站**（飞书 REST / Qwen / 百度 / pgvector / Sandbox MCP）/ **运维**（/metrics / /health）。
> 每个接口按：**定位 / Schema / 鉴权 / 错误语义 / v2 加固点** 描述。
>
> **v2.1 权威引用**：端口见 [ssot/ports.md](ssot/ports.md)；Feature Flags 见 [ssot/feature-flags.md](ssot/feature-flags.md)；威胁清单见 [ssot/threats.md](ssot/threats.md)；锁清单见 [ssot/locks.md](ssot/locks.md)。

---

## 目录

1. [接口总览](#1-接口总览)
2. [飞书 Webhook（入站）](#2-飞书-webhook入站)
3. [TestAPI（入站）](#3-testapi入站)
4. [飞书 REST API（出站）](#4-飞书-rest-api出站)
5. [Qwen DashScope API（出站）](#5-qwen-dashscope-api出站)
6. [百度千帆 API（出站）](#6-百度千帆-api出站)
7. [pgvector PG 协议（出站）](#7-pgvector-pg-协议出站)
8. [AIO-Sandbox MCP（出站）](#8-aio-sandbox-mcp出站)
9. [/metrics 与 /health（运维）](#9-metrics-与-health运维)
10. [内部协议](#10-内部协议)

---

## 1. 接口总览

| 接口 | 方向 | 协议 | 鉴权 | 访问性 | v2.1 加固点 |
|---|---|---|---|---|---|
| 飞书 `im.message.receive_v1` | 入 | WebSocket | SDK 建连时 app_secret 校验 + 应用层 ReplayCache | 所有环境 | 应用层 event_id 去重 + rate-limit |
| 飞书 `im.chat.member.bot.added_v1` | 入 | WebSocket | 同上 | 所有环境 | 同上 |
| TestAPI `POST /api/test/message` | 入 | HTTPS (aiohttp) | Bearer Token + 127.0.0.1 bind（见 [ssot/ports.md](ssot/ports.md) 9090） | 仅 dev | prod 强制禁用 |
| TestAPI `POST /api/test/clear` | 入 | 同上 | 同上 | 仅 dev | 同上 |
| 飞书 REST（发消息/读文档/表格/日历） | 出 | HTTPS | tenant_access_token | 所有环境 | 429 识别 + Semaphore(5)（见 [ssot/locks.md#L4](ssot/locks.md)） |
| Qwen DashScope（chat / embedding） | 出 | HTTPS | Bearer + API Key | 所有环境 | tenacity 重试 + token 计数 |
| 百度千帆 `web_search` | 出 | HTTPS | API Key | 可选 | tenacity 重试 |
| pgvector | 出 | PostgreSQL wire | user/password + TLS | 所有环境 | `psycopg2.pool.ThreadedConnectionPool`（v2.1）+ 独立 DB user |
| AIO-Sandbox MCP | 出 | HTTP (SSE) | 无（内网，见 [ssot/ports.md](ssot/ports.md) 8080） | 所有环境 | **不对宿主暴露端口**（T9） |
| `/metrics` | 入 | HTTPS | Bearer Token + constant_time（见 [ssot/ports.md](ssot/ports.md) 8090） | 所有环境 | prod 强制 token |
| `/health` | 入 | HTTPS | 无（同 8090） | 所有环境 | 仅返回 200 + git sha |

**设计原则**：
- **入站少、出站多**：XiaoPaw 主要是"消息处理器"，暴露面尽量小
- **所有出站必须可重试**：通过 `xiaopaw/utils/retry.py` 统一封装
- **所有入站必须可限流**：[07-security.md §6](07-security.md) RateLimiter 覆盖

---

## 2. 飞书 Webhook（入站）

### 2.1 接入方式

XiaoPaw 作为**长连接 Client**（非公网 Webhook Server）接入飞书开放平台。底层用 `lark-oapi` 的 `ws.Client` 维护 WebSocket 连接，由飞书主动推送事件。

**优势**：
- 无需公网 IP / 反向代理
- 无需 TLS 证书管理
- 适合内网 / 本地部署

**劣势**：
- 单个 WebSocket 连接中断会丢事件（但 lark-oapi 自动重连，5 分钟内补发）
- 不能水平扩容（单连接 = 单 Client 实例，多节点需要额外 session 分发）

### 2.2 订阅的事件类型

| 事件 | 用途 | 处理位置 |
|---|---|---|
| `im.message.receive_v1` | 接收用户消息 | `FeishuListener._on_message` |
| `im.chat.member.bot.added_v1` | Bot 被拉入群 | `FeishuListener._on_bot_added`（欢迎语） |

### 2.3 EventMessage Schema

完整字段见 [03-data.md §2](03-data.md)。关键字段：

```python
# 伪 Schema（lark-oapi 自动解析）
class EventMessage:
    message_id:   str   # om_xxx
    chat_id:      str   # oc_xxx
    thread_id:    str   # ot_xxx（话题消息）
    chat_type:    str   # p2p / group
    message_type: str   # text / image / file / post / audio
    content:      str   # JSON 字符串
    sender:       Sender
    create_time:  int   # ms
```

### 2.4 建连鉴权与应用层重放防护（v2.1）

**SDK 真相**（见 [sdk-verification-report.md §1](sdk-verification-report.md)）：`lark-oapi.ws.Client` 真实签名**只接受** `app_id / app_secret / log_level / event_handler / domain / auto_reconnect`，**不支持** `encrypt_key` / `verification_token` 参数。WebSocket 模式下，飞书服务端在握手阶段使用 `app_secret` 签发 token 并校验；应用侧没有 HMAC 介入点，也无法实现"SDK 侧验签"。

因此 v2.1 把 T3 防御聚焦到**应用层 ReplayCache**（event_id LRU + TTL），与 SDK 真实能力对齐。

```python
# xiaopaw/feishu/listener.py
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

def _build_client(cfg) -> lark.ws.Client:
    """
    v2.1：WebSocket 客户端仅需 app_id + app_secret；SDK 在握手阶段完成服务端鉴权。
    应用层 ReplayCache 在 FeishuListener._on_message 入口执行 event_id 去重。
    """
    return lark.ws.Client(
        app_id=cfg.feishu.app_id,
        app_secret=cfg.feishu.app_secret,
        event_handler=_build_event_handler(),
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
```

**startup 校验**（`config/safety.py`，v2.1）：
- `app_id` / `app_secret` 非空
- `is_weak_credential(app_secret)` 为 False（`config.safety` 弱密码拒绝）
- Feature Flag F9 `enable_webhook_replay_cache` 在 prod 强制为 true（见 [ssot/feature-flags.md#F9](ssot/feature-flags.md)）
- **不再有** `encrypt_key` / `verification_token` 校验（字段已从配置 schema 移除）

> 如果未来确实需要 HMAC 验签，只能改走 HTTP 回调路径（FastAPI endpoint + `EventDispatcherHandler.builder().encrypt_key(...)`），与 WS 模式二选一。

### 2.5 重放防护（v2.1 应用层）

```python
# xiaopaw/feishu/listener.py
class FeishuListener:
    def __init__(self, ..., replay_cache: ReplayCache):   # v2.1：必填
        self._replay_cache = replay_cache

    async def _on_message(self, event: P2ImMessageReceiveV1):
        event_id = event.header.event_id
        if await self._replay_cache.seen(event_id):
            logger.debug("replay_hit event_id=%s skipped", event_id)
            return
        # ... 处理消息 ...
```

**语义与约束**（见 [ssot/threats.md#T3](ssot/threats.md) 与 [concurrency-verification-report.md §6](concurrency-verification-report.md)）：
- 飞书 5 分钟内重发的相同 `event_id` 会被丢弃
- 进程级缓存，容器/systemd 重启必然丢失（monotonic 重置，但缓存本身为空，语义正常）
- 跨重启/多节点部署 → 改用 Redis `SET event_id 1 EX 300 NX`
- Feature Flag：[ssot/feature-flags.md#F9](ssot/feature-flags.md) `enable_webhook_replay_cache`

### 2.6 速率限制（v2 新增）

```python
# xiaopaw/feishu/listener.py
async def _on_message(self, event):
    sender_id = event.event.sender.sender_id.open_id
    if not self._rate_limiter.allow(sender_id):
        XIAOPAW_RATE_LIMITED_TOTAL.inc()
        return  # 静默丢弃
    # ... 继续处理 ...
```

默认 **每用户 20 条/分钟**；超限静默丢弃 + metric。

### 2.7 错误处理

- **WebSocket 断开**：lark-oapi 自动重连，指数退避；5 分钟未重连触发 `XiaopawDown` 告警
- **事件解析失败**：记 `WARNING` 日志，丢弃该事件，不影响后续
- **下游 Runner 异常**：`_on_message` 内 try/except 兜底，避免异常冒泡导致 Client 断开

### 2.8 v2 vs v1 差异

- **v1**：`encrypt_key` / `verification_token` 作为 `ws.Client` 参数（**无效参数，SDK 实际不支持**）；无重放防护；无速率限制
- **v2.1**：接入侧只传 `app_id + app_secret`（与 SDK 真实签名对齐）；应用层 ReplayCache 必填（见 [ssot/threats.md#T3](ssot/threats.md)）；RateLimiter 必填（见 [ssot/threats.md#T7](ssot/threats.md)）

---

## 3. TestAPI（入站）

### 3.1 定位

本地 HTTP 接口，模拟飞书事件进入 Runner，**仅用于开发与自动化测试**。生产环境必须禁用。

### 3.2 端点清单

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/api/test/message` | 模拟用户消息，同步返回 bot 回复 |
| `POST` | `/api/test/clear` | 清空所有 session（危险操作） |

### 3.3 `/api/test/message` Schema

**Request**：

```json
{
  "routing_key": "p2p:ou_dev_user",
  "text": "你好",
  "attachment": {
    "msg_type": "file",
    "file_key": "file_xxx",
    "file_name": "report.pdf",
    "file_path": "/tmp/upload.pdf"
  }
}
```

- `routing_key`：必填，格式遵循 [03-data.md §2.2](03-data.md)
- `text`：必填，用户消息文本
- `attachment`：可选，指定本地文件路径或飞书 file_key

**Response**（HTTP 200）：

```json
{
  "reply": "你好，我是小爪子...",
  "trace_id": "abc123def456...",
  "session_id": "s-xxx",
  "duration_ms": 3542
}
```

**错误响应**：

| HTTP | 场景 |
|---|---|
| 400 | Schema 校验失败（routing_key 格式错等） |
| 401 | Bearer Token 缺失或错误 |
| 500 | 内部异常（Runner 处理失败） |

### 3.4 `/api/test/clear` Schema

**Request**：`POST` 无 body

**Response**（HTTP 200）：`{"cleared": true}`

**后果**：删除 `data/sessions/*.jsonl` + `index.json` + `data/ctx/*`。**不清** pgvector。

### 3.5 鉴权与 bind（v2.1 硬化）

**端口约定**：TestAPI 监听 `9090`（见 [ssot/ports.md](ssot/ports.md)），docker compose dev 必须显式声明 `"127.0.0.1:9090:9090"` 防止误绑 `0.0.0.0`。

**启动校验**（`config/safety.py`）：

```python
def assert_production_safe(cfg: dict) -> None:
    if os.getenv("XIAOPAW_ENV") == "prod":
        if cfg["debug"]["enable_test_api"]:
            raise RuntimeError("prod 环境禁用 TestAPI")
    if cfg["debug"]["enable_test_api"]:
        host = cfg["debug"]["test_api_host"]
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"TestAPI 只能 bind loopback，当前: {host}")
        token = os.getenv("XIAOPAW_TESTAPI_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("XIAOPAW_TESTAPI_TOKEN 必填且长度 ≥32")
```

**运行时校验**：

```python
# xiaopaw/api/test_server.py
async def _message_handler(request: web.Request) -> web.Response:
    expected = os.getenv("XIAOPAW_TESTAPI_TOKEN", "")
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), expected.encode()):
        return web.Response(status=401)
    # ... 处理逻辑 ...
```

### 3.6 CaptureSender 同步响应

TestAPI 需要把 Agent 回复同步返回给 HTTP 调用方，不走飞书。`Runner` 在 TestAPI 入口注入 `CaptureSender`（基于 `asyncio.Future` 的实现），`dispatch` 完成后从 Future 取回结果。

```python
# xiaopaw/api/capture_sender.py
class CaptureSender(SenderProtocol):
    def __init__(self):
        self._future: asyncio.Future[str] = asyncio.Future()

    async def send(self, routing_key: str, content: str) -> str:
        if not self._future.done():
            self._future.set_result(content)
        return "test-msg-id"

    async def send_thinking(self, ...) -> str | None:
        return None  # 不 block

    async def update_card(self, ...) -> None:
        pass

    async def wait(self, timeout: float = 60) -> str:
        return await asyncio.wait_for(self._future, timeout)
```

---

## 4. 飞书 REST API（出站）

### 4.1 使用场景

| 场景 | 接口 | Skill |
|---|---|---|
| 发送消息 | `im.v1.message.create` | FeishuSender 直发 |
| 更新卡片 | `im.v1.message.patch` | FeishuSender.update_card |
| 读云文档 | `docx.v1.document.raw_content` | feishu_ops.read_doc |
| 读电子表格 | `sheets.v2.spreadsheets.get` | feishu_ops.read_sheet |
| 查群成员 | `im.v1.chat.members.get` | feishu_ops.get_chat_members |
| 创建日历事件 | `calendar.v4.calendar.event.create` | feishu_ops.create_event |
| 查日历事件 | `calendar.v4.calendar.event.list` | feishu_ops.list_events |
| 发文件消息 | `im.v1.file.create` + `im.v1.message.create` | feishu_ops.send_file |

### 4.2 SDK 客户端

项目使用 `lark-oapi` 官方 SDK 统一调用：

```python
import lark_oapi as lark

client = lark.Client.builder() \
    .app_id(cfg.feishu.app_id) \
    .app_secret(cfg.feishu.app_secret) \
    .enable_set_token(True) \
    .build()
```

`tenant_access_token` 由 SDK 自动管理（定时刷新）。

### 4.3 FeishuSender 实现要点

**interactive 卡片 + Markdown 格式**（保留 v1 行为）：

```python
# xiaopaw/feishu/sender.py
async def send(self, routing_key: str, content: str) -> str:
    async with self._sem:  # Semaphore(5)
        card_body = {
            "schema": "2.0",
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content}
                ]
            }
        }
        for attempt in range(self.max_retries):
            try:
                req = CreateMessageRequest.builder().body(...).build()
                resp = await self._client.im.v1.message.acreate(req)
                if not resp.success():
                    # v2: 识别真实 429 错误码
                    if resp.code in FEISHU_RATE_LIMIT_CODES:
                        XIAOPAW_FEISHU_RATE_LIMIT_TOTAL.inc()
                        await asyncio.sleep(min(2**attempt, 60))
                        continue
                    raise RuntimeError(f"{resp.code}: {resp.msg}")
                return resp.data.message_id
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(self.retry_backoff[attempt])
```

### 4.4 429 错误码识别（v2.1 修正）

```python
# v1 错：把权限不足码当 rate limit
# FEISHU_RATE_LIMIT_CODES = {99991400}  # ❌ 这是权限码

# v2.1：经验常量集合
#   注意：lark-oapi SDK 源码 grep 对 99991663/72/71 零命中；
#   飞书官方 server-api-error-codes 页面为 SPA 前端 render，WebFetch 无法证实。
#   实现时 HTTP 429 为主（标准）、SDK code 作兜底；新码值待官方文档确认后再扩。
FEISHU_RATE_LIMIT_CODES = {99991663, 99991672, 99991671}
FEISHU_HTTP_RATE_LIMIT_STATUS = {429}
```

HTTP 层 429 处理需从 raw response 读 `Retry-After`：

```python
# v2.1：lark-oapi BaseResponse 的 HTTP 层属性叫 .raw（不是 .raw_response）
# 参考 sdk-verification-report.md §6：
#   class BaseResponse:
#       raw: Optional[RawResponse] = None   # 正确
#       code: Optional[int] = None
#       msg: Optional[str] = None
raw = getattr(resp, "raw", None)
if raw and raw.status_code in FEISHU_HTTP_RATE_LIMIT_STATUS:
    retry_after = raw.headers.get("Retry-After", "")
    delay = int(retry_after) if retry_after.isdigit() else min(2**attempt, 60)
    await asyncio.sleep(delay)
```

### 4.5 卡片 UI 三步

v2 保留 v1 的 Loading UI 模式：

1. `send_thinking(routing_key, "⏳ 思考中...")` → 返回 `card_msg_id`
2. Agent 处理中
3. `update_card(card_msg_id, final_reply)` → 用最终回复替换卡片

失败路径：若 `send_thinking` 失败返回 `None`，`update_card` 跳过，改用 `send` 发新消息（不阻断主流程）。

---

## 5. Qwen DashScope API（出站）

### 5.1 使用场景

| 场景 | 模型 | 调用位置 |
|---|---|---|
| 主 Agent 对话 | `qwen3-max` | `MemoryAwareCrew.run_and_index` |
| Sub-Crew | `qwen3-max` | `build_skill_crew` |
| 压缩摘要（L19） | `qwen3-turbo` | `context_mgmt._summarize_chunk` |
| 记忆摘要（L21） | `qwen3-max` | `indexer.extract_summary_and_tags` |
| Embedding | `text-embedding-v3` dim=1024 | `indexer.embed_texts`, `search_memory.embed_query` |

### 5.2 适配层：AliyunLLM

CrewAI 的 `BaseLLM` 子类实现：

```python
# xiaopaw/llm/aliyun_llm.py
from crewai.llms.base_llm import BaseLLM

class AliyunLLM(BaseLLM):
    def __init__(self, model: str, api_key: str, base_url: str, **kwargs):
        super().__init__(model=model, temperature=kwargs.get("temperature"))
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        # ...

    async def call(self, messages: list[dict], tools: list | None = None,
                   **kwargs) -> str | list:
        # 1. 注入 trace_id 到 header
        headers = {"X-DashScope-RequestId": trace_id_var.get()}
        # 2. tenacity 重试
        @external_api_retry((APITimeoutError, APIConnectionError, RateLimitError))
        async def _do():
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                extra_headers=headers,
            )
            return resp
        resp = await _do()
        # 3. 返回给 CrewAI
        msg = resp.choices[0].message
        if msg.tool_calls:
            return msg.tool_calls  # list → CrewAI 判 isinstance
        return msg.content or ""
```

### 5.3 连接端点

```
base_url = https://dashscope.aliyuncs.com/compatible-mode/v1
```

OpenAI 兼容格式；`model` 字段传 `qwen3-max` 等。

### 5.4 超时 & 重试

- **单次请求超时**：默认 120s（可配 `config.yaml.agent.llm_timeout_s`）
- **重试**：tenacity 3 次，指数退避（1/2/4s）
- **重试触发条件**：`APITimeoutError` / `APIConnectionError` / `RateLimitError` / HTTP 5xx

### 5.5 错误语义

| 错误 | 处理 |
|---|---|
| `RateLimitError`（429） | tenacity 重试 3 次；仍失败记 ERROR + `xiaopaw_llm_calls_total{status="rate_limited"}` |
| `APITimeoutError` | 重试 |
| `APIStatusError` 400 | 上下文 token 溢出（见 [05-concurrency.md §7.2 compress](05-concurrency.md)），记 ERROR，上抛给 Runner |
| 网络中断 | 重试；仍失败 → Runner 兜底 "处理出错，请稍后重试" |

### 5.6 成本控制

通过 `xiaopaw_llm_calls_total{model, status}` metric 计费：

- 查询 `sum(rate(xiaopaw_llm_calls_total{model="qwen3-max"}[1d])) * 86400` 获得日调用次数
- 乘以单价（公开）得成本
- 月度成本超预算时告警

---

## 6. 百度千帆 API（出站）

### 6.1 场景

`baidu_search` Skill 调用 `web_search` 接口做网络搜索。

### 6.2 凭证管理

```
BAIDU_API_KEY=bce-v3/xxxxx
```

**注入路径**：
1. 运维写到 `.env`
2. `main.py` 加载到 `config.yaml.baidu.api_key`
3. `CleanupService.write_baidu_credentials()` 启动时写到 `data/workspace/.config/baidu.json`（mode 0600）
4. Sub-Crew 沙盒内读该文件调用 API

**密钥不进模型**：Main Agent 完全不感知 API Key，Skill 脚本读本地 JSON 获取。

### 6.3 重试

`BaiduSearchTool` 用 `tenacity`：

```python
@external_api_retry((requests.exceptions.Timeout, requests.exceptions.ConnectionError))
def run(self, query: str, **kwargs):
    resp = requests.post("https://qianfan.baidubce.com/v2/ai_search/web_search",
                          headers={...}, json={...}, timeout=30)
    ...
```

### 6.4 降级

若 API key 为空（未配置），`CleanupService.write_baidu_credentials` 静默跳过；`baidu_search` Skill 运行时返回 `{"errcode": 503, "message": "百度搜索未配置"}`。

---

## 7. pgvector PG 协议（出站）

### 7.1 连接参数

```
MEMORY_DB_DSN=postgresql://xiaopaw_app:xxx@pgvector:5432/xiaopaw_memory?sslmode=require
```

**权限最小化**（v2 新增）：
- 独立 DB user `xiaopaw_app`
- 仅授权 `memories` 表的 `SELECT, INSERT`（不授予 DELETE；CleanupService 用 DBA 账号单独执行）

### 7.2 同步驱动 psycopg2

选 psycopg2 而非 psycopg3 / asyncpg 的原因（见 [sdk-verification-report.md §5](sdk-verification-report.md)）：
- 本机已装 `psycopg2-binary==2.9.11`；psycopg3（`psycopg[pool]`）未安装
- `async_index_turn` 已通过 `run_in_executor` / `asyncio.to_thread` 走线程池
- 开发环境依赖 pgvector 的 psycopg2 参数绑定方式（`pgvector.psycopg2.register_vector`）
- 无显著性能差异（qps 远低于 DB 瓶颈）

> **不要用 `psycopg_pool`**：它属于 psycopg3 生态，与 psycopg2 连接对象不兼容。

### 7.3 连接池（v2.1 默认启用）

```yaml
feature_flags:
  enable_pgvector_connection_pool: true   # 见 ssot/feature-flags.md#F12
```

```python
# xiaopaw/memory/indexer.py
from functools import cache
from psycopg2.pool import ThreadedConnectionPool

@cache
def _get_pool() -> ThreadedConnectionPool:
    """
    v2.1：psycopg2 标准库子模块，零额外依赖；线程安全（对齐 run_in_executor 线程池语义）。
    min=2, max=10（见 ssot/feature-flags.md#F12 默认值）。
    """
    return ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=_DB_DSN,
        application_name="xiaopaw",
    )

def _get_conn():
    pool = _get_pool()
    return pool.getconn()

def _put_conn(conn):
    _get_pool().putconn(conn)
```

### 7.4 错误语义

| 错误 | 处理 |
|---|---|
| `psycopg2.OperationalError`（连接失败） | `indexer` 记 WARNING，不冒泡；丢失本轮索引 |
| `psycopg2.errors.UniqueViolation` | `ON CONFLICT DO NOTHING` 已处理（不会真出现） |
| SQL 错误 | ERROR + 上抛（测试环境能发现；生产不应出现） |

### 7.5 trace_id 传递

```python
conn = psycopg2.connect(
    _DB_DSN,
    application_name=f"xiaopaw:{trace_id_var.get()}",
)
```

便于 pgvector 侧用 `pg_stat_activity.application_name` 反查慢查询。

---

## 8. AIO-Sandbox MCP（出站）

### 8.1 接入方式

Sub-Crew 通过 MCP 协议接入 sandbox 容器内的工具。**端口与网络拓扑**（见 [ssot/ports.md](ssot/ports.md) 8080 + [ssot/threats.md#T9](ssot/threats.md)）：
- 容器间地址：`http://aio-sandbox:8080/mcp`（Docker DNS 解析 + 容器内 8080）
- **不对宿主暴露端口**（compose 无 `ports:` 节，仅 `xiaopaw-net` 内部可见）

```python
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters

params = StdioServerParameters(
    command="docker",
    args=["exec", "-i", "aio-sandbox", "mcp-server"],
    env={"WORKSPACE": "/workspace"},
)
```

### 8.2 暴露的 MCP 工具

**全量清单**（v2 保留 v1）：
- `sandbox_execute_bash` / `sandbox_execute_code`
- `sandbox_file_operations` / `sandbox_str_replace_editor`
- `sandbox_convert_to_markdown`
- `browser_navigate` / `browser_get_markdown` / `browser_screenshot` / `browser_get_clickable_elements` / 等

### 8.3 v2 白名单过滤

```python
if flags.enable_mcp_whitelist and allowed_tools:
    tools = [t for t in all_tools if t.name in allowed_tools]
else:
    tools = all_tools  # 开发模式
```

详见 [07-security.md §7](07-security.md) 和 [02-modules.md §3.3](02-modules.md)。

### 8.4 workspace 挂载

```yaml
# xiaopaw-docker-compose.yaml
services:
  aio-sandbox:
    volumes:
      - ./data/workspace:/workspace:rw
```

**v2 路径隔离**：`skill-creator` / `memory-save` 等写文件类 Skill 在写入前 `Path.resolve()` 校验，确保路径在 `/workspace/sessions/{sid}/` 或 `/workspace/.config/` 内（见 [07-security.md T5](07-security.md)）。

### 8.5 超时

```python
result = await asyncio.wait_for(
    crew.akickoff(inputs={...}),
    timeout=cfg.sandbox.timeout_s,  # 默认 120s
)
```

超时后调 sandbox kill endpoint：

```python
await httpx.AsyncClient().post(
    f"{sandbox_url}/mcp/session/kill",
    json={"skill": skill_name},
    timeout=5,
)
```

---

## 9. /metrics 与 /health（运维）

### 9.1 端点总览

| Method | Path | 鉴权 | 用途 |
|---|---|---|---|
| `GET` | `/metrics` | Bearer Token | Prometheus scrape |
| `GET` | `/health` | 无 | 容器健康检查 |

**端口**：同一 aiohttp Application 监听 `:8090`（见 [ssot/ports.md](ssot/ports.md)）；Bearer middleware 仅作用于 `/metrics` 子路由，`/health` 不做鉴权（供容器 healthcheck 调用）。TestAPI 监听 `:9090`（独立）。

### 9.2 `/metrics` 响应

Prometheus 标准文本格式：

```
# HELP xiaopaw_inbound_total Inbound messages entered Runner dispatch.
# TYPE xiaopaw_inbound_total counter
xiaopaw_inbound_total{source="feishu",routing_type="p2p"} 123.0
xiaopaw_inbound_total{source="feishu",routing_type="group"} 45.0
# HELP xiaopaw_agent_latency_seconds End-to-end Runner._handle latency
# TYPE xiaopaw_agent_latency_seconds histogram
xiaopaw_agent_latency_seconds_bucket{le="0.5"} 10.0
...
```

### 9.3 `/health` 响应

```json
{
  "status": "ok",
  "git_sha": "e3a7b12abc",
  "uptime_sec": 36842,
  "started_at": "2026-04-19T00:00:00+00:00"
}
```

完整实现见 [06-observability.md §5](06-observability.md)。

### 9.4 鉴权安全（v2 新增）

```python
async def _metrics_handler(request):
    expected = os.getenv("XIAOPAW_METRICS_TOKEN", "")
    if not expected:
        if os.getenv("XIAOPAW_ENV") == "prod":
            return web.Response(status=500, text="METRICS_TOKEN missing")
        return web.Response(status=403)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not hmac.compare_digest(token.encode(), expected.encode()):
        return web.Response(status=401)
    return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
```

---

## 10. 内部协议

### 10.1 SenderProtocol（运行时多态）

`Runner` 接受 `SenderProtocol`，生产用 `FeishuSender`，测试用 `CaptureSender`。

```python
# xiaopaw/models.py
from typing import Protocol

class SenderProtocol(Protocol):
    # v2.1 约定：方法签名不含 root_id（话题定位由 routing_key 承载，见 §10.3）
    async def send(self, routing_key: str, content: str) -> str: ...
    async def send_thinking(self, routing_key: str, text: str = "⏳ 思考中...") -> str | None: ...
    async def update_card(self, card_msg_id: str, content: str) -> None: ...
    async def send_text(self, routing_key: str, text: str) -> None: ...
```

**v1 → v2 迁移**：v1 `sender.send(routing_key, content, root_id=...)` 调用点需删除 `root_id` 参数；v2 话题消息的定位由 `routing_key` 自带的 `thread:{chat_id}:{thread_id}` 前缀承载。

### 10.2 SkillLoaderInput / SkillResult

见 [03-data.md §9](03-data.md)。关键约定：
- 所有 Skill 返回 `SkillResult` Pydantic 模型（`errcode` / `message` / `data` / `files`）
- `errcode=0` 成功，非 0 失败
- 失败时 `message` 必须人类可读，供 Agent 回复给用户

### 10.3 InboundMessage

见 [03-data.md §2.2](03-data.md)。**v2.1 新增字段**：
- `trace_id: str` —— 每条入站消息生成的 16 字符 trace id（`uuid.uuid4().hex[:16]`），用于贯穿日志、metrics exemplar、DB `application_name`。

---

## 11. 接口版本与兼容性

- **飞书事件版本**：`im.message.receive_v1` / `im.chat.member.bot.added_v1`（lark-oapi 自动适配）
- **飞书 REST 版本**：lark-oapi SDK 固定版本，随依赖升级
- **MCP 协议**：MCP SDK 定义版本，兼容性跟随 AIO-Sandbox
- **TestAPI**：`POST /api/test/message` 作为稳定接口；未来如需变更，走 `/api/v2/test/message` 并保留旧版 90 天

---

## 下一步阅读

- **接口鉴权细节** → [07-security.md §4-5](07-security.md)
- **接口超时与重试** → [05-concurrency.md §8](05-concurrency.md)
- **接口监控指标** → [06-observability.md §4](06-observability.md)
- **接口配置字段** → [09-config.md](09-config.md)
