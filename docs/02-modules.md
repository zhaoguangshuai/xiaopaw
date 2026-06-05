# 02 模块详细设计

> 本文是 [DESIGN.md](../DESIGN.md) §4 的详细展开。
> 读者：实现工程师。
> 最后更新：2026-04-19（v2.1）
>
> 每个模块按以下结构描述：**职责 / 接口 / 关键实现 / v2 变化 / 测试要点 / 依赖**。
>
> **v2.1 权威引用**：锁清单见 [ssot/locks.md](ssot/locks.md)；Task 清单见 [ssot/tasks.md](ssot/tasks.md)；端口清单见 [ssot/ports.md](ssot/ports.md)；Feature Flags 见 [ssot/feature-flags.md](ssot/feature-flags.md)；威胁清单见 [ssot/threats.md](ssot/threats.md)。

---

## 目录

1. [接入层](#1-接入层)：FeishuListener / SessionRouter / TestAPI
2. [执行层](#2-执行层)：Runner / SessionManager / FeishuDownloader / FeishuSender
3. [Agent 层](#3-agent-层)：MemoryAwareCrew / Sub-Crew / SkillLoaderTool
4. [记忆层](#4-记忆层)：bootstrap / context_mgmt / token_counter / indexer
5. [LLM 层](#5-llm-层)：AliyunLLM
6. [工具层](#6-工具层)：BaiduSearchTool / AddImageToolLocal / IntermediateTool
7. [Skill 生态](#7-skill-生态)：13 个 Skills 的加固点
8. [基础设施](#8-基础设施)：Config / Cron / Cleanup / Observability / Security

---

## 1. 接入层

### 1.1 FeishuListener（`xiaopaw/feishu/listener.py`）

**职责**：维护飞书 WebSocket 长连接；解析事件为 `InboundMessage`；执行**应用层**重放防护 + 入站速率限制。

**核心接口**（v2.1）：

```python
class FeishuListener:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[InboundMessage], Awaitable[None]],
        replay_cache: ReplayCache,                # v2.1：必填（event_id LRU+TTL）
        rate_limiter: RateLimiter,                # v2.1：必填
        on_bot_added: Callable[[str, str], Awaitable[None]] | None = None,
        allowed_chats: list[str] | None = None,
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

> **v2.1 SDK 真相（见 [sdk-verification-report.md §1](sdk-verification-report.md)）**：`lark-oapi.ws.Client` **不支持** `encrypt_key` / `verification_token` 参数；WebSocket 模式下验签由飞书服务端在建连握手阶段完成（仅校验 `app_id + app_secret`），应用侧没有 HMAC 介入点，也无法实现"SDK 侧验签"。

**`lark.ws.Client` 真实签名**（`inspect.signature` 验证）：

```python
lark.ws.Client(
    app_id=cfg.feishu.app_id,
    app_secret=cfg.feishu.app_secret,
    log_level=lark.LogLevel.INFO,
    event_handler=_build_event_handler(),
    domain="https://open.feishu.cn",
    auto_reconnect=True,
)
```

**消息类型处理**（同 v1）：

| msg_type | 处理 |
|---|---|
| `text` | 提取 `content.text` |
| `image` / `file` | 记录 `attachment`，不下载 |
| `post` | `_extract_post_text` 解析富文本 |
| `audio` / `merge_forward` | 返回"暂不支持" |
| `sticker` | 忽略 |

**v2.1 变化**：
1. **WS 鉴权**：WebSocket 模式下飞书服务端做签名验证（`app_secret` 握手时交换 token）；应用层实现 event_id 重放防护。参数 `encrypt_key` / `verification_token` **从接口上移除**（SDK 不支持，见 Phase 0 验证报告）。
2. **重放防护**：基于 `event_id` 的 LRU+TTL 缓存（5 分钟窗口），重复 event_id 直接丢弃 → 见 [ssot/threats.md#T3](ssot/threats.md) 和 [ssot/feature-flags.md#F9](ssot/feature-flags.md) `enable_webhook_replay_cache`
3. **速率限制**：`RateLimiter.allow(sender_id)`，超限 → metric `xiaopaw_rate_limited_total` + 静默丢弃（见 [ssot/locks.md#L5](ssot/locks.md) 与 [ssot/threats.md#T7](ssot/threats.md)）
4. **trace_id 生成**：入口统一 `trace_id = uuid.uuid4().hex[:16]` 写入 `InboundMessage.trace_id`
5. **PII mask 延迟到日志层**：listener 本身不 mask `content`，但日志输出前 mask（[06-observability.md](06-observability.md)）

**测试要点**：
- `test_feishu_listener_replay.py`：相同 event_id 重复推送只处理一次（TC-P0-1-a/b）
- `test_feishu_listener_rate_limit.py`：单用户超 20/min 被拦截（TC-P2-8）
- `test_feishu_listener_allowed_chats.py`：白名单外群消息拒绝
- ~~`test_feishu_listener_verify_signature.py`~~：**v2.1 删除**（验签不在应用层）

**依赖**：`lark_oapi.ws.Client`、`RateLimiter` / `ReplayCache`（[observability/security.py](#83-observability-security-v2-新增)）

---

### 1.2 SessionRouter（`xiaopaw/feishu/session_key.py`）

**职责**：将飞书事件的三种会话类型映射为统一 `routing_key`。

**纯函数实现**（无状态，便于单元测试）：

```python
def resolve_routing_key(event: P2ImMessageReceiveV1) -> str:
    chat_type = event.event.message.chat_type
    if chat_type == "p2p":
        return f"p2p:{event.event.sender.sender_id.open_id}"
    elif chat_type == "group":
        chat_id = event.event.message.chat_id
        thread_id = getattr(event.event.message, "thread_id", None)
        if thread_id:
            return f"thread:{chat_id}:{thread_id}"
        return f"group:{chat_id}"
    raise ValueError(f"unknown chat_type: {chat_type}")
```

**v2 变化**：无。纯函数保持不变。

**测试**：针对 3 种 chat_type 各 2 个样例（含 thread_id 有无分支）。

---

### 1.3 TestAPI（`xiaopaw/api/test_server.py`）

**职责**：提供 HTTP 接口模拟飞书消息，用于自动化测试与调试。

**核心接口**：

```python
async def POST /api/test/message
Headers:
    Authorization: Bearer <XIAOPAW_TESTAPI_TOKEN>
Body:
    {
      "routing_key": "p2p:ou_dev",
      "text": "你好",
      "attachment": {...}?  # 可选
    }
Response:
    {
      "reply": "你好，我是小爪子...",
      "trace_id": "abc123...",
      "session_id": "s-xxx"
    }
```

**v2 变化**：
1. **prod 强制禁用**：启动时 `if XIAOPAW_ENV=prod and enable_test_api → 拒绝启动`
2. **Bearer Token 强制**：`XIAOPAW_TESTAPI_TOKEN` 必填，长度 ≥32 字符；constant-time 比对
3. **监听地址限制**：`bind` 只能 `127.0.0.1 / ::1 / localhost`
4. **CaptureSender**：内置 `CaptureSender`（future-based），同步返回 bot 回复

**测试**：`test_api_test_server.py`：无 token / 错误 token / 正常流程 / 非 loopback bind / prod env 全部覆盖。

---

## 2. 执行层

### 2.1 Runner（`xiaopaw/runner.py`）

**职责**：核心协调层，串联 Session 管理、Agent 执行、存储写入、消息回复。

**核心接口**：

```python
class Runner:
    def __init__(
        self,
        sender: SenderProtocol,
        session_mgr: SessionManager,
        agent_fn: AgentFn,
        cfg,                        # v2.1：读 cfg.runner.queue_idle_timeout_s
        max_queue_size: int = 10,
    ) -> None:
        ...
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._queue_gen: dict[str, int] = {}   # v2 新增
        self._dispatch_lock = asyncio.Lock()   # L1（见 ssot/locks.md#L1）
        self._pending_index_tasks: set[asyncio.Task] = set()  # v2 新增

    async def dispatch(self, inbound: InboundMessage) -> None: ...
    async def shutdown(self) -> None: ...
```

**`_dispatch_lock` 保护范围**（v2.1 扩展，见 [ssot/locks.md#L1](ssot/locks.md)）：
- `_queues` / `_workers` / `_queue_gen`（worker 生命周期管理）
- **`SessionManager._jsonl_locks` 的 `setdefault`**（两级锁第一级，见 §2.2）

**并发模型**（见 [05-concurrency.md](05-concurrency.md) §2）：
- 同一 `routing_key` 消息**串行**（per-rk Queue + worker）
- 不同 routing_key **并行**
- queue 空闲 `cfg.runner.queue_idle_timeout_s` 后 worker 自动退出，释放内存（默认 300s，由配置决定，不硬编码）
- **v2 关键：queue_gen counter** 防止清理竞态

**`_handle` 执行步骤**（v2.1）：

1. 生成 trace_id / 从 InboundMessage 透传
2. Slash command 拦截（不进入 Agent）
3. `session_mgr.get_or_create(routing_key)` → session_id
4. Downloader 下载附件（session 确定后）
5. `session_mgr.load_history(sid, max_turns=20)` （v2.1：`asyncio.to_thread` 倒序流式读，3.9+ 自动 copy_context）
6. 发送 Loading 卡片（`send_thinking`）
7. 构造 `MemoryAwareCrew(...)` 实例
8. `await crew.run_and_index()` 获取 `assistant_reply`（**仅**返回 reply；不再在 crew 内部 save）
9. **写三件事（v2.1 集中于 Runner）**：`save_session_ctx(sid, crew._last_msgs)` + `append_session_raw(sid, new_msgs)` + `session_mgr.append(sid, user=..., feishu_msg_id=..., assistant=...)`
10. **index 协程托管**：若 `crew._index_coroutine is not None`，`t = asyncio.create_task(crew._index_coroutine); self._pending_index_tasks.add(t); t.add_done_callback(self._pending_index_tasks.discard)`（见 [ssot/tasks.md#S1](ssot/tasks.md)）
11. `sender.update_card(card_msg_id, assistant_reply)`

**worker cleanup 正确性**（v2）：

```python
async def _worker(self, key: str, gen: int):
    idle_timeout = self._cfg.runner.queue_idle_timeout_s  # v2.1：读配置，不硬编码
    try:
        while True:
            try:
                inbound = await asyncio.wait_for(
                    self._queues[key].get(), timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                break
            try:
                await self._handle(inbound)
            except Exception:
                logger.exception("handle failed")
    finally:
        async with self._dispatch_lock:
            # 必须比较 gen，防止 dispatch 期间已 +1 创建新 queue
            if self._queue_gen.get(key) == gen:
                self._queues.pop(key, None)
                self._workers.pop(key, None)
                self._queue_gen.pop(key, None)
```

**shutdown 时 pending tasks**（v2.1：改用公开 API `shutdown_default_executor`，见 [ssot/tasks.md §4](ssot/tasks.md)）：

```python
async def shutdown(self) -> None:
    # 优先等所有 worker 结束
    for w in list(self._workers.values()):
        w.cancel()
    await asyncio.gather(*self._workers.values(), return_exceptions=True)

    # 再等 index tasks
    if self._pending_index_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending_index_tasks, return_exceptions=True),
                timeout=5,
            )
        except asyncio.TimeoutError:
            # run_in_executor 跑的是线程，gather 取消不终止线程（见并发验证报告 §4）
            XIAOPAW_INDEX_TASK_ZOMBIE.inc(len(self._pending_index_tasks))

    # v2.1：公开 API 替代 loop._default_executor.shutdown(...)
    loop = asyncio.get_running_loop()
    await loop.shutdown_default_executor()
```

**测试要点**：
- `test_runner_queue_gen_race.py`：并发 dispatch + worker timeout，验证新 queue 不被误删
- `test_runner_pending_tasks_shutdown.py`：注册 10 个 index task 后 shutdown，全部完成或被统计为 zombie
- `test_runner_parallel_across_routing_keys.py`：3 个 rk 并发，延迟 <1.5x 单 rk
- `test_runner_serial_within_routing_key.py`：同 rk 10 条消息顺序严格

---

### 2.2 SessionManager（`xiaopaw/session/manager.py`）

**职责**：管理 session 生命周期；维护 `index.json` 路由映射；读写 `{sid}.jsonl` 对话历史。

**核心接口**：

```python
class SessionManager:
    def __init__(
        self,
        data_dir: Path,
        max_active_sessions: int = 1000,  # v2 新增（LRUCache 上限）
    ) -> None:
        self._data_dir = data_dir
        self._sessions_dir = data_dir / "sessions"
        self._index_path = self._sessions_dir / "index.json"
        self._index_lock = asyncio.Lock()
        self._jsonl_locks: LRUCache[str, asyncio.Lock] = LRUCache(
            maxsize=max_active_sessions
        )  # v2 改造

    async def get_or_create(self, routing_key: str) -> SessionEntry: ...
    async def create_new_session(self, routing_key: str) -> SessionEntry: ...
    async def update_verbose(self, routing_key: str, verbose: bool) -> None: ...
    async def load_history(self, session_id: str, max_turns: int = 20) -> list[MessageEntry]: ...
    async def append(self, session_id: str, *, user, feishu_msg_id, assistant) -> None: ...
```

**v2.1 关键变化**：

**① LRUCache + 两级锁 append**（P0-4 修复，见 [ssot/locks.md#L1+L3](ssot/locks.md)）：

```python
async def append(self, session_id, *, user, feishu_msg_id, assistant):
    # 两级锁：先用 _dispatch_lock 保护 LRUCache 的 setdefault（L1）
    async with self._dispatch_lock:
        if session_id not in self._jsonl_locks:
            self._jsonl_locks[session_id] = asyncio.Lock()
        lock = self._jsonl_locks[session_id]
    # 再用 per-sid lock 真正保护 JSONL 写（L3）
    async with lock:
        jsonl_path = self._jsonl_path(session_id)
        async with aiofiles.open(jsonl_path, "a") as f:
            ...  # 写 message 行
```

**为什么两级锁**（来自 [concurrency-verification-report.md §1](concurrency-verification-report.md)）：
纯 "check + create + get" 三行虽无 await、协程内原子，但若 A 协程正持 `lock_v1` 并 `await` 写 JSONL，期间别的 sid 把 `sid_A` 从 LRU 挤掉、B 协程同 sid 进来 → `not in cache` True → 新建 `lock_v2`，A/B 持不同锁同时写同一 JSONL。第一级 `_dispatch_lock` 保证"check + create + get" 与别的协程的 LRUCache 写入互斥；第二级 per-sid lock 才是真正的 JSONL 互斥。
**运营要求**：`LRUCache(max_active_sessions=1000)` 必须 > 峰值 active session 数，否则 L3 正确性依赖退化为概率性（活跃超限触发驱逐=双锁并存窗口）。活跃超限应作为运维告警指标。

**② 流式倒序读**（H10 修复）：

```python
async def load_history(self, sid, max_turns=20):
    path = self._jsonl_path(sid)
    if not path.exists():
        return []

    def _collect():
        msgs = []
        for line in _iter_lines_reverse_sync(path):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "message":
                continue
            msgs.append(MessageEntry(...))
            if len(msgs) >= max_turns:
                break
        msgs.reverse()
        return msgs

    return await asyncio.to_thread(_collect)   # Python 3.9+ 自动 copy_context
```

`_iter_lines_reverse_sync` 实现：从文件末尾 seek + 4KB 块回退读 + 按行拼接，确保 UTF-8 多字节字符不被切断。

> **v2.1 ContextVar 真相**（见 [concurrency-verification-report.md §2](concurrency-verification-report.md)）：`asyncio.to_thread` 自 **Python 3.9** 起即自动 `copy_context()`，无需手工处理，也不是"Py 3.11+ bug"或"Py ≤3.13 bug"。trace_id 可直接在 `_collect` 里读取。

**索引与 JSONL 格式**（[03-data.md](03-data.md)）：
- `index.json`：write-then-rename 原子写
- `{sid}.jsonl`：append-only，第一行 `meta`，之后 `message`

**测试要点**：
- `test_session_lock_lifecycle.py`：1 万 session 创建 + GC 后 `len(_jsonl_locks) <= 1000`
- `test_session_lock_mutex.py`：100 协程并发 append 同 sid，JSONL 无交叉写入，所有行合法 JSON
- `test_load_history_does_not_block_loop.py`：load 10MB JSONL 时 heartbeat 延迟 <10ms
- `test_load_history_utf8_boundary.py`：4097 字节处恰好是中文多字节中间，不崩
- `test_load_history_corrupt_line.py`：损坏 JSON 行被跳过不影响整体

---

### 2.3 FeishuDownloader（`xiaopaw/feishu/downloader.py`）

**职责**：下载飞书消息附件（图片/文件）到 workspace sessions uploads 目录。

**核心接口**：

```python
class FeishuDownloader:
    def __init__(self, client: lark.Client, workspace_dir: Path) -> None: ...

    async def download_attachment(
        self,
        message_id: str,
        file_key: str,
        file_name: str | None,
        session_id: str,
    ) -> Path:
        """
        返回下载到的绝对路径，位于 workspace/sessions/{session_id}/uploads/
        """
```

**v2 变化**：
1. **路径遍历防护**：写入前 `resolved_path = (session_dir / safe_filename).resolve()`，验证 `resolved_path.is_relative_to(session_dir.resolve())`
2. **大小限制**：单文件 ≤30MB（飞书本身限制），超限拒绝
3. **文件名净化**：去掉 `../` / 空字节 / 特殊字符

---

### 2.4 FeishuSender（`xiaopaw/feishu/sender.py`）

**职责**：发送消息到飞书（create / reply / interactive card / update_card）；支持幂等（UUID）；**v2 新增** 429 感知 + 并发控。

**核心接口**：

```python
class FeishuSender(SenderProtocol):
    def __init__(
        self,
        client: lark.Client,
        max_retries: int = 3,
        retry_backoff: tuple[int, ...] = (1, 2, 4),
        max_concurrent: int = 5,  # v2 新增，见 ssot/locks.md#L4
    ) -> None:
        ...
        self._sem = asyncio.Semaphore(max_concurrent)  # L4

    async def send(self, routing_key: str, content: str) -> str: ...
    async def send_thinking(self, routing_key: str, text: str = "⏳ 思考中...") -> str | None: ...
    async def update_card(self, card_msg_id: str, content: str) -> None: ...
    async def send_text(self, routing_key: str, text: str) -> None: ...
```

**v2.1 限流处理**（ADR-005 + Phase 0 [sdk-verification-report.md §4/§6](sdk-verification-report.md)）：

> **错误码 / 属性真相**：①飞书限流错误码 `99991663 / 99991672 / 99991671` **目前未经官方文档完全确认**（SDK 源码 grep 零命中；`docs/reference/server-api-error-codes` 页面为 SPA 前端，WebFetch 抓不到正文）。实现时优先处理 **HTTP 429**（标准），SDK `resp.code` 按经验常量集合作兜底。②lark-oapi `BaseResponse` 的 HTTP 层属性叫 **`.raw`**（`.raw.status_code` / `.raw.headers`），不是 `.raw_response`。

```python
# 经验常量集合（待飞书官方文档证实后固化）
FEISHU_RATE_LIMIT_CODES = {99991663, 99991672, 99991671}
FEISHU_HTTP_RATE_LIMIT_STATUS = {429}

async def send(self, ...):
    async with self._sem:                             # L4，见 ssot/locks.md#L4
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.request(...)
                # ① HTTP 层 429（优先——这是标准语义）
                raw = getattr(resp, "raw", None)      # v2.1：属性名是 .raw
                if raw and raw.status_code in FEISHU_HTTP_RATE_LIMIT_STATUS:
                    retry_after = raw.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() \
                            else min(2 ** attempt, 60)
                    XIAOPAW_FEISHU_RATE_LIMIT_TOTAL.inc()
                    await asyncio.sleep(delay)
                    continue
                # ② SDK 层错误码（兜底，码值待官方文档验证）
                if getattr(resp, "code", 0) in FEISHU_RATE_LIMIT_CODES:
                    delay = min(2 ** attempt, 60)
                    XIAOPAW_FEISHU_RATE_LIMIT_TOTAL.inc()
                    await asyncio.sleep(delay)
                    continue
                return resp.message_id if hasattr(resp, "message_id") else None
            except (httpx.TimeoutException, ConnectionError):
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(self.retry_backoff[attempt])
```

**测试要点**：
- `test_sender_rate_limit_sdk_code.py`：mock `resp.code=99991663`，验证退避后重试
- `test_sender_rate_limit_http_429.py`：mock HTTP 429 + `Retry-After: 5`，验证 sleep 5s
- `test_sender_concurrency_limited.py`：并发 20 个 send，同时执行数 ≤5

---

## 3. Agent 层

### 3.1 MemoryAwareCrew（`xiaopaw/agents/main_crew.py`）

**职责**：主 Agent 编排 + `@before_llm_call` hook；每请求新实例避免状态污染。

**核心类**：

```python
@CrewBase
class MemoryAwareCrew:
    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(
        self,
        session_id: str,
        routing_key: str,
        user_message: str,
        sender: SenderProtocol,
        workspace_dir: Path,
        ctx_dir: Path,
        history_all: list[MessageEntry],
        db_dsn: str = "",
        max_history_turns: int = 20,
        sandbox_url: str = "",
        flags: FeatureFlags | None = None,  # v2 新增
    ) -> None:
        ...
        self.session_id = session_id
        self.routing_key = routing_key                    # v2.1：__init__ 里赋值（修 P1-12 遗漏）
        self.user_message = user_message
        self._sender = sender
        self._workspace_dir = workspace_dir
        self._ctx_dir = ctx_dir
        self._history_all = history_all
        self._db_dsn = db_dsn
        self._sandbox_url = sandbox_url
        self._flags = flags or FeatureFlags()
        self._index_coroutine: Coroutine | None = None   # v2 新增（Runner 取用）
        self._turn_start_ts = int(time.time() * 1000)
        self._last_msgs: list[dict] = []
        self._history_len = 0
        self._ctx_restored = False
        self._ctx_window_tokens = (
            flags.context_window_tokens
            if flags and hasattr(flags, "context_window_tokens")
            else 32000
        )  # v2.1：不再依赖 context.llm.context_window_size

    @before_llm_call
    def _hook_before_llm_call(self, context: LLMCallHookContext) -> None:
        messages = context.messages    # v2.1：必须 in-place 修改（CrewAI 契约）
        # 首次调用：恢复 ctx.json + Bootstrap 已在 backstory 完成
        if not self._ctx_restored:
            restored = load_session_ctx(self.session_id, ctx_dir=self._ctx_dir)
            if restored:
                messages.extend(restored)
            self._history_len = len(messages)
            self._ctx_restored = True
        # 每次：剪枝 + 压缩（必须 in-place，不能替换引用）
        prune_tool_results(messages, keep_turns=10)
        # v2.1：context_window 不从 context.llm 读（llm 可能是 str alias），用 config 固定值
        maybe_compress(
            messages, model_limit=self._ctx_window_tokens,
            fresh_keep_turns=10, compress_threshold=0.45,
        )
        # 保留拷贝供 run_and_index 持久化
        self._last_msgs = list(messages)

        # 注意：若 hook 需要"完全替换 messages"，必须 in-place：
        # messages[:] = system_msgs + summary_msgs + fresh_msgs
        # 禁止 context.messages = [...]（会脱钩，CrewAI 执行器仍用旧引用）

    @agent
    def orchestrator(self) -> Agent:
        bootstrap_backstory = build_bootstrap_prompt(self._workspace_dir)
        return Agent(
            config=self.agents_config["orchestrator"],
            backstory=bootstrap_backstory,
            tools=[SkillLoaderTool(
                session_id=self.session_id,
                workspace_dir=self._workspace_dir,
                history_all=self._history_all,
                sandbox_url=self._sandbox_url,
                flags=self._flags,
            )],
        )

    @task
    def orchestrate_task(self) -> Task:
        return Task(config=self.tasks_config["orchestrate_task"])

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents, tasks=self.tasks, verbose=True,
            step_callback=self._step_callback,
        )

    async def run_and_index(self) -> str:
        """
        v2.1 契约：仅返回 reply + 暴露 _index_coroutine。
        不再在内部调用 save_session_ctx / append_session_raw（由 Runner._handle 负责）。
        """
        result = await self.crew().akickoff(
            inputs={
                "user_message": self.user_message,
                "history": _format_history(self._history_all),
            }
        )
        # 提取 reply
        assistant_reply = result.raw or str(result)
        if result.pydantic and hasattr(result.pydantic, "reply"):
            assistant_reply = str(result.pydantic.reply)
        # v2.1：暴露 coroutine 给 Runner（Runner 负责 create_task + 托管）
        if self._db_dsn:
            self._index_coroutine = async_index_turn(
                session_id=self.session_id,
                routing_key=self.routing_key,
                user_message=self.user_message,
                assistant_reply=assistant_reply,
                turn_ts=self._turn_start_ts,
                db_dsn=self._db_dsn,
            )
        return assistant_reply
```

**v2.1 关键变化**（对比 v2.0-draft）：
1. **双写移走**（P0-5 修复）：`run_and_index` 只返回 reply；`save_session_ctx` / `append_session_raw` / `session_mgr.append` 三个写动作由 `Runner._handle` 集中完成，避免两处并存导致的不一致。
2. **`_index_coroutine` 暴露给 Runner**（ADR-003），由 Runner 包装成 Task 托管于 `_pending_index_tasks`（见 [ssot/tasks.md#S1](ssot/tasks.md)）。
3. **`@before_llm_call` in-place 契约**（P0-新 BeforeHook）：必须 `messages[:] = ...` 或 `append/extend`，禁止 `context.messages = [...]`。依据见 [sdk-verification-report.md §2](sdk-verification-report.md)。
4. **`context.llm.context_window_size` 不可依赖**（Phase 0 验证：`llm` 类型可能是 `str` alias，无任何属性）—— v2.1 改从 `self._ctx_window_tokens` 读配置固定值（默认 32000，由 `cfg.agent.context_window_tokens` 提供）。
5. **FeatureFlags 通过参数注入**，内部条件分支决定是否启用 MCP 白名单等。
6. **`@after_llm_call` 不使用**（课文 L19 已说明 CrewAI bug）。
7. **构造函数 in-place 绑定 `self.routing_key`**（修 P1-12 遗漏）。

**测试**（集成测试居多）：
- `test_memory_aware_crew_before_hook.py`：mock hook，验证 prune / compress 调用次数
- `test_memory_aware_crew_index_coroutine.py`：run_and_index 后 `_index_coroutine` 非空且可 await
- `test_memory_aware_crew_no_db.py`：db_dsn 为空时 `_index_coroutine is None`

---

### 3.2 SkillLoaderTool（`xiaopaw/tools/skill_loader.py`）

**职责**：渐进式披露 Skills；按类型派发（reference 返回 SKILL.md / task 触发 Sub-Crew）；**v2 新增** MCP tool 白名单 + `asyncio.wait_for` 超时。

**核心接口**：

```python
class SkillLoaderInput(BaseModel):
    skill_name: str
    task_context: str = ""
    history_page: int | None = None
    history_page_size: int | None = None

class SkillResult(BaseModel):
    errcode: int = 0
    message: str = ""
    data: dict = Field(default_factory=dict)

class SkillLoaderTool(BaseTool):
    name: str = "skill_loader"
    description: str = "..."
    args_schema: type = SkillLoaderInput

    _session_id: str = PrivateAttr()
    _workspace_dir: Path = PrivateAttr()
    _history_all: list = PrivateAttr()
    _sandbox_url: str = PrivateAttr()
    _flags: FeatureFlags = PrivateAttr()
    _skill_cache: dict = PrivateAttr(default_factory=dict)
    _routing_key: str = PrivateAttr()  # v2 新增

    def _run(self, skill_name: str, task_context: str = "", **kwargs) -> str:
        # v2.1：BaseTool._run 由 CrewAI `asyncio.to_thread(self.kickoff)` 在 worker 线程调用，
        # 此处无 running event loop，asyncio.run() 可直接使用（Phase 0 验证，见
        # sdk-verification-report.md §3）。优先实现 _arun 供原生 async 路径调用；_run 作为
        # 同步兜底路径，可保留"ThreadPoolExecutor 包 asyncio.run"的 v1 保守写法。
        return asyncio.run(self._execute_skill_async(skill_name, task_context, **kwargs))

    async def _execute_skill_async(self, skill_name, task_context, **kwargs) -> str:
        manifest = self._load_manifest(skill_name)

        # v2 安全：history_reader 内联（无 sandbox）
        if skill_name == "history_reader":
            return self._paginate_history(...)

        # v2 安全：search_memory 强制 routing_key 校验
        if skill_name == "search_memory":
            expected_rk = self._routing_key
            provided_rk = kwargs.get("routing_key")
            if provided_rk and provided_rk != expected_rk:
                return SkillResult(
                    errcode=403,
                    message="routing_key 校验失败，拒绝跨用户查询",
                ).model_dump_json()
            kwargs["routing_key"] = expected_rk  # 兜底注入

        if manifest["type"] == "reference":
            return self._get_skill_instructions(manifest)

        # task 型：构建 Sub-Crew
        allowed_tools = manifest.get("allowed_tools")  # v2 新增
        crew = build_skill_crew(
            skill_name=skill_name,
            instructions=self._get_skill_instructions(manifest),
            workspace_dir=self._workspace_dir,
            session_id=self._session_id,
            sandbox_url=self._sandbox_url,
            allowed_tools=allowed_tools,     # v2：传给 MCPToolFilter
            flags=self._flags,
        )

        timeout = self._flags.skill_timeout_s if self._flags.enable_skill_timeout else None
        try:
            if timeout:
                result = await asyncio.wait_for(
                    crew.akickoff(inputs={...}),
                    timeout=timeout,
                )
            else:
                result = await crew.akickoff(inputs={...})
        except asyncio.TimeoutError:
            await self._try_kill_sandbox_process(skill_name)
            XIAOPAW_SKILL_TIMEOUT_TOTAL.labels(skill=skill_name).inc()
            return SkillResult(
                errcode=408,
                message=f"Skill {skill_name} 执行超时 {timeout}s",
            ).model_dump_json()

        return self._parse_crew_result(result)

    async def _try_kill_sandbox_process(self, skill_name: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{self._sandbox_url}/mcp/session/kill",
                    json={"skill": skill_name},
                )
        except Exception:
            logger.warning("sandbox kill 失败", exc_info=True)
            XIAOPAW_SANDBOX_ZOMBIE_TOTAL.inc()
```

**SKILL.md 加载安全**（T6 防御）：

```python
def _load_manifest(self, skill_name: str) -> dict:
    skill_dir = self._skills_dir / skill_name
    md_path = skill_dir / "SKILL.md"
    if not md_path.exists():
        raise SkillNotFound(skill_name)
    content = md_path.read_text(encoding="utf-8")
    # 禁止 yaml.load，强制 safe_load
    try:
        meta = yaml.safe_load(_parse_frontmatter(content))
    except yaml.YAMLError as e:
        raise SkillLoadError(f"SKILL.md 解析失败: {e}")
    # 路径遍历防护
    for script in meta.get("scripts", []):
        resolved = (skill_dir / script).resolve()
        if not resolved.is_relative_to(skill_dir.resolve()):
            raise SkillLoadError(f"路径越界: {script}")
    return meta
```

**v2 新增**：
- `allowed_tools` 从 SKILL.md frontmatter 读取，通过 FeatureFlag 控制是否启用
- `_routing_key` 用于强制 `search_memory` 多租户隔离
- Skill 执行超时 + sandbox kill

**测试**：
- `test_skill_loader_timeout.py`：mock 卡死 Sub-Crew，验证 wait_for 触发 + kill
- `test_skill_loader_routing_key_reject.py`：LLM 传错 routing_key 被 403 拒绝
- `test_skill_loader_yaml_safe_load.py`：构造 YAML 注入 payload，raise SkillLoadError
- `test_skill_loader_path_traversal.py`：`scripts: ../../../etc/passwd` 被 raise

---

### 3.3 Sub-Crew 工厂（`xiaopaw/agents/skill_crew.py`）

**职责**：每次任务型 Skill 调用时动态构建隔离的 Sub-Crew，接入 AIO-Sandbox MCP。

**核心接口**：

```python
def build_skill_crew(
    skill_name: str,
    instructions: str,
    workspace_dir: Path,
    session_id: str,
    sandbox_url: str,
    allowed_tools: list[str] | None = None,  # v2 新增
    flags: FeatureFlags | None = None,
) -> Crew:
    mcp_server_params = StdioServerParameters(
        command="docker",
        args=[...sandbox...],
    )

    all_tools = _load_mcp_tools(mcp_server_params)

    # v2 白名单过滤
    if flags and flags.enable_mcp_whitelist and allowed_tools:
        tools = [t for t in all_tools if t.name in allowed_tools]
    else:
        tools = all_tools  # 开发模式：全开放

    agent = Agent(
        role=f"Skill Executor ({skill_name})",
        goal=instructions,
        backstory=_build_sandbox_directive(allowed_tools=allowed_tools),
        tools=tools,
        max_iter=flags.sub_agent_max_iter if flags else 20,
    )
    task = Task(description=instructions, agent=agent)
    return Crew(agents=[agent], tasks=[task])
```

**v2 变化**：
- MCP tool 按 `allowed_tools` 过滤（feature flag 控制，见 [ssot/feature-flags.md#F7](ssot/feature-flags.md) `enable_mcp_whitelist` 和 [ssot/threats.md#T1](ssot/threats.md) Prompt Injection 防御）
- backstory 动态生成，只描述 `allowed_tools` 里真实存在的工具

---

## 4. 记忆层

### 4.1 bootstrap（`xiaopaw/memory/bootstrap.py`）

**职责**：L19 Bootstrap 阶段，读取 workspace 4 文件（soul/user/agent/memory.md）构建 Agent backstory。

**核心函数**：

```python
def build_bootstrap_prompt(workspace_dir: Path) -> str:
    """返回 XML 标签包裹的 4 节 backstory。memory.md 硬上限 250 行。"""
    sections = []
    for name, tag in [("soul.md", "soul"), ("user.md", "user"),
                      ("agent.md", "agent"), ("memory.md", "memory")]:
        path = workspace_dir / name
        if not path.exists():
            continue  # 缺失静默跳过
        content = path.read_text(encoding="utf-8")
        if name == "memory.md":
            lines = content.splitlines()
            if len(lines) > MEMORY_HARD_LIMIT:
                logger.warning(
                    "memory.md 超 %d 行（%d），触发截断告警",
                    MEMORY_HARD_LIMIT, len(lines)
                )
                XIAOPAW_MEMORY_OVERFLOW_TOTAL.inc()
                content = "\n".join(lines[:MEMORY_HARD_LIMIT])
        sections.append(f"<{tag}>\n{content}\n</{tag}>")
    return "\n\n".join(sections)
```

**v2 变化**：
- 超限不再静默截断，改为 warning + metric 告警（M9 修复）
- 阈值 `MEMORY_HARD_LIMIT=250` 在 `memory/config.py` 统一定义
- 与 `memory-save/SKILL.md` 的 150/180 阈值一致（说明文档）

---

### 4.2 context_mgmt（`xiaopaw/memory/context_mgmt.py`）

**职责**：L19 上下文生命周期管理——剪枝、压缩、ctx.json 快照、raw.jsonl 审计。

**核心函数**（接口同 v1，实现加固）：

```python
def prune_tool_results(messages: list[dict], keep_turns: int = 10) -> None: ...
def chunk_by_tokens(messages: list[dict], chunk_tokens: int = 2000) -> list[list[dict]]: ...
# v2.1：不再接收 CrewAI hook context（llm.context_window_size 不可依赖，见 §3.1）；
# 改为直接传 model_limit token 数（由调用方从 config 读）。
def maybe_compress(messages, *, model_limit: int = 32000, fresh_keep_turns: int = 10,
                   chunk_tokens: int = 2000, compress_threshold: float = 0.45) -> None: ...
def load_session_ctx(session_id: str, ctx_dir: Path) -> list[dict]: ...
def save_session_ctx(session_id: str, messages: list[dict], ctx_dir: Path) -> None: ...
def append_session_raw(session_id: str, messages: list[dict], ctx_dir: Path) -> None: ...
```

**v2.1 加固点**：

1. **`maybe_compress` cutoff 保护 tool_calls pair**（H4 修复）：

```python
def maybe_compress(messages, *, model_limit: int = 32000, ...):
    ...
    approx_tokens = count_tokens(non_system)  # v2：用 token_counter 模块
    if approx_tokens / model_limit < compress_threshold:
        return
    ...
    cutoff = user_indices[-fresh_keep_turns]
    # v2：向前扩展直到遇到完整 user 边界
    while cutoff > 0:
        prev = non_system[cutoff - 1]
        if prev.get("role") == "user":
            break
        # 如果 prev 是 assistant.tool_calls 未匹配对应 tool 响应，继续前移
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            # 检查 cutoff 之后有没有对应 tool_call_id 的 tool 消息
            tool_ids_needed = {tc["id"] for tc in prev["tool_calls"]}
            tool_ids_found = {
                m.get("tool_call_id") for m in non_system[cutoff:]
                if m.get("role") == "tool"
            }
            if not tool_ids_needed.issubset(tool_ids_found):
                cutoff -= 1
                continue
        break
    old_msgs = non_system[:cutoff]
    fresh_msgs = non_system[cutoff:]
    ...
```

2. **`count_tokens` 用 token_counter 模块**（见 [§4.3](#43-token_counter-v2-新增)）。

3. **`_summarize_chunk` 异常降级**：LLM 调用失败返回 `"[压缩失败，内容省略]"`，不阻塞主流程（v1 已有，保留）。

**测试要点**：
- `test_compress_preserves_tool_call_pair.py`：构造 assistant.tool_calls + tool 消息分布在 cutoff 两侧，压缩后 fresh_msgs 仍然完整
- `test_compress_token_count_accurate.py`：Qwen tokenizer 实际 token 数 vs 估算偏差 <10%
- `test_ctx_json_restore.py`：save + load 往返一致

---

### 4.3 token_counter（`xiaopaw/memory/token_counter.py`，v2 新增）

**职责**：精确计数消息 token 数（Qwen 官方 tokenizer 优先）。

**三级 fallback 链**（受 [ssot/feature-flags.md#F1](ssot/feature-flags.md) `token_counter_mode` 控制，三值：`qwen_official` / `hf_qwen` / `rough`）：

1. `dashscope.get_tokenizer("qwen-max")` — 首选
2. `HuggingFace AutoTokenizer.from_pretrained("Qwen/Qwen2-7B-Instruct")` — 离线可用
3. `rough` — `len(content) // 2` 兜底

**核心接口**：

```python
_tokenizer = None

def _get_tokenizer():
    """惰性初始化，避免 import 阶段网络 IO 崩溃。"""
    global _tokenizer
    if _tokenizer is None:
        try:
            from dashscope import get_tokenizer
            _tokenizer = get_tokenizer("qwen-max")
            logger.info("Qwen 官方 tokenizer 加载成功")
        except Exception:
            try:
                from transformers import AutoTokenizer
                _tokenizer = AutoTokenizer.from_pretrained(
                    "Qwen/Qwen2-7B", trust_remote_code=False, local_files_only=True,
                )
                logger.info("HuggingFace Qwen tokenizer 加载成功（本地）")
            except Exception:
                logger.warning("Qwen tokenizer 均不可用，降级 rough", exc_info=True)
                _tokenizer = "rough"
    return _tokenizer

def count_tokens(messages: list[dict]) -> int:
    t = _get_tokenizer()
    if t == "rough":
        return sum(len(str(m.get("content", ""))) // 2 for m in messages)
    total = 0
    for m in messages:
        content = str(m.get("content", ""))
        if hasattr(t, "encode"):
            total += len(t.encode(content))
        else:
            total += len(t(content)["input_ids"])
        total += 4  # role + separator
    return total
```

**Phase 0 校准报告**：[docs/tokenizer-calibration.md](tokenizer-calibration.md)

**测试**：
- `test_token_counter_fallback.py`：所有 tokenizer 失败时降级 rough 不崩
- `test_token_counter_accuracy.py`：跑 100 条样本对比 dashscope vs cl100k_base 偏差

---

### 4.4 indexer（`xiaopaw/memory/indexer.py`）

**职责**：L21 搜索层——每轮对话后异步提取摘要+向量化+入 pgvector。

**v2 关键变化**：

1. **LLM client 单例化**（H5 修复）：

```python
from functools import cache

@cache
def _get_llm_client():
    from openai import OpenAI
    return OpenAI(api_key=_QWEN_API_KEY, base_url=_QWEN_BASE_URL)

@cache
def _get_embed_client():
    return _get_llm_client()
```

2. **`async_index_turn` 签名不变**（接口兼容），但由 Runner 包装为 Task：

```python
async def async_index_turn(
    session_id: str, routing_key: str, user_message: str,
    assistant_reply: str, turn_ts: int, db_dsn: str,
) -> None:
    if not db_dsn:
        return  # 静默跳过
    await asyncio.get_running_loop().run_in_executor(
        None,
        _index_single_turn,
        session_id, routing_key, user_message, assistant_reply, turn_ts, db_dsn,
    )
```

3. **embedding / extract 使用 tenacity 重试**（M7 修复）：

```python
@external_api_retry((APIError, TimeoutError, ConnectionError))
def extract_summary_and_tags(user_message, assistant_reply): ...

@external_api_retry((APIError, TimeoutError, ConnectionError))
def embed_texts(texts): ...
```

4. **错误不冒泡**：`_index_single_turn` 吞 `Exception` 记 warning（保留 v1 行为）。

---

## 5. LLM 层

### 5.1 AliyunLLM（`xiaopaw/llm/aliyun_llm.py`）

**职责**：CrewAI `BaseLLM` 适配器，通过 DashScope 兼容 OpenAI API 调用 Qwen。

**v2 变化**：

1. **`_normalize_multimodal_tool_result` structured marker**（M8 修复）：

```python
# v1: f"xp_img:v1:{url}" 可被注入绕过
# v2: JSON 序列化
IMAGE_MARKER_KEY = "__xp_type"
IMAGE_MARKER_VALUE = "img"

def make_image_payload(url: str) -> str:
    return json.dumps({IMAGE_MARKER_KEY: IMAGE_MARKER_VALUE, "url": url})

def is_image_payload(text: str) -> bool:
    try:
        data = json.loads(text)
        return isinstance(data, dict) and data.get(IMAGE_MARKER_KEY) == IMAGE_MARKER_VALUE
    except (json.JSONDecodeError, TypeError):
        return False
```

2. **`AddImageToolLocal._run()` 返回使用新 marker**（改造）。

**其他行为（保留 v1）**：
- sync / async 双接口（CrewAI 要求）
- tenacity 重试（5xx / timeout）
- function calling / multimodal image
- `QWEN_DEBUG_PAYLOAD=1` 环境变量输出完整 payload

---

## 6. 工具层

### 6.1 BaiduSearchTool / AddImageToolLocal / IntermediateTool

这三个工具的 v2 变化较小：

- **BaiduSearchTool**：加 `tenacity` 重试（M7）；凭证读取不变
- **AddImageToolLocal**：path traversal 防护（已存在）；返回使用 v2 structured marker
- **IntermediateTool**：v1 `_run()` 为空实现，v2 补充最小持久化（记一行到 `data/traces/{sid}/intermediate.jsonl`）

---

## 7. Skill 生态

共 13 个 Skills（保留 v1 全部），按 v2 加固点分组说明：

### 7.1 文件处理类（pdf / docx / pptx / xlsx）

- **SKILL.md frontmatter 新增 `allowed_tools`**：例 `[sandbox_file_operations, sandbox_execute_code]`
- 其他不变

### 7.2 交互类（feishu_ops / scheduler_mgr / history_reader）

- **feishu_ops**：10 个独立脚本不变；凭证来自 `/workspace/.config/feishu.json`（CleanupService 启动时写入，mode 0600）
- **scheduler_mgr**：通过 CronService 的 filelock 保护（H8 修复）
- **history_reader**：内联分页不变

### 7.3 搜索类（baidu_search / web_browse / search_memory）

- **baidu_search**：`allowed_tools: [sandbox_execute_code]`（只跑 search.py 脚本）；凭证 `/workspace/.config/baidu.json`
- **web_browse**：`allowed_tools: [sandbox_convert_to_markdown, browser_navigate, browser_get_markdown, browser_screenshot, browser_get_clickable_elements]`（细化）
- **search_memory**（v2 重大加固）：
  - SKILL.md `routing_key` 从 optional 改 required
  - `scripts/search.py` 强制校验 `routing_key`
  - SkillLoader 层校验拒绝跨用户

### 7.4 记忆类（memory-save / skill-creator / memory-governance）

- **memory-save**（v2 加固）：
  - scripts/save.py 加 `filelock.FileLock(f"{topic}.md.lock")`（H9）
  - scripts/save.py 加 `is_safe_memory_content(text)` 过滤 BLOCKED_PATTERNS（T2）
  - 超时 10s 报错，不降级合并（v1 v2 差异）
- **skill-creator**（v2 加固）：
  - 写入 SKILL.md 前做 YAML 合法性验证
  - `scripts:` 路径白名单（不允许 `../`）
  - `load_skills.yaml` 更新使用 filelock
- **memory-governance**：仅报告不自动执行（L20 CRITICAL 约束，保持）

### 7.5 Skill 清单（`skills/load_skills.yaml`）

```yaml
skills:
  - name: pdf
    type: task
    allowed_tools: [sandbox_file_operations, sandbox_execute_code]
  - name: docx
    type: task
    allowed_tools: [sandbox_file_operations, sandbox_execute_code]
  ...
  - name: search_memory
    type: task
    allowed_tools: [sandbox_execute_code]
  - name: memory-save
    type: task
    allowed_tools: [sandbox_file_operations, sandbox_execute_code]
  - name: history_reader
    type: reference  # 内联，无 Sub-Crew
```

---

## 8. 基础设施

### 8.1 Config 模块（`xiaopaw/config/`，v2 新增）

**`validator.py`**：Pydantic schema 校验 `config.yaml`；启动失败时给出明确错误字段路径。

**`safety.py`**：凭证安全校验（正则+hash 弱密码 + `FORBIDDEN_DEFAULTS`）—— 见 [DESIGN.md §4 Phase 1](../../multi-agent/review_L22/02_重构设计文档_xiaopaw-with-memory_v2.md)

**`flags.py`**：FeatureFlags registry；每个 flag 对应 metric。

### 8.2 Cron（`xiaopaw/cron/`）

**`service.py`**：asyncio 精确 timer；mtime+size 热重载。
**`storage.py`**（v2 新增）：所有读写通过 `filelock.FileLock` 保护；DLQ 写入 `tasks.dlq.jsonl`。

### 8.3 Observability & Security（`xiaopaw/observability/`）

#### trace.py（v2 新增）

```python
import contextvars

trace_id_var = contextvars.ContextVar("trace_id", default="-")

def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]

async def run_in_executor_with_context(fn, *args):
    """
    v2.1 语义说明（见 concurrency-verification-report.md §2）：
    - asyncio.to_thread 自 Python 3.9 起【自动 copy_context】，无需此 helper；
    - loop.run_in_executor(None, fn) 【任何 Python 版本都不自动 copy_context】
      （这是 run_in_executor 的契约，与 Python 版本无关）；
    - 故本 helper 仅在不得不用 run_in_executor（如传入自定义 executor）时使用。
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(None, ctx.run, fn, *args)
```

**shutdown 修正**（v2.1）：shutdown 时用 **`await loop.shutdown_default_executor()`**（Python 3.9+ 公开 API，3.12+ 支持 timeout），替代 `loop._default_executor.shutdown(wait=True)`（私有 API 且同步阻塞事件循环）。详见 [ssot/tasks.md §4](ssot/tasks.md)。

#### logging_config.py（v2 加固）

```python
class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_pii(record.getMessage()),  # v2 新增
            "caller": f"{record.filename}:{record.lineno}:{record.funcName}",  # v2 新增
            "trace_id": trace_id_var.get(),  # v2 新增
            "routing_key": getattr(record, "routing_key", None),
            "session_id": getattr(record, "session_id", None),
            "feishu_msg_id": getattr(record, "feishu_msg_id", None),
        }
        if record.exc_info:
            payload["stacktrace"] = self.formatException(record.exc_info)  # v2 新增
        return json.dumps(payload, ensure_ascii=False)
```

#### pii_mask.py（v2 新增）

```python
PII_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"),
     lambda m: m.group()[:3] + "****" + m.group()[-4:]),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
     lambda m: "***@***"),
    (re.compile(r"\d{17}[\dXx]"),
     lambda m: m.group()[:6] + "********" + m.group()[-4:]),
]

def mask_pii(text: str) -> str:
    for pat, repl in PII_PATTERNS:
        text = pat.sub(repl, text)
    return text
```

#### security.py（v2 新增）

```python
class RateLimiter:
    def __init__(self, per_user_per_minute: int = 20):
        self._limit = per_user_per_minute
        self._windows = defaultdict(deque)

    def allow(self, user_key: str) -> bool:
        now = time.time()
        window = self._windows[user_key]
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= self._limit:
            return False
        window.append(now)
        return True


BLOCKED_PATTERNS = [
    re.compile(r"<\s*system\s*>", re.I),
    re.compile(r"(忽略|ignore)\s*(之前|previous|上面)的?\s*(指令|instruction|prompt)", re.I),
    re.compile(r"你现在是|你的新身份|pretend\s+you\s+are", re.I),
    re.compile(r"```\s*system", re.I),
]

def is_safe_memory_content(text: str, max_length: int = 2000) -> bool:
    if len(text) > max_length:
        return False
    return not any(p.search(text) for p in BLOCKED_PATTERNS)


class ReplayCache:
    """
    v2.1：应用层 event_id 去重，LRU + TTL 5 min。
    职责：防御 T3 飞书 Webhook 重放（见 ssot/threats.md#T3）。
    反受 Feature Flag F9 `enable_webhook_replay_cache` 控制（见 ssot/feature-flags.md#F9）。

    真相约束（见 concurrency-verification-report.md §6）：
    - 进程级缓存，重启丢失；5 分钟 TTL 窗口内跨重启重放仍可成功
    - 单节点/容器重启场景可接受；多节点或要求严格防御时，改用 Redis：
      SET event_id 1 EX 300 NX
    """
    def __init__(self, maxsize: int = 10000, ttl_sec: int = 300):
        self._cache = LRUCache(maxsize=maxsize)
        self._ttl = ttl_sec
        self._lock = asyncio.Lock()  # L5（ssot/locks.md#L5）

    async def seen(self, event_id: str) -> bool:
        async with self._lock:
            now = time.time()
            if event_id in self._cache:
                ts = self._cache[event_id]
                if now - ts < self._ttl:
                    return True
            self._cache[event_id] = now
            return False
```

#### metrics.py（v2 精简为 8 个）

见 [DESIGN.md §8](../DESIGN.md)。每个指标有 Prometheus 定义和对应 helper 函数。

#### metrics_server.py（v2 Bearer + constant_time_equals）

```python
def _constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

async def _metrics_handler(request):
    expected = os.getenv("XIAOPAW_METRICS_TOKEN", "")
    if not expected:
        if os.getenv("XIAOPAW_ENV") == "prod":
            return web.Response(status=500, text="METRICS_TOKEN missing")
        return web.Response(status=403)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not _constant_time_equals(token, expected):
        return web.Response(status=401)
    return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
```

### 8.4 Cleanup（`xiaopaw/cleanup/service.py`）

**职责**：双触发（启动 + 每日 3:00）；按策略清理 `workspace / traces / sessions`；启动时写入 `.config/feishu.json` 和 `.config/baidu.json`。

**v2 变化**：
- 失败多次重试（3 次，间隔 5 分钟）
- `xiaopaw_cleanup_failure_total` metric

---

## 9. 签名一致性与 v1→v2 迁移速查

### 9.1 SenderProtocol（v2 约定：不含 `root_id`）

```python
# v2 约定（见 §10.1 of 04-api.md）
class SenderProtocol(Protocol):
    async def send(self, routing_key: str, content: str) -> str: ...
    async def send_thinking(self, routing_key: str, text: str = "⏳ 思考中...") -> str | None: ...
    async def update_card(self, card_msg_id: str, content: str) -> None: ...
    async def send_text(self, routing_key: str, text: str) -> None: ...
```

**v1 → v2 迁移**：v1 所有 `sender.send(routing_key, content, root_id=...)` 调用点需删除 `root_id` 参数；话题消息的定位由 `routing_key` 自带的 `thread:{chat_id}:{thread_id}` 前缀承载，不再在发送接口上出现。

### 9.2 `build_skill_crew` 参数对照

| # | v1 参数 | v2.1 参数 | 说明 |
|---|---|---|---|
| 1 | `skill_name` | `skill_name` | 保持 |
| 2 | `instructions` | `instructions` | 保持 |
| 3 | `workspace_dir` | `workspace_dir` | 保持 |
| 4 | `session_id` | `session_id` | 保持 |
| 5 | `sandbox_url` | `sandbox_url` | 保持 |
| 6 | —（无） | `allowed_tools: list[str] \| None` | **新增**，MCP 工具白名单（F7） |
| 7 | —（无） | `flags: FeatureFlags \| None` | **新增**，注入 feature flags |

### 9.3 `MemoryAwareCrew.__init__` 参数（v2.1 最终）

新增 `flags` 参数；内部保存 `self.routing_key`（修 P1-12 遗漏）；内部计算 `self._ctx_window_tokens`（不依赖 `context.llm.context_window_size`）；`self._index_coroutine` 由 Runner 取用后 create_task。

### 9.4 FeishuListener 参数（v2.1 最终）

| v2.0-draft | v2.1 | 原因 |
|---|---|---|
| `encrypt_key: str` | 删除 | SDK 不支持 |
| `verification_token: str` | 删除 | SDK 不支持 |
| `replay_cache: ReplayCache \| None` | `replay_cache: ReplayCache`（必填） | 应用层重放防护必须启用 |
| `rate_limiter: RateLimiter \| None` | `rate_limiter: RateLimiter`（必填） | 同上 |

---

**下一步阅读**：
- 数据格式细节 → [03-data.md](03-data.md)
- 并发与锁模型 → [05-concurrency.md](05-concurrency.md)
- 威胁模型 → [07-security.md](07-security.md)
- SSOT 权威清单 → [ssot/](ssot/)
