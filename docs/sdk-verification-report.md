# SDK 真相报告（Phase 0 前置验证）

**验证日期**：2026-04-19
**验证环境**：`/usr/local/lib/python3.12/dist-packages/`
**已装版本**：lark-oapi `1.5.3` / crewai `1.10.1` / psycopg2-binary `2.9.11`（psycopg/psycopg-pool/pgvector/psycopg3 **未安装**）

---

## 1. lark-oapi ws.Client — ❌ 关键假设不成立

**真实签名**（`inspect.signature(lark_oapi.ws.client.Client.__init__)`）：

```
(self, app_id: str, app_secret,
 log_level: LogLevel = LogLevel.INFO,
 event_handler: EventDispatcherHandler = None,
 domain: str = 'https://open.feishu.cn',
 auto_reconnect: bool = True) -> None
```

**结论**：
- ❌ `encrypt_key` **不存在**
- ❌ `verification_token` **不存在**
- ✅ WebSocket 长连模式下，验签/解密由**飞书服务端在建立长连时完成**（客户端只出示 `app_id` + `app_secret`），SDK 客户端不做也无需做验签
- ❌ SDK 源码里**没有** event_id 去重逻辑（grep 确认 `lark_oapi` 目录下仅 `dispatcher_handler.py` / `action_handler.py` / `event_and_callback_encrypt_strategy.py` 三处出现 `encrypt_key`，均属于 HTTP 回调路径，与 WS 长连无关）
- ✅ v1 实际用法（`xiaopaw/feishu/listener.py:164`）只传 `app_id / app_secret / log_level / event_handler`，与真实签名一致

**T3 验签加固该怎么改**：
1. 删掉"SDK 客户端侧做 encrypt_key 验签"的描述，改为"WebSocket 长连由 SDK 在握手阶段用 app_secret 鉴权，业务代码无需也无法介入 HMAC 校验"
2. 重放防护必须**应用自己实现**：在 `on_message` 入口维护 `event_id -> ts` LRU/Redis，TTL ~5 min
3. 如仍要保留传统 `encrypt_key` 校验，只能走 HTTP 回调（另起一个 FastAPI endpoint + `EventDispatcherHandler.builder().encrypt_key(...)`），与 WS 模式二选一

---

## 2. CrewAI `@before_llm_call` — ⚠️ 装饰器存在但 v2 文档的一些字段假设错误

**装饰器存在** ✅：`from crewai.hooks import before_llm_call`（`crewai/hooks/hook_decorators.py`）内部 `_create_hook_decorator` + `register_before_llm_call_hook`，**同时支持裸用 `@before_llm_call` 与 `@before_llm_call(agents=[...])`**。

**`LLMCallHookContext` 字段**（`crewai/hooks/llm_hooks.py` 源码）：

```python
executor: CrewAgentExecutor | AgentExecutor | LiteAgent | None
messages: list[LLMMessage]      # 可变，in-place 修改
agent: Any
task: Any
crew: Any
llm: BaseLLM | None | str | Any
iterations: int
response: str | None           # 仅 after_llm_call 有值
```

**结论**：
- ✅ `context.messages` 存在，且 docstring 明确说"必须 in-place 修改（append/extend），不要 `context.messages = []`"——v2 若写"替换 messages 列表"必须改为"append/extend/原地清空再 append"
- ✅ `context.llm` 存在
- ❌ `context.llm.context_window_size` **不保证存在**——`llm` 的类型标注是 `BaseLLM | None | str | Any`，字符串 alias（如 `"qwen-max"`）情况下不是对象，没有任何属性；即使是 `LLM` 实例，属性名在 CrewAI 里通常是 `get_context_window_size()` 方法或 `max_tokens`，不是 `context_window_size`。v2 里所有 `context.llm.context_window_size` 必须改为带防御的 `getattr` 或显式查表。
- ✅ `@before_llm_call` **必须绑在 `@CrewBase` 类上**（v1 `main_crew.py:211` 有注释：「手动构造 Crew 无法绑定 @before_llm_call hook」）—— v2 若描述为"装饰普通函数"是错的

**CrewAI 内部并发**：`Crew.kickoff_async` 实现为 `return await asyncio.to_thread(self.kickoff, ...)`（`crewai/crew.py:815/833`），即 **kickoff 整个跑在 `asyncio.to_thread` 默认 ThreadPoolExecutor 的一个 worker 线程里**。这个线程里**没有 running event loop**。

---

## 3. BaseTool `_run` 在 async 上下文 — ✅ 真相与 v1 实现一致

**`BaseTool._run`**：同步方法（`crewai/tools/base_tool.py:229`），签名 `def _run(self, *args, **kwargs) -> Any`。
**`BaseTool._arun`**：`async def`（`:213`），如果不覆盖会抛 `NotImplementedError`。

