# SSOT｜端口清单

> **Single Source of Truth**。所有文档引用端口时必须链接到本清单。
> 版本：v2.1 | 最后更新：2026-04-19

---

## 1. 端口总表

| 端口 | 服务 | 暴露级别 | 鉴权 | 环境 | 备注 |
|---|---|---|---|---|---|
| **8090** | xiaopaw `/metrics` + `/health` | 宿主 | `/metrics`: Bearer Token；`/health`: 无 | 所有 | aiohttp 同 Application 路由分发 |
| **9090** | TestAPI `/api/test/*` | loopback only（`127.0.0.1:9090`） | Bearer Token + loopback bind | dev only | prod 强制禁用 |
| **8080** | AIO-Sandbox MCP | docker network 内部（`xiaopaw-net`），**不对宿主暴露** | 无（内网） | 所有 | **不得映射 host ports** |
| **5432** | pgvector PostgreSQL | docker network 内部 | user+password+TLS | 所有 | 不对宿主暴露（仅容器间） |

---

## 2. aiohttp 端口内的路由

### 8090（xiaopaw 主进程观测服务）

```python
app = web.Application()
# /health 无鉴权（容器 healthcheck 需要）
app.router.add_get("/health", health_handler)
# /metrics 走 Bearer middleware（仅作用于 /metrics）
metrics_app = web.Application(middlewares=[bearer_middleware])
metrics_app.router.add_get("/metrics", metrics_handler)
app.add_subapp("/", metrics_app)
```

**Dockerfile**：`EXPOSE 8090`，`HEALTHCHECK CMD curl -f http://localhost:8090/health`。

### 9090（TestAPI，仅 dev）

```python
# xiaopaw/api/test_server.py
async def start_test_api(runner, cfg):
    if cfg.debug.enable_test_api is False:
        return
    if cfg.debug.test_api_host not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError(f"TestAPI 必须 loopback: {cfg.debug.test_api_host}")
    if os.getenv("XIAOPAW_ENV") == "prod":
        raise RuntimeError("prod 禁用 TestAPI")

    app = web.Application(middlewares=[bearer_middleware])
    app.router.add_post("/api/test/message", message_handler)
    app.router.add_post("/api/test/clear", clear_handler)
    runner_obj = web.AppRunner(app)
    await runner_obj.setup()
    site = web.TCPSite(runner_obj, cfg.debug.test_api_host, 9090)
    await site.start()
```

---

## 3. Docker Compose 端口映射

### 生产（prod）

```yaml
services:
  xiaopaw:
    ports:
      - "8090:8090"            # 仅 metrics + health
    # 不暴露 9090（TestAPI）

  pgvector:
    # 无 ports: 节，仅内网
    networks: [xiaopaw-net]

  aio-sandbox:
    # 无 ports: 节，仅内网（v2 T9 防御）
    networks: [xiaopaw-net]
```

### 开发（dev）

```yaml
services:
  xiaopaw:
    ports:
      - "8090:8090"            # metrics + health
      - "127.0.0.1:9090:9090"  # TestAPI，显式 loopback，防止 0.0.0.0 误绑
```

---

## 4. 客户端侧端口引用

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `observability.metrics_port` | 8090 | **同一个端口**给 /metrics 和 /health |
| `observability.health_port` | **删除此字段**（与 metrics_port 同一） | — |
| `debug.test_api_port` | 9090 | TestAPI |
| `debug.test_api_host` | `127.0.0.1` | loopback only |
| `sandbox.url` | `http://aio-sandbox:8080/mcp` | 容器间地址（Docker DNS），端口是容器内 8080 |
| `memory.db_dsn` | `postgresql://xiaopaw_app:xxx@pgvector:5432/xiaopaw_memory` | 容器间地址 |

---

## 5. v2.1 修正点（from review）

| 原 v2.0 | v2.1 修正 |
|---|---|
| `observability.health_port: 9091` + `metrics_port: 9091`（注释"同端口"） | 删 `health_port`；统一 `metrics_port: 8090` |
| `sandbox.url: http://aio-sandbox:8022/mcp` | `http://aio-sandbox:8080/mcp`（8022 是 v1 host 映射，容器间应用 8080） |
| `/metrics` 端口 9091 | 改 8090 |
| dev compose `ports: ["9090:9090"]` | 改 `"127.0.0.1:9090:9090"` 显式 loopback |
| aio-sandbox 在 prod compose 里可能有 `ports:` 节 | 强制删除，仅 internal network |

---

## 6. 测试锚点

- TC-P1-3｜`/health` 200 + git_sha 非空
- TC-P1-3｜`/metrics` 无 Bearer 401
- TC-P1-4｜dev TestAPI 只 listen loopback
- TC-P1-7｜aio-sandbox 容器无 host ports 映射（docker compose config 扫描）
- TC-P2-0｜pgvector 容器无 host ports 映射
