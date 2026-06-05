# 并发真相报告（Phase 0 前置验证）

> 环境：Python 3.12.3 / cachetools 7.0.5 / filelock 3.25.0（Linux）
> 方法：CPython/库源码引用 + 可执行复现脚本（`/tmp/cachetools_race.py` `/tmp/ctxvar_test.py` `/tmp/shutdown_test.py` `/tmp/lock_loop_test.py`）

## 1. cachetools.LRUCache 并发 — ❌ 驱逐+重建会产生双锁

**源码证据**（`cachetools/__init__.py:78-100, 296-298`）：`Cache.__setitem__` / `__contains__` / `LRUCache.__setitem__` 均为纯 Python dict 操作，**无 await 无锁**。同一协程内 `if sid not in c: c[sid]=Lock(); lock=c[sid]` 三行原子（asyncio 单线程不会在无 await 处切换）。

**但**：若 A 协程正 `async with lock_v1:`（await 让出），期间其他 sid 写入把 `sid_A` 从 LRU 挤掉，B 协程同 sid 再进来 → `not in cache` True → 新建 `lock_v2` → A/B 同时写同一 JSONL。

复现（`/tmp/cachetools_race.py`）：
```
sid_A still in cache? False
lock is same object? False
id(v1)=...666944, id(v2)=...667376
```

**05-concurrency.md §3.2 的"引用计数保 lock 不被 GC"只对了一半**：A 手里 v1 没 GC 没错，但 B 拿到 v2，两把锁并存，互斥被破坏。

**正确解法**：WeakValueDictionary + 全局 `_locks_dispatch_lock` 保护 check-and-create，或 LRU + dispatch_lock 组合。

## 2. to_thread vs run_in_executor 的 ContextVar — ❌ 版本说法错

**源码证据**（`asyncio/threads.py`，3.9 引入即此实现；`asyncio/base_events.py::run_in_executor`）：

```python
# to_thread：ctx = contextvars.copy_context(); run_in_executor(None, ctx.run...)
# run_in_executor：executor.submit(func, *args)  ← 任何版本都不 copy_context
```

复现 Python 3.12（`/tmp/ctxvar_test.py`）：
```
to_thread          → 'MAIN_VALUE'
run_in_executor    → 'NONE'
run_in_executor+ctx.run → 'MAIN_VALUE'
```

**真相**：① `to_thread` 自 **3.9** 起即自动 copy_context（不是文档说的 3.11+）；② `run_in_executor` **任何版本**都不 copy_context，不是 "Py ≤3.13 坑"。`run_in_executor_with_context` helper 本身写法正确，但**理由错**——必要性与 Python 版本无关，纯粹因为 `run_in_executor` 的契约。

## 3. functools.lru_cache 线程安全 — ✅ 安全，⚠️ 但并发首启会重复执行

**源码证据**（`functools.py:21, 536, 570, 584`）：用 `_thread.RLock` 保护 linkedlist；但 L583-584 `misses += 1` 后**释放锁再调 user_function**，锁外执行。N 个线程并发 miss 同 key → user_function 被调 N 次，最终 cache 只留一份。C 扩展版语义一致。

tokenizer / Skill loader 等昂贵初始化若用 `@lru_cache`，并发首启会重复。单例语义需外加 `asyncio.Lock`。

## 4. asyncio.Lock shutdown 场景 — ❌ 同步阻塞任务无法取消

**源码证据**（`asyncio/base_events.py::shutdown_default_executor`）：启 thread 调 `executor.shutdown(wait=True)`；线程无法外部 kill。

复现（`/tmp/shutdown_test.py`，0.5s 超时取消 3s `time.sleep`）：
```
[0.50s] TimeoutError raised in coroutine
[0.50s] main coroutine continues; fut.done=True
[3.00s] thread asyncio_0 job1 DONE   ← 3s 后线程才结束
```

**真相**：`gather + wait_for` 取消 → Task 收 CancelledError，但 `executor.submit` 的 Future 只有**任务未开始**时 cancel 才生效。psycopg2 execute 必须跑完。`loop._default_executor.shutdown(wait=True)` 同步阻塞 loop；`loop.shutdown_default_executor()`（3.9+ coroutine，3.12+ 支持 timeout）把等待搬到独立线程，推荐用后者。