**执行路径真相**：
- `crew.akickoff()` → `asyncio.to_thread(self.kickoff)` → kickoff 同步栈 → Agent._execute_tool → `tool.run()` → `tool._run()`
- 被调用时**当前线程不是主事件循环线程**，`asyncio.get_running_loop()` 会抛 `RuntimeError`
- 所以 `asyncio.run(coro)` **能正常工作**（它会新建一个 event loop）

**v1 `skill_loader.py` 的实际做法**（证据）：同时提供 `_arun`（原生 await，给 akickoff 链路）**和** `_run`（用 `ThreadPoolExecutor(max_workers=1) + pool.submit(asyncio.run, coro)` 再套一层新线程，规避"万一 `_run` 被从有 loop 的线程调到"的边界情况）。

**v2 应采取的写法**：
- 首选实现 `_arun`（CrewAI async 路径会优先调 `_arun`）
- `_run` 用 `ThreadPoolExecutor` 包一层 `asyncio.run(coro)`，不要直接 `asyncio.run(...)` 裸调（与 v1 一致；这是**最保守**写法）
- ❌ 不需要 `nest_asyncio`（v1 未用，测试全 pass）

---

## 4. 飞书限流错误码 — ⚠️ 未在 SDK 和官方文档页中找到任何证据

**SDK 源码搜索结果**（`grep -r '99991663|99991672|99991671|99991400|rate.?limit|RateLimit' lark_oapi/`）：
- 无任何命中

**SDK 源码里关于 lark headers 的常量**（`core/const.py`）：
- `X-Lark-Request-Timestamp` / `X-Lark-Request-Nonce` / `X-Lark-Signature` / `X-Tt-Logid`
- ❌ **没有** `Retry-After`、**没有** `X-Lark-Request-RateLimit-Reset`

**官方文档 WebFetch**：两次访问（`server-error-codes` / `reference/server-api-error-codes`）都只返回页面标题，未能抓到正文——页面是 SPA 前端渲染，WebFetch 拿不到。无法在本次验证中证实/证伪具体码值。

**结论**：
- ⚠️ `99991663 / 99991672 / 99991671 / 99991400` 是否为真实限流码**本次无法证实**，但 v2 文档不应把它们写死当白名单；应**只判 `resp.code != 0` + 日志记录 code/msg**
- ⚠️ `Retry-After` header 可能存在（HTTP 标准），但 SDK 不会自动 respect，需应用层读 `resp.raw.headers.get("Retry-After")`
- ❌ `X-Lark-Request-RateLimit-Reset` **几乎可确定是虚构的**——SDK 常量里没有，飞书公开 header 命名约定里也没有
- **建议**：限流检测用「收到非 0 code 且 msg 含 `rate limit` / `限流` / `too many` 关键词」+ 退避 1s/2s/4s，不绑死具体 code

---

## 5. psycopg 生态 — ❌ v2 假设的 `psycopg_pool` 未安装，v1 用的是 psycopg2

**已装**：只有 `psycopg2-binary==2.9.11`。`psycopg`（psycopg3）、`psycopg-pool`、`pgvector` **全部没装**。

**v1 实际做法**（`xiaopaw/memory/indexer.py:55-56`）：
```python
import psycopg2
return psycopg2.connect(db_dsn)
```
每次新建连接、用完关闭，**无连接池**。

**兼容性真相**：
- ❌ `psycopg2` 与 `psycopg_pool` **不兼容**。`psycopg_pool`（`pip install psycopg-pool`）是 psycopg3 生态的产物，依赖 `psycopg`（v3），不能包 psycopg2 连接
- ✅ 若要连接池且继续用 psycopg2：`psycopg2.pool.ThreadedConnectionPool` / `SimpleConnectionPool`（标准库子模块，零额外依赖）
- ✅ 若要异步：必须升级到 psycopg3（`pip install "psycopg[binary,pool]"`），提供原生 `AsyncConnection` + `AsyncConnectionPool`
- ✅ `pgvector` PyPI 包对 **psycopg2 和 psycopg3 都支持**（分别是 `pgvector.psycopg2.register_vector(conn)` 和 `pgvector.psycopg.register_vector(conn)`），但 import 路径不同

**v2 建议**：选一条路走通
- 路径 A（稳、改动小）：保持 psycopg2 + `psycopg2.pool.ThreadedConnectionPool` + `pgvector.psycopg2`；所有 DB 调用用 `asyncio.to_thread` 包一层，避免阻塞 event loop
- 路径 B（激进、更贴 asyncio）：升级到 `psycopg[pool]` v3，用 `AsyncConnectionPool` + `pgvector.psycopg`；v1 所有 `psycopg2.connect` 需重写

---

