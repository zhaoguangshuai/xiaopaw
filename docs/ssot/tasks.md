# SSOT｜Task / 后台循环清单

> **Single Source of Truth**。其他文档描述 Task 时必须链接到本清单。
> 版本：v2.1 | 最后更新：2026-04-19

---

## 1. 长生命周期 Task（启动即创建，shutdown 取消）

| # | Task | 创建位置 | 取消方式 | shutdown 顺序 |
|---|---|---|---|---|
| T1 | FeishuListener WebSocket 事件循环 | `main.py` 启动时 | `listener.stop()` | 1（最先停，切断入站） |
| T2 | TestAPI aiohttp server | `main.py` 启动时（若启用） | `runner.cleanup()` → `site.stop()` | 2 |
| T3 | Runner worker（per-routing_key） | `Runner.dispatch()` 首次遇到 rk 时 lazy 创建 | `worker.cancel()` + gather | 3 |
| T4 | CronService main loop | `main.py` 启动时 | `cron_service.stop()` | 4 |
| T5 | CleanupService daily scheduler | `main.py` 启动时 | `cleanup_service.stop()` | 5 |
| T6 | metrics aiohttp server（`:8090`） | `main.py` 启动时 | `metrics_runner.cleanup()` | 6（最后停，保留观测） |

---

## 2. 短生命周期 Task（一次性 fire-and-forget）

| # | Task | 创建位置 | 托管方式 | 典型持续时间 |
|---|---|---|---|---|
| S1 | `async_index_turn` coroutine | `MemoryAwareCrew.run_and_index` 构造 coroutine → Runner 包装为 Task | `Runner._pending_index_tasks: set[Task]` + `add_done_callback(set.discard)` | 2-5s |
| S2 | `send_thinking` / `update_card` | Runner `_handle` 内 `await` | 同步等待完成（非 fire-and-forget） | <3s |
| S3 | Sub-Crew `crew.akickoff` | `SkillLoaderTool._execute_skill_async` | `asyncio.wait_for(timeout=120s)` | <120s |

---

## 3. 线程池 Task（同步任务走 executor）

| # | 任务 | 执行位置 | 原因 | 取消语义 |
|---|---|---|---|---|
| E1 | `_index_single_turn`（psycopg2 写 pgvector） | `asyncio.get_running_loop().run_in_executor(None, ...)` | psycopg2 是同步库 | **不可 cancel**（Python 线程限制） |
| E2 | `SessionManager._iter_lines_reverse_sync`（JSONL 倒序读） | `asyncio.to_thread(_collect)` | 避免阻塞事件循环 | 不可 cancel |
| E3 | tokenizer 加载（首次） | `asyncio.to_thread` | 阻塞 10-30s | 不可 cancel |
| E4 | `_summarize_chunk`（压缩摘要 LLM 调用） | `asyncio.to_thread` + CrewAI LLM wrapper | 同步 LLM 路径 | 不可 cancel |

**ContextVar 透传**：
- `asyncio.to_thread(fn, ...)` — **Python 3.9+ 自动 copy_context**（无需手工）
- `loop.run_in_executor(None, fn)` — **任何 Python 版本都不 copy_context**

若需在 `run_in_executor` 里保留 trace_id：

```python
# xiaopaw/observability/trace.py
import asyncio, contextvars
from functools import partial

async def run_in_executor_with_context(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()  # 必须在事件循环线程调
    call = partial(fn, *args, **kwargs)
    return await loop.run_in_executor(None, ctx.run, call)
```

若已用 `asyncio.to_thread`，**不需要**手动 `copy_context`。

---

## 4. shutdown 顺序

```
SIGTERM 接收
    ↓
① FeishuListener.stop()                  # 切断入站消息
    ↓
② TestAPI.cleanup()                      # 切断 TestAPI 注入
    ↓
③ Runner.shutdown()
   ├─ 取消所有 worker: worker.cancel()
   ├─ gather workers with return_exceptions=True
   └─ 等 _pending_index_tasks 最多 5s:
      await asyncio.wait_for(
          asyncio.gather(*self._pending_index_tasks, return_exceptions=True),
          timeout=5,
      )
      # 超时后 pending tasks 的 executor 线程仍可能在跑
      # 不强杀（psycopg2 线程无法 cancel）
      # 记 xiaopaw_index_task_zombie_total metric
    ↓
④ CronService.stop()                     # 停定时触发
    ↓
⑤ FeishuSender drain                     # 等正在发送的 update_card 完成
    ↓
⑥ CleanupService.stop()
    ↓
⑦ metrics server.cleanup()
    ↓
⑧ await loop.shutdown_default_executor() # 公开 API，不用 loop._default_executor
    ↓
⑨ loop.close()
```

**重要修正**（v2.1 from concurrency-verification）：
- **不用** `loop._default_executor.shutdown(wait=True)`（私有 API）
- 用 `await loop.shutdown_default_executor()`（async，Python 3.9+）
- psycopg2 同步任务 5s 超时后**仍可能占线程**，承认这个事实，用 metric 记 zombie 数，**不阻塞进程退出**

---

## 5. 取消语义矩阵

| Task 类型 | 可 cancel? | cancel 行为 |
|---|---|---|
| 纯 async coroutine | ✅ | 抛 `CancelledError`，执行 finally |
| `asyncio.wait_for` 包装的 coroutine | ✅ | 超时后 cancel 内层 |
| `run_in_executor` 同步任务 | ❌ | future 取消但线程继续跑 |
| `asyncio.to_thread` 同步任务 | ❌ | 同上 |
| CrewAI `crew.akickoff` 内部 | ⚠️ | CrewAI 1.9.x 对 cancel 不稳定；用 `wait_for` + sandbox kill 兜底 |
| Sub-Crew sandbox 进程 | 通过 MCP kill endpoint | `httpx.post(f"{sandbox_url}/mcp/session/kill")` |

---

## 6. 测试锚点

- TC-P0-5｜index_coroutine 由 Runner 托管而非 crew 自建 Task（S1）
- TC-P0-4-b｜shutdown 5s 超时后 zombie metric 递增（E1）
- TC-P1-13｜worker 异常退出 finally 清理（T3）
- TC-P1-9｜shutdown_default_executor 用公开 API（E1-E4）
