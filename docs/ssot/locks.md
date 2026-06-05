# SSOT｜锁清单

> **Single Source of Truth**。其他文档描述锁时必须链接到本清单，不得硬编码。
> 版本：v2.1 | 最后更新：2026-04-19

---

## 1. 进程内锁（单节点 asyncio 保护）

| # | 锁 | 类型 | 保护资源 | 粒度 | 持有者 | 典型持有时间 | 失败降级 |
|---|---|---|---|---|---|---|---|
| L1 | `Runner._dispatch_lock` | `asyncio.Lock` | `_queues` / `_workers` / `_queue_gen` / `_jsonl_locks`（双层锁的第一级） | 全局 | Runner | <1ms | 阻塞等待 |
| L2 | `SessionManager._index_lock` | `asyncio.Lock` | `index.json` 读写 | 全局 | SessionManager | <5ms | 阻塞等待 |
| L3 | `SessionManager._jsonl_locks[sid]` | `asyncio.Lock`（LRUCache 存储） | `{sid}.jsonl` append | per-session | SessionManager | <10ms（含 fsync） | 阻塞等待 |
| L4 | `FeishuSender._sem` | `asyncio.Semaphore(5)` | 出站飞书 API 并发 | 全局 | FeishuSender | <3s | 排队等信号量 |
| L5 | `ReplayCache._lock`（内部） | `asyncio.Lock` | event_id LRU 缓存的 put/check | 全局 | FeishuListener | <1ms | 阻塞 |

**L3 正确获取模式**（v2.1 定稿）：

```python
# 必须走 _dispatch_lock 的两级锁，避免 LRUCache 驱逐 + 并发 getter 竞态
async def append(self, sid, *, user, feishu_msg_id, assistant):
    async with self._dispatch_lock:  # L1：短暂保护 LRUCache setdefault
        if sid not in self._jsonl_locks:
            self._jsonl_locks[sid] = asyncio.Lock()
        lock = self._jsonl_locks[sid]
    async with lock:  # L3：真正的 per-session 互斥
        ... 写 JSONL ...
```

**为什么两级锁**：
- `_dispatch_lock` 保证 "check + create + get" 三步原子
- LRUCache 驱逐后再次访问同 sid 时，若 A 协程仍持有旧锁（闭包引用），B 协程在 L1 保护下 `setdefault` 会**拿到已存在的旧锁**（cache 已驱逐但闭包保持对象存活，但 cache 里不在）；**等等** —— 实际驱逐后 cache 里没有，`sid not in cache` 为 True，会创建新锁。所以**必须假设驱逐后新旧锁并存**。
- 因此 LRUCache `maxsize=1000` 必须 > 峰值 active session，**活跃 session 数超限是运维告警**，而不是正确性依赖。

---

## 2. 跨进程锁（filelock）

| # | 锁 | 类型 | 保护资源 | 持有者 | 超时 | 失败降级 |
|---|---|---|---|---|---|---|
| F1 | `CronStorage._lock` | `filelock.FileLock` | `data/cron/tasks.json` 读写 | CronService / scheduler_mgr Skill（两个进程） | 10s | 捕获 Timeout → retry 1 次 → metric 告警 |
| F2 | `memory-save` topic lock | `filelock.FileLock` | `data/workspace/{topic}.md`（user/memory/agent） | memory-save Skill（Sub-Crew 进程） | 10s | 捕获 Timeout → errcode=408 返回 |

**锁文件命名**：`{protected_file}.lock`（加入 .gitignore）。

**默认 backend**：`fcntl.flock`（Linux）。Windows 不支持（单节点 v2 只支持 Linux 部署）。

---

## 3. 函数级原子性（Python 运行时保证）

| # | 机制 | 用途 |
|---|---|---|
| A1 | `functools.cache` / `lru_cache` 装饰器 | LLM client 单例（`indexer._get_llm_client`）。CPython 内置 RLock 保证并发首次调用最终收敛到一个实例。 |
| A2 | CPython GIL | 字节码级原子操作（不依赖，仅作为最后兜底） |
| A3 | `contextvars.ContextVar` + `ContextVar.set(token) / reset(token)` | trace_id 传播，Token 机制保证 finally 能 reset 到正确值 |

---

## 4. Task 协作（非锁，但影响并发正确性）

- Runner 的 `_pending_index_tasks: set[asyncio.Task]` 不是锁，是 task 托管集合，用于 shutdown 优雅等待。见 [tasks.md](tasks.md)。

---

## 5. v2 vs v1 差异

| 锁 | v1 | v2 |
|---|---|---|
| L3 `_jsonl_locks` | 无界 `dict`（OOM） | `LRUCache(1000)` + L1 保护的两级取锁 |
| L4 FeishuSender 并发 | 无 | `Semaphore(5)` |
| L5 ReplayCache | 无 | 新增 |
| F1 Cron | 单进程原子（实际跨进程） | `filelock` 跨进程 + DLQ |
| F2 memory-save | 无锁（覆盖风险） | topic 粒度 `filelock` |

---

## 6. 测试锚点（见 test-cases-for-known-risks.md）

- TC-P0-4-a/b｜LRUCache 驱逐 + 并发 append 互斥（对应 L1+L3）
- TC-P1-6-a｜Cron 跨进程锁（对应 F1）
- TC-P0-4-c｜memory-save topic 锁（对应 F2）
- TC-P2-7-a｜FeishuSender Semaphore 限流（对应 L4）
- TC-P0-1-b｜ReplayCache 重放拒绝（对应 L5）