## 6. lark-oapi Response 对象 — ❌ 属性名不是 `raw_response`

**真实类型**（`lark_oapi/core/model/base_response.py`）：

```python
class BaseResponse:
    def __init__(self, d=None):
        self.raw: Optional[RawResponse] = None   # 注意：.raw 不是 .raw_response
        self.code: Optional[int] = None
        self.msg: Optional[str] = None
        self.error: Optional[Error] = None

    def success(self) -> bool:
        return self.code is not None and self.code == 0

    def get_log_id(self) -> Optional[str]:
        return self.raw.headers.get("X-Tt-Logid") if self.raw and self.raw.headers else None

class RawResponse:
    status_code: Optional[int]
    headers: Dict[str, str]
    content: Optional[bytes]
```

**结论**：
- ❌ `response.raw_response` **不存在**；正确属性是 `response.raw`
- ✅ `response.raw.status_code` → HTTP 状态码
- ✅ `response.raw.headers` → `Dict[str, str]`，可 `headers.get("Retry-After")`
- ✅ `response.code` / `response.msg` → 业务码 / 业务消息
- ✅ `response.success()` → 业务码是否 0
- ✅ `response.get_log_id()` → 飞书问题排查号（`X-Tt-Logid`）

**v1 `sender.py` 实际用法**（证据）：只用了 `resp.code` 和 `resp.msg`（见 `:106/131/174/200/226`），**从未访问 `raw_response`**。v2 里所有 `.raw_response` 必须改为 `.raw`。

---

## 总结：设计文档受影响位置

| 文档 | 部分 | v2 假设 | 真相 | 必须改 |
|---|---|---|---|---|
| `07-security.md` | T3 验签加固 | `ws.Client(encrypt_key=..., verification_token=...)` | ws.Client 签名只接受 app_id/app_secret/log_level/event_handler/domain/auto_reconnect | **整个 T3 重写**：WS 模式下由服务端完成鉴权；应用层只需做 event_id LRU 去重；若要 HMAC 必须改走 HTTP 回调路径 |
| `02-modules.md` / `05-concurrency.md` | SkillTool `_run` | 假设主 loop 里直调 `asyncio.run()` 会失败 | akickoff → `asyncio.to_thread(kickoff)` → `_run` 跑在 worker 线程，无 running loop，`asyncio.run` 本身可用 | 描述改为"稳妥起见用 ThreadPoolExecutor 包一层"而非"必须 nest_asyncio"；首选实现 `_arun` |
| `02-modules.md` | @before_llm_call | `context.messages = new_list` / `context.llm.context_window_size` | messages 必须 in-place 改；llm 可能是 str，无 `context_window_size` 属性 | 改为 `messages[:] = ...` 或 `messages.clear(); messages.extend(...)`；context_window 查表或 `getattr(llm, "get_context_window_size", lambda: 8192)()` |
| `04-api.md` / `06-observability.md` | Response 处理 | `response.raw_response.status_code` / `response.raw_response.headers` | 正确属性是 `response.raw.status_code` / `response.raw.headers` | 全局替换 `raw_response` → `raw` |
| `06-observability.md` | 限流 header | `response.headers["X-Lark-Request-RateLimit-Reset"]` | SDK 常量里无此 header；几乎可确定虚构 | 删除；改为读 `Retry-After`（可能有）+ 退避递增策略 |
| `07-security.md` / `06-observability.md` | 错误码白名单 | `99991663/72/71/400` 枚举 | 本次无法在 SDK 和 WebFetch 官方文档中证实；建议不绑死码值 | 改为基于 `resp.code != 0` + msg 关键词匹配（`rate limit` / `限流` / `too many` / `permission`） |
| `03-data.md` / `05-concurrency.md` | 连接池 | `psycopg_pool.AsyncConnectionPool` + psycopg2 | psycopg_pool 属 psycopg3 生态，与 psycopg2 不兼容；本机未装 | 两条路径二选一：①保 psycopg2 + `psycopg2.pool.ThreadedConnectionPool` + `asyncio.to_thread` 包；②升级 psycopg3（`psycopg[pool]`）并重写 indexer.py |
| `10-testing.md` | BaseTool 测试 | 用裸 `asyncio.run` 驱动 `_run` | 同上，测试场景下可能从已有 loop 调到，与 v1 做法一致用 ThreadPoolExecutor 更稳 | 测试工具方法保持 v1 写法（见 `tests/unit/test_skill_loader.py`） |

**总体判断**：v2 设计文档里的 SDK 假设**至少 4 项确认错误**（T3 验签、raw_response 属性、psycopg_pool、context.messages 赋值），**2 项部分错误**（限流 header 虚构、错误码未证实），**1 项表达不精确但结论对**（BaseTool async）。进入 Phase 1 编码前必须按上表修订。