## 5. asyncio.Lock 跨 loop — ✅ 空闲可复用，❌ 持锁/有 waiter 换 loop 会炸

**源码证据**（`asyncio/mixins.py::_LoopBoundMixin._get_loop`）：首次使用绑 `_loop`；后续若 `loop is not self._loop` → `RuntimeError: bound to a different event loop`。

模块级 `asyncio.Lock()` 在多次 `asyncio.run` 之间空闲复用 OK（实测 `/tmp/lock_loop_test.py`），但不推荐。**规范**：所有 Lock 放 `__init__` 里创建，不放模块级；测试串行 `asyncio.run` 更安全。

## 6. ReplayCache 与 time.monotonic — ⚠️ 进程重启重置，但设计实际正确

**事实**：`time.monotonic()` 是进程级单调时钟（Linux `CLOCK_MONOTONIC`），**容器/systemd 重启必然重置**。但 `ReplayCache._cache` 是进程内 LRU，进程死则 cache 空，monotonic 重置无关紧要。

**真正的语义丢失**：重启后 5 分钟 TTL 窗口内的旧 webhook 可以成功重放（cache 是空的）。飞书风险：同 event_id 跨重启超过 5 分钟重复投递概率极低，**单节点够用**，但文档必须明示：跨重启/多节点防护 → 改用 Redis `SET event_id 1 EX 300 NX`。

## 7. filelock.FileLock 跨容器 bind mount — ✅ 同一宿主机 inode 生效

**源码证据**（`filelock/_unix.py`）：`UnixFileLock` 默认 `fcntl.flock`。flock 按内核 inode 协调，同一宿主机两容器 bind mount 同一文件 → 同一 inode → 互斥生效。`SoftFileLock` 退化到 pathlib.exists 轮询，弱，不用。

**注意**：NFS、CIFS、overlayfs 内部路径的 flock 历史上有坑；bind mount + 本地 FS OK。单节点无此问题。

---

## 总结：设计文档受影响的位置

| 文档 | 部分 | v2 假设 | 真相 | 必须改 |
|---|---|---|---|---|
| 01-architecture.md | ADR-002 | LRUCache(1000) 比 WeakValueDictionary 安全 | LRU 驱逐+重建会双锁；WeakValueDict + dispatch_lock 才对 | ✅ 重写 |
| 02-modules.md | SessionManager.append §275-286 | "sid 是 UUID 所以不冲突" | 同 sid 被驱逐后二次访问会撞双锁 | ✅ dispatch_lock 模式 |
| 03-data.md | §13.4 | 同上 | 同上 | ✅ |
| 05-concurrency.md | §3.1-3.2 | "驱逐时不失效互斥"（只说 lock 不被 GC） | 没覆盖双锁并存问题 | ✅ 重写 §3.2 |
| 06-observability.md | §2.3 L104 | "Py ≤3.13 copy_context 在 executor 里看不到" | 任何版本 run_in_executor 都不 copy_context | ✅ 改理由 |
| 06-observability.md | L140 | "to_thread 3.11+ 自动处理" | 3.9+ 即自动 | ✅ 改版本 |
| 07-security.md | ReplayCache | 用 monotonic 做 TTL | 进程级；跨重启防护丢失 | ⚠️ 补限制说明；敏感场景 Redis |
| 08-deployment.md | shutdown | gather+wait_for 即可 graceful | 同步线程任务不可取消 | ✅ 补 grace period |
| 08-deployment.md | Cron filelock | 默认多进程安全 | 需 bind mount 本地 FS；NFS 不可靠 | ⚠️ 补前提 |
| 10-testing.md | test_session_lock_mutex | 100 协程并发即覆盖 | 必须加 LRU 驱逐后重入场景 | ✅ 新增 `_after_eviction` 用例 |
| 02-modules.md | 若 Skill/tokenizer 用 @lru_cache | 单例 | 并发首启重复执行 | ⚠️ 外加 asyncio.Lock |

**Phase 0 前置结论**：动工前先修上表 ✅ 条目（LRUCache 双锁、ContextVar 版本说法、shutdown grace、测试补强），⚠️ 条目可文档补说明后推进。
