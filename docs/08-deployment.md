# 08 - 部署设计

> **适用范围**：xiaopaw-v2 单节点生产/灰度/开发三种部署形态。
> **定位**：本文件覆盖部署形态、Docker Compose / Dockerfile、凭证分层、启动依赖、健康检查、升级回滚、持久化备份、日志监控接入、容器硬化、资源预算、故障演练。
> **前置约束**：v2 以**单节点**为硬前提，多节点属 M4 阶段。
> **相关文档**：
> - [DESIGN.md §12](../DESIGN.md) — 部署与运维总览
> - [01-architecture.md §5](./01-architecture.md) — 进程/组件部署视图
> - [07-security.md §12](./07-security.md) — 容器硬化与威胁模型
> - [06-observability.md](./06-observability.md) — 指标/告警规则
> - [phase0-checklist.md](./phase0-checklist.md) — Phase 0 就绪清单

---

## 目录

- [1. 部署形态](#1-部署形态)
- [2. Docker Compose 完整方案](#2-docker-compose-完整方案)
- [3. Dockerfile](#3-dockerfile)
- [4. 环境变量与凭证分层](#4-环境变量与凭证分层)
- [5. 启动顺序与依赖](#5-启动顺序与依赖)
- [6. Healthcheck 设计](#6-healthcheck-设计)
- [7. 升级流程](#7-升级流程)
- [8. Phase 0 Checklist](#8-phase-0-checklist)
- [9. 数据持久化与备份](#9-数据持久化与备份)
- [10. 日志与监控接入](#10-日志与监控接入)
- [11. 容器硬化](#11-容器硬化)
- [12. 资源预算](#12-资源预算)
- [13. 故障演练清单](#13-故障演练清单)
- [14. 上线 Checklist](#14-上线-checklist)

---

## 1. 部署形态

v2 支持三种环境：**dev / canary / prod**。差异通过 `XIAOPAW_ENV` 环境变量与 `config.{env}.yaml` 两个维度控制。

### 1.1 形态对照表

| 维度 | dev | canary | prod |
|------|-----|--------|------|
| `XIAOPAW_ENV` | `dev` | `canary` | `prod` |
| 启动方式 | `docker compose -f compose.dev.yaml up` | `docker compose -f compose.prod.yaml up`（独立 host） | `docker compose -f compose.prod.yaml up`（生产 host） |
| TestAPI (`:9090`) | 开启，bind `127.0.0.1` | 开启（仅接入演练） | **关闭**（`assert_production_safe` 断言） |
| `/metrics` Bearer | 弱 token 可接受（32 字符下限仍生效） | 生产级 token | 生产级 token，强制 ≥32 字符 |
| `/health` | 开放（8090） | 开放（8090） | 开放（8090，仅返回 `200 + git_sha`） |
| pgvector 实例 | 本地 compose 容器 | 独立 canary 库（数据与 prod 隔离） | 独立实例 |
| aio-sandbox | 本地 compose 容器 | 独立 canary 实例 | 独立实例 |
| 流量来源 | 开发者手工 / 回归用例 | 0% 真实流量；pytest-memray 长跑 + 合成负载 | 100% 真实流量 |
| 日志级别 | `DEBUG` | `INFO` | `INFO`（含 trace_id） |
| Feature Flags | 全开（含 T1 `enable_mcp_whitelist` 可关以便调试） | 与 prod 一致 | 见 [DESIGN.md §13](../DESIGN.md) 默认值 |
| 供应链钉版本 | 允许 `image:tag`（提速迭代） | **强制** `image:tag@sha256:<digest>` | **强制** `image:tag@sha256:<digest>` |
| `.env` mode | 0600（协作方便） | 0400 | 0400 |
| 镜像来源 | 本地 `docker build` | 私有 registry（tag + digest） | 私有 registry（tag + digest） |

### 1.2 三形态共享约束

- **进程树**：无论哪个形态，xiaopaw 主进程、pgvector、aio-sandbox 三个服务都通过同一份 docker compose 定义，仅用 `.env` 与 `compose.*.override.yaml` 参数化。
- **数据目录**：bind mount 路径一致（见 §9），便于 dev 复盘 prod 故障。
- **端口契约**（SSOT：[`ssot/ports.md`](./ssot/ports.md)）：
  - `8090` → xiaopaw `/health` + `/metrics`（同一 aiohttp Application，子路由分发；`/health` 无鉴权，`/metrics` 走 Bearer）
  - `9090` → TestAPI（仅 dev，`127.0.0.1` loopback；prod 禁用）
  - `5432` → pgvector（仅 `xiaopaw-net` 容器间；**不对宿主暴露**）
  - `8080` → aio-sandbox（仅 `xiaopaw-net` 容器间；**强制不对宿主暴露**，见 T9）
- **Image digest**：canary 与 prod 必须等值；一旦 canary 通过 72h baseline，prod 可直接沿用。

### 1.3 非目标

- 不支持多副本（`replicas > 1`）。原因：`_dispatch_lock`、workspace 文件锁、cron filelock 均为进程内/单文件语义（见 [05-concurrency.md](./05-concurrency.md)）。
- 不支持 Kubernetes StatefulSet（M4 再议）。
- 不支持跨可用区部署。

---

## 2. Docker Compose 完整方案

### 2.1 目录布局

```
xiaopaw-v2/
├── Dockerfile
├── .dockerignore
├── docker/
│   ├── compose.base.yaml            # 所有环境共用
│   ├── compose.dev.override.yaml    # dev 差异
│   ├── compose.prod.override.yaml   # canary / prod 差异
│   ├── seccomp/
│   │   ├── xiaopaw-profile.json
│   │   └── sandbox-profile.json
│   ├── initdb/
│   │   └── 01-schema.sh             # 见 §9.2（shell wrapper，非 .sql）
│   └── secrets/                     # 仅 prod，mode 0400，uid/gid 65534
│       ├── pg_root_password.txt
│       ├── pg_app_password.txt
│       ├── metrics_bearer_token.txt
│       ├── testapi_token.txt
│       ├── qwen_api_key.txt
│       ├── memory_db_dsn.txt
│       ├── xiaopaw_app_dsn.txt
│       ├── feishu_app_id.txt        # v2.1：从 env 迁到 secrets
│       ├── feishu_app_secret.txt    # v2.1：从 env 迁到 secrets
│       ├── feishu_verification_token.txt   # 备用 HTTP 回调路径
│       └── feishu_encrypt_key.txt          # 备用 HTTP 回调路径
├── .env.example                     # 入库，无值
├── .env                             # 不入库，mode 0400
├── config.yaml                      # 不入库
├── config.yaml.example              # 入库
└── data/                            # bind mount 根
    ├── workspace/                   # Agent 产出（含 sessions/{sid}/）
    ├── sessions/                    # session metadata
    ├── ctx/                         # ContextCache 落盘
    ├── traces/                      # 巡检稿
    ├── cron/                        # tasks.json（filelock 保护）
    └── pgvector/                    # PG 数据（由 Docker volume 管理）
```

### 2.2 `compose.base.yaml`

```yaml
# docker/compose.base.yaml
# 三环境共用。环境差异通过 .env + override 文件叠加。

name: xiaopaw

x-common-labels: &common-labels
  com.xiaopaw.version: "${XIAOPAW_VERSION:-v2.0.0}"
  com.xiaopaw.git_sha: "${GIT_SHA:-unknown}"

services:
  pgvector:
    image: pgvector/pgvector:pg16@sha256:${PGVECTOR_DIGEST}
    container_name: xiaopaw-pgvector
    labels: *common-labels
    environment:
      POSTGRES_DB: xiaopaw_db
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_root_password
      POSTGRES_INITDB_ARGS: "--data-checksums"
      # 传入 xiaopaw_app 密码文件路径，供 initdb shell wrapper 用 $(cat ...) 读取
      # 关键：不能用 psql `\set`（不展开环境变量）；必须 shell wrapper
      XIAOPAW_APP_DB_PASSWORD_FILE: /run/secrets/pg_app_password
    secrets:
      - pg_root_password
      - pg_app_password
    volumes:
      - pgvector-data:/var/lib/postgresql/data
      - ./docker/initdb:/docker-entrypoint-initdb.d:ro
    networks:
      - xiaopaw-net
    # 不对宿主暴露 5432（仅 xiaopaw-net 容器间）。dev override 可开 loopback。
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    read_only: false   # PG 需要写 WAL，不能 read_only
    tmpfs:
      - /tmp
      - /var/run/postgresql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d xiaopaw_db"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 20s
    # 资源约束（见 §12）
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 2G }
        reservations: { cpus: "0.5", memory: 512M }

  aio-sandbox:
    image: ghcr.io/agent-infra/sandbox:${SANDBOX_TAG}@sha256:${SANDBOX_DIGEST}
    container_name: xiaopaw-sandbox
    labels: *common-labels
    # ⚠ T9 关键防御：**强制无 `ports:` 节**。容器内 8080 只通过 xiaopaw-net
    # 由 xiaopaw 容器以 `http://aio-sandbox:8080/mcp` 访问；不对宿主暴露任何端口。
    environment:
      # 凭证经 env 注入沙盒内部，不经过 LLM
      QWEN_API_KEY_FILE: /run/secrets/qwen_api_key
      MEMORY_DB_DSN_FILE: /run/secrets/memory_db_dsn
      SANDBOX_LOG_LEVEL: "INFO"
    secrets:
      - qwen_api_key
      - memory_db_dsn
    volumes:
      # skills 读写：skill-creator 可在沙盒内新增 Skill
      - ./xiaopaw/skills:/mnt/skills:rw
      # v2.1：挂整个 data/workspace（含 .config 子目录），CleanupService
      # 启动期会向 .config/feishu.json / .config/baidu.json 重写凭证。
      # cron 子目录同一 mount 下可见（v1 兼容路径）。
      - ./data/workspace:/workspace:rw
    networks:
      - xiaopaw-net
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
      - "seccomp:./docker/seccomp/sandbox-profile.json"
    cap_drop:
      - ALL
    # sandbox 需要以 root 启动 python/node 子进程，不加 USER
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:8080/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 15s
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 2G }
        reservations: { cpus: "0.5", memory: 512M }

  xiaopaw:
    image: ${XIAOPAW_IMAGE}@sha256:${XIAOPAW_DIGEST}
    container_name: xiaopaw-app
    labels: *common-labels
    build:
      context: ..
      dockerfile: Dockerfile
      args:
        GIT_SHA: "${GIT_SHA:-unknown}"
        BUILD_DATE: "${BUILD_DATE}"
        XIAOPAW_VERSION: "${XIAOPAW_VERSION:-v2.0.0}"
    env_file:
      - ../.env
    environment:
      XIAOPAW_ENV: "${XIAOPAW_ENV:-prod}"
      # 容器间 URL：aio-sandbox 容器内部监听 8080（非宿主机 8022）
      XIAOPAW_SANDBOX_URL: "http://aio-sandbox:8080/mcp"
      XIAOPAW_PG_DSN_FILE: /run/secrets/xiaopaw_app_dsn
      XIAOPAW_METRICS_TOKEN_FILE: /run/secrets/metrics_bearer_token
      XIAOPAW_TESTAPI_TOKEN_FILE: /run/secrets/testapi_token
      # v2.1：飞书四件套改从 secrets 读，不走 env_file
      FEISHU_APP_ID_FILE: /run/secrets/feishu_app_id
      FEISHU_APP_SECRET_FILE: /run/secrets/feishu_app_secret
      # 备用 HTTP 回调路径才需要；WS 模式下 SDK 不消费（见 sdk-verification-report §1）
      FEISHU_VERIFICATION_TOKEN_FILE: /run/secrets/feishu_verification_token
      FEISHU_ENCRYPT_KEY_FILE: /run/secrets/feishu_encrypt_key
    secrets:
      - xiaopaw_app_dsn
      - metrics_bearer_token
      - testapi_token
      - feishu_app_id
      - feishu_app_secret
      - feishu_verification_token
      - feishu_encrypt_key
    volumes:
      - ./data/workspace:/app/data/workspace:rw
      - ./data/sessions:/app/data/sessions:rw
      - ./data/ctx:/app/data/ctx:rw
      - ./data/traces:/app/data/traces:rw
      - ./data/cron:/app/data/cron:rw
      - ../config.yaml:/app/config.yaml:ro
    ports:
      # 统一端口：/health + /metrics 同端口 8090（aiohttp 同 Application 分路由）
      # TestAPI (9090) 仅 dev override 打开。prod 不暴露。
      - "8090:8090"
    networks:
      - xiaopaw-net
    restart: unless-stopped
    stop_grace_period: 30s
    stop_signal: SIGTERM
    security_opt:
      - no-new-privileges:true
      - "seccomp:./docker/seccomp/xiaopaw-profile.json"
    cap_drop:
      - ALL
    read_only: true
    tmpfs:
      - /tmp:size=64m
      - /app/.cache:size=128m
    user: "65534:65534"   # 数字 uid/gid，比 nobody 名称更可移植
    depends_on:
      pgvector:
        condition: service_healthy
      aio-sandbox:
        condition: service_healthy
    healthcheck:
      # /health 独立 handler，无 Bearer；不查下游（避免级联重启）
      test: ["CMD-SHELL", "curl -fsS http://localhost:8090/health || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s   # v2.1：aiohttp + tokenizer 惰性加载冷启动更宽松
    deploy:
      resources:
        limits: { cpus: "4.0", memory: 4G }
        reservations: { cpus: "1.0", memory: 1G }

networks:
  xiaopaw-net:
    driver: bridge
    internal: false   # xiaopaw 需出站访问飞书 / DashScope
    driver_opts:
      com.docker.network.bridge.name: br-xiaopaw

volumes:
  pgvector-data:
    driver: local

secrets:
  # v2.1：所有 secret 加 uid/gid/mode，确保容器内挂到 /run/secrets/<name> 时
  # owner=nobody(65534)、mode=0400，与 xiaopaw USER 65534:65534 一致
  pg_root_password:
    file: ./docker/secrets/pg_root_password.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  pg_app_password:
    file: ./docker/secrets/pg_app_password.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  xiaopaw_app_dsn:
    file: ./docker/secrets/xiaopaw_app_dsn.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  metrics_bearer_token:
    file: ./docker/secrets/metrics_bearer_token.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  testapi_token:
    file: ./docker/secrets/testapi_token.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  qwen_api_key:
    file: ./docker/secrets/qwen_api_key.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  memory_db_dsn:
    file: ./docker/secrets/memory_db_dsn.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  # 飞书四件套：v2.1 从 environment/env_file 统一迁到 secrets
  feishu_app_id:
    file: ./docker/secrets/feishu_app_id.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  feishu_app_secret:
    file: ./docker/secrets/feishu_app_secret.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  feishu_verification_token:
    # 备用：仅 HTTP 回调路径消费；WS 模式下留空占位即可（见 sdk-verification-report §1）
    file: ./docker/secrets/feishu_verification_token.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
  feishu_encrypt_key:
    # 同上
    file: ./docker/secrets/feishu_encrypt_key.txt
    uid: "65534"
    gid: "65534"
    mode: 0400
```

### 2.3 `compose.dev.override.yaml`

```yaml
# docker/compose.dev.override.yaml
services:
  xiaopaw:
    image: xiaopaw:dev           # dev 允许用 tag，不钉 digest
    build: { context: .. }
    environment:
      XIAOPAW_ENV: "dev"
      XIAOPAW_LOG_LEVEL: "DEBUG"
    # dev 放宽：允许 root，便于在容器内 attach pdb
    user: "0:0"
    read_only: false
    # dev 下 /metrics+/health 走 8090；TestAPI 9090 **显式 loopback**
    ports: !override
      - "8090:8090"
      - "127.0.0.1:9090:9090"    # 显式 bind 127.0.0.1，防止 0.0.0.0 误绑
  pgvector:
    ports:
      - "127.0.0.1:5432:5432"    # dev 直连 psql（仅 loopback）
```

### 2.4 `compose.prod.override.yaml`

```yaml
# docker/compose.prod.override.yaml
services:
  xiaopaw:
    environment:
      XIAOPAW_ENV: "${XIAOPAW_ENV}"   # prod 或 canary
      XIAOPAW_LOG_LEVEL: "INFO"
    # prod 只暴露 8090（/health + /metrics 同端口）；TestAPI 9090 **不映射**
    # Prometheus scrape 走宿主机回环（或内网）的 8090/metrics，带 Bearer
    ports: !override
      - "8090:8090"
  pgvector: {}          # prod 继承 base 的无 ports 策略（容器间访问）
  aio-sandbox: {}       # prod 继承 base 的无 ports 策略（T9）
```

### 2.5 启动命令

```bash
# dev
docker compose \
  -f docker/compose.base.yaml \
  -f docker/compose.dev.override.yaml \
  --env-file .env.dev up -d

# canary
XIAOPAW_ENV=canary docker compose \
  -f docker/compose.base.yaml \
  -f docker/compose.prod.override.yaml \
  --env-file .env.canary up -d

# prod
XIAOPAW_ENV=prod docker compose \
  -f docker/compose.base.yaml \
  -f docker/compose.prod.override.yaml \
  --env-file .env.prod up -d
```

---

## 3. Dockerfile

### 3.1 设计要点

- **Multi-stage**：builder 阶段装依赖（含编译工具链），runtime 阶段仅保留 `python:3.11-slim` + `/deps`，体积压到 ~220MB。
- **`GIT_SHA` / `BUILD_DATE` / `XIAOPAW_VERSION` ARG 注入**：CI 构建时传入，通过 `LABEL` 固化到镜像，再由 `/health` 返回给外部。
- **`USER 65534:65534`（数字 uid/gid）**：runtime 阶段完全以非 root 运行，与 compose 的 `user: "65534:65534"` 二次保险。**用数字 uid 而非 `nobody` 名称**——不同发行版 `nobody` 可能对应不同 uid，数字更可移植。
- **`HEALTHCHECK`**：内置 `/health` 探活（端口 8090），作为 docker compose `service_healthy` 的兜底；`start_period=30s`，放宽冷启动期。
- **最小依赖**：builder 装 `build-essential` / `libpq-dev`，runtime 仅装 `libpq5` + `ca-certificates` + `curl`（healthcheck 用）。
- **不 `COPY .env`、不 `COPY config.yaml`**：靠 compose volume / secrets 运行时注入（见 §4）。
- **`.dockerignore` 严格**：排除 `.env*`、`data/`、`.git/`、`tests/`、`study/`、`memory/`。

### 3.2 Dockerfile 完整示例

```dockerfile
# syntax=docker/dockerfile:1.7
# Dockerfile

# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

# 编译时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 仅复制 requirements.txt 触发 layer 缓存
COPY requirements.txt requirements.lock ./

# 所有依赖打到独立目录，避免污染 runtime
RUN pip install --no-cache-dir --upgrade pip==24.2 \
    && pip install --no-cache-dir --target=/deps --require-hashes -r requirements.lock

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

ARG GIT_SHA=unknown
ARG BUILD_DATE
ARG XIAOPAW_VERSION=v2.0.0

# 运行时依赖（libpq5 for psycopg2, ca-certificates for https, curl for healthcheck）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
        curl \
        tini \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# OCI 标签（被 /health 读取、被 Prometheus `xiaopaw_build_info` gauge 读取）
LABEL org.opencontainers.image.title="xiaopaw" \
      org.opencontainers.image.version="${XIAOPAW_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/xiaopaw/xiaopaw-v2" \
      org.opencontainers.image.licenses="MIT"

# 环境变量：GIT_SHA 在进程内可读，/health 返回
ENV GIT_SHA=${GIT_SHA} \
    XIAOPAW_VERSION=${XIAOPAW_VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/deps \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 仅复制源代码（不复制 .env / config.yaml / tests / docs）
COPY --chown=nobody:nogroup xiaopaw/ ./xiaopaw/
COPY --chown=nobody:nogroup workspace-init/ ./workspace-init/
COPY --chown=nobody:nogroup scripts/entrypoint.sh ./scripts/entrypoint.sh

# 依赖目录（从 builder）
COPY --from=builder /deps /deps

# 预建数据目录挂载点（供 VOLUME 使用；真实数据由 compose bind mount）
RUN mkdir -p /app/data && chown -R nobody:nogroup /app /deps && chmod +x /app/scripts/entrypoint.sh

# 非 root 用户（数字 uid/gid，比 nobody 名称更可移植）
USER 65534:65534

VOLUME ["/app/data"]

# 仅 8090（/health + /metrics 同端口）；9090 TestAPI 仅 dev compose 暴露
EXPOSE 8090

# 容器级 healthcheck（compose 层已有同名设置，这里作为单独 docker run 的兜底）
# v2.1：start_period 从 20s 调到 30s（aiohttp + tokenizer 惰性加载更宽松）；curl 更简洁
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8090/health || exit 1

# tini 作为 PID 1：回收 zombie、转发 SIGTERM / SIGHUP
ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/entrypoint.sh"]
CMD ["python", "-m", "xiaopaw.main"]
```

### 3.3 entrypoint.sh

```bash
#!/usr/bin/env sh
# scripts/entrypoint.sh
# 职责：secret 文件 → env（FILE 后缀约定）、SIGHUP 预热、启动主进程。
# v2.1：补飞书四件套（APP_ID / APP_SECRET / VERIFICATION_TOKEN / ENCRYPT_KEY），
# 统一从 /run/secrets/<name> 读，不走 .env。
set -eu

for var in \
    XIAOPAW_PG_DSN \
    XIAOPAW_METRICS_TOKEN \
    XIAOPAW_TESTAPI_TOKEN \
    FEISHU_APP_ID \
    FEISHU_APP_SECRET \
    FEISHU_VERIFICATION_TOKEN \
    FEISHU_ENCRYPT_KEY; do
    file_var="${var}_FILE"
    eval "file_path=\${${file_var}:-}"
    if [ -n "${file_path}" ] && [ -r "${file_path}" ]; then
        eval "export ${var}=\"$(cat "${file_path}")\""
    fi
done

# 交还给 tini 转发信号
exec "$@"
```

### 3.4 `.dockerignore`

```
.git/
.github/
data/
tests/
study/
memory/
multi-agent/
.env*
*.log
*.pyc
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
docs/
```

---

## 4. 环境变量与凭证分层

### 4.1 分层模型

```
┌─────────────────────────────────────────────────────────┐
│ L0  .env.example（入库，仅 key 名，无值）                │
├─────────────────────────────────────────────────────────┤
│ L1  .env（本机 dev；不入库；mode 0400）                  │
├─────────────────────────────────────────────────────────┤
│ L2  docker secrets files（canary/prod；mode 0400）       │
│     位于 ./docker/secrets/*.txt，由 secret manager 同步  │
├─────────────────────────────────────────────────────────┤
│ L3  External Secret Manager（Vault / 阿里云 KMS / SOPS）│
│     是 L1/L2 的真实来源；CI 用 sops decrypt 写入        │
├─────────────────────────────────────────────────────────┤
│ L4  Kubernetes Secret（M4 预留，当前不启用）            │
└─────────────────────────────────────────────────────────┘
```

### 4.2 变量清单

| 变量名 | 层级 | 示例值 | 说明 |
|-------|------|-------|------|
| `XIAOPAW_ENV` | L1 | `dev` / `canary` / `prod` | 触发 `assert_production_safe` |
| `GIT_SHA` | L1（CI 注入） | `a1b2c3d` | 进入 `/health` / metrics 的 `build_info` |
| `XIAOPAW_VERSION` | L1 | `v2.0.0` | 同上 |
| `PGVECTOR_DIGEST` | L1 | `sha256:...` | 钉 pgvector 镜像 |
| `SANDBOX_DIGEST` | L1 | `sha256:...` | 钉 sandbox 镜像 |
| `XIAOPAW_DIGEST` | L1 | `sha256:...` | 钉主镜像（CI 构建后写回） |
| `XIAOPAW_IMAGE` | L1 | `registry.example/xiaopaw:v2.0.0` | 镜像地址 |
| `XIAOPAW_PG_DSN` | L2 | `postgresql://xiaopaw_app:***@pgvector:5432/xiaopaw_db` | 应用连接串（最小权限，见 [07-security.md §13](./07-security.md)） |
| `XIAOPAW_METRICS_TOKEN` | L2 | ≥32 字符 base64 | `/metrics` Bearer |
| `XIAOPAW_TESTAPI_TOKEN` | L2 | ≥32 字符 base64 | TestAPI Bearer（prod 不用） |
| `QWEN_API_KEY` | L2 | `sk-...` | 沙盒内注入，不经过 LLM |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | **L2**（v2.1） | — | 飞书凭证；**v2.1 从 env_file 统一迁到 docker secrets**，entrypoint.sh 从 `/run/secrets/feishu_*` 读 |
| `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` | L2 | — | **备用字段**：WS 模式 SDK 不消费（见 [sdk-verification-report.md §1](./sdk-verification-report.md)）；仅启用 HTTP 回调路径时生效 |

### 4.3 注入方式

- **L1（.env）**：compose `env_file: ../.env` 将所有键导入 xiaopaw 容器进程环境；`mode 0400`，宿主机 owner 为部署用户。
- **L2（secrets）**：compose `secrets:` 节段挂到 `/run/secrets/<name>`（tmpfs，mode 0400）。进程通过 `entrypoint.sh` 把带 `_FILE` 后缀的路径读成同名变量。
- **L3（manager）**：prod 部署脚本 `scripts/deploy.sh` 从 Vault 读取，`sops -d` 解密后写入 `./docker/secrets/*.txt`，然后 `docker compose up -d`。secrets 文件不进 git（`.gitignore` 匹配 `docker/secrets/**`）。
- **禁止**：在 `config.yaml` 内硬编码敏感值；在 `Dockerfile ENV` 里放 token；在日志里打印任何 secret 值（见 [07-security.md §6.2](./07-security.md) PII mask）。

### 4.4 `.env.example` 规范

```dotenv
# .env.example —— 入库，仅列 key，无任何值
# v2.1：所有敏感凭证（含飞书四件套）统一走 docker secrets；.env 只保留非敏感变量
XIAOPAW_ENV=
XIAOPAW_VERSION=
GIT_SHA=
XIAOPAW_IMAGE=
XIAOPAW_DIGEST=
PGVECTOR_DIGEST=
SANDBOX_DIGEST=
SANDBOX_TAG=
# 以下在所有环境由 docker secrets 提供（canary/prod 强制，dev 可走 secrets 占位文件）
# XIAOPAW_PG_DSN=
# XIAOPAW_METRICS_TOKEN=
# XIAOPAW_TESTAPI_TOKEN=
# QWEN_API_KEY=
# FEISHU_APP_ID=           # v2.1：迁移到 docker/secrets/feishu_app_id.txt
# FEISHU_APP_SECRET=       # v2.1：迁移到 docker/secrets/feishu_app_secret.txt
# FEISHU_VERIFICATION_TOKEN=  # 仅 HTTP 回调路径使用（WS 模式不消费）
# FEISHU_ENCRYPT_KEY=         # 仅 HTTP 回调路径使用（WS 模式不消费）
```

---

## 5. 启动顺序与依赖

### 5.1 启动拓扑

```
pgvector (healthy?) ──┐
                      ├──▶ xiaopaw (启动)
aio-sandbox (healthy?)┘
```

- **pgvector** 先起。`pg_isready` 通过后，schema migration 由 `docker-entrypoint-initdb.d/01-schema.sh`（v2.1 改 shell wrapper，见 §9.2）在**首次启动**时幂等执行；再次启动时 PG 自动跳过（`PG_VERSION` 存在）。
- **aio-sandbox** 独立启动，不依赖 pgvector（沙盒内的记忆搜索 Skill 才需要 DSN，启动不需要）。整个 `./data/workspace` 已挂入容器，`.config/` 目录此时可能为空或是 v1 遗留。
- **xiaopaw** `depends_on.condition: service_healthy` 阻塞到前两者 healthy 才启动：
  ```yaml
  xiaopaw:
    depends_on:
      pgvector: { condition: service_healthy }
      aio-sandbox: { condition: service_healthy }
  ```
- **凭证注入时序**（v2.1 新增）：xiaopaw 启动约 `t+1s` 时 `CleanupService.write_feishu_credentials` / `write_baidu_credentials` 从 secrets 读并写入 `/workspace/.config/feishu.json` / `.config/baidu.json`。此时 aio-sandbox 已 healthy 但**首次 Skill 调用尚未发生**，凭证文件就绪，不会竞态。

### 5.2 `depends_on` 配置

```yaml
xiaopaw:
  depends_on:
    pgvector:
      condition: service_healthy
    aio-sandbox:
      condition: service_healthy
```

### 5.3 启动内部时序（xiaopaw 容器内）

```
t=0     tini 作为 PID 1 启动
t+0.1   entrypoint.sh 读 secrets 转 env（含飞书四件套）
t+0.3   python -m xiaopaw.main
t+0.5   load config.yaml + feature flags
t+0.8   assert_production_safe（prod 环境）
t+1.0   CleanupService 写 /workspace/.config/feishu.json / baidu.json
        （此时 sandbox 已 healthy，但首次 Skill 调用未发生；凭证文件就位）
t+1.2   初始化 structlog + trace ContextVar
t+1.5   psycopg2.pool.ThreadedConnectionPool 初始化（v2.1）
t+2.0   connect pgvector；执行 SELECT 1
t+3.0   connect aio-sandbox（ping /healthz）
t+3.5   bind :8090 /metrics + /health（同 aiohttp Application）
t+4.0   (非 prod) bind 127.0.0.1:9090 TestAPI
t+4.5   飞书 WebSocket 建连 + 订阅
t+5.0   启动 scheduler（cron filelock）
t+5.2   /health 开始返回 200 + git_sha
        → compose service_healthy 生效
```

### 5.4 关机顺序

- `docker compose down`：逆序发送 SIGTERM：xiaopaw → aio-sandbox → pgvector。
- xiaopaw 容器内 `tini` 把 SIGTERM 转给主进程，主进程：
  1. 取消新事件入队；
  2. 等待 `_dispatch_lock` 释放（最多 `stop_grace_period=30s`）；
  3. flush trace/ctx/traces 目录；
  4. 关闭 PG pool（返还连接）；
  5. 退出。
- 超时后 Docker 发 SIGKILL。
- pgvector 在 SIGTERM 后执行 smart shutdown（WAL flush），因 `POSTGRES_INITDB_ARGS=--data-checksums`，重启时可校验。

---

## 6. Healthcheck 设计

### 6.1 三层探活契约

| 层 | 端点 / 机制 | 目的 | 失败动作 |
|----|------------|------|---------|
| L1 容器内 | `HEALTHCHECK` in Dockerfile | 进程活着、事件循环未死锁、PG pool 可用 | `docker inspect` 标记 unhealthy |
| L2 compose 编排 | `services.*.healthcheck` | `depends_on` 门控；自动重启触发 | `restart: unless-stopped` 触发 |
| L3 外部巡检 | Prometheus blackbox / SRE 脚本 | SLA 告警 | PagerDuty（见 [06-observability.md](./06-observability.md)） |

### 6.2 `/health` 实现（xiaopaw，端口 8090）

```python
# xiaopaw/api/health_server.py
# 无鉴权、200 OK 必须 < 200ms；不查下游。
# 与 /metrics 共用同一 aiohttp Application（8090），但走独立 handler，
# 不经过 Bearer middleware（middleware 挂在 /metrics 子 app 上）。
from aiohttp import web
import os, time

BUILD_INFO = {
    "service": "xiaopaw",
    "version": os.getenv("XIAOPAW_VERSION", "unknown"),
    "git_sha": os.getenv("GIT_SHA", "unknown"),
    "env": os.getenv("XIAOPAW_ENV", "unknown"),
    "started_at": int(time.time()),
}

async def health(request: web.Request) -> web.Response:
    # 不做任何 IO：DB/Sandbox 状态走 /metrics 暴露的 gauge
    return web.json_response({**BUILD_INFO, "uptime_s": int(time.time()) - BUILD_INFO["started_at"]})
```

关键约束：
- **不查下游（pgvector / sandbox）**：一旦下游抖动，`/health` 连环失败会误触发 compose 级联重启，掩盖真实故障。下游状态改用 `/metrics` 的 `xiaopaw_pgvector_up` / `xiaopaw_sandbox_up` gauge 体现，由 Prometheus 告警捕获而非 compose restart 策略处理。
- **无鉴权**：供 compose / blackbox 无状态探活；不返回任何敏感信息（仅 version / sha / uptime）。`/metrics` 同端口但走独立 subapp，带 Bearer middleware。
- **延迟预算 < 200ms**：避免 `HEALTHCHECK timeout=5s` 被慢路径占满。

### 6.3 pgvector / sandbox healthcheck

- **pgvector**：`pg_isready -U postgres -d xiaopaw_db`（内置工具）。不做 `SELECT 1`，因为 pg_isready 已足够判断主循环存活。
- **aio-sandbox**：`wget -qO- http://127.0.0.1:8080/healthz`。沙盒镜像自带。

### 6.4 失败重启策略

- 全部服务 `restart: unless-stopped`：异常退出自动重启；人工 `docker compose stop` 不会反弹。
- **连续失败保护**：compose 本身无指数退避；由 Prometheus `xiaopaw_restart_count_total` 监控，> 3 次/15 分钟触发告警（见 [06-observability.md](./06-observability.md)）。
- **start_period**：xiaopaw 设 **30s**（v2.1，aiohttp + tokenizer 惰性加载更宽松），pgvector 20s，sandbox 15s，覆盖冷启动开销，避免首 healthcheck 就失败。
- **SIGKILL 底线**：`stop_grace_period: 30s`，超时强杀。若多次触发，说明关机逻辑死锁，进入 §13 故障演练复盘。

---

## 7. 升级流程

### 7.1 升级类型与策略

| 升级类型 | 触发 | 策略 | 宕机窗口 |
|---------|------|------|---------|
| 配置变更（feature flags / 非 schema） | `config.yaml` diff | SIGHUP 热重载 | 0 |
| 代码变更（无 schema 改动） | 镜像 digest 变更 | 蓝绿切换 | < 10s |
| Schema 变更（加字段/加索引） | `initdb/*.sql` diff | 先 migration（幂等 DDL），后蓝绿 | < 10s |
| 破坏性 Schema（删字段/改类型） | 罕见 | 两阶段：N 版兼容写 → N+1 版迁移 → N+2 版下线旧字段 | 0（两阶段） |
| 安全补丁（base image CVE） | digest bump | 蓝绿 | < 10s |
| 依赖大版本（CrewAI / pgvector） | lockfile diff | canary 72h → prod 蓝绿 | < 10s |

### 7.2 蓝绿部署流程

```
t=0   当前 prod：xiaopaw-blue（digest A），接 100% 流量
t+1   拉新镜像：docker pull xiaopaw@sha256:B
t+2   启动 green：compose -p xiaopaw-green --env-file .env.prod up -d
      green 用独立 container name / 独立端口映射（如宿主机 18090:8090）
t+3   green /health 返回 200 + sha=B
t+5   green 跑冒烟：
      - curl -s 127.0.0.1:18090/health
      - curl -sH "Authorization: Bearer $T" 127.0.0.1:18090/metrics | grep xiaopaw_
      - python scripts/smoke.py --host 127.0.0.1:18090
t+10  切流量：停 blue 的飞书 WS → green 建立新 WS（lark-oapi 实际重连 5-30s，不是 <5s）
t+40  停 blue：docker compose -p xiaopaw-blue stop xiaopaw
      保留 blue 容器 24h，便于快速回滚
t+24h 清理 blue：docker compose -p xiaopaw-blue down
```

> 注：飞书 WebSocket 是**单连接**，蓝绿严格意义上是「快速滚动」。v2.1 修正：lark-oapi 重连窗口实测 **5-30s**（取决于心跳超时 + 重建时 app_secret 握手 + 订阅事件恢复），原 "<5s" 过激。队列消息因 `_dispatch_lock` 不丢失；飞书服务端在 WS 断开期间会缓存事件数分钟（见 §10 FAQ Q3）。

### 7.3 滚动重启（同 image，仅 config）

```bash
# 仅更新 config.yaml，进程内 SIGHUP 热重载
docker compose kill -s SIGHUP xiaopaw
# 观察日志：structlog 输出 {"event":"config_reload","flags":{...}}
docker compose logs xiaopaw --tail 50 | grep config_reload
```

SIGHUP 可重载范围（见 [DESIGN.md §13](../DESIGN.md)）：
- feature_flags 全部
- `rate_limit.*`
- `logging.level`
- **不可重载**：DB DSN、端口绑定、沙盒 URL（需重启）。

### 7.4 回滚步骤

```bash
# 场景：green 上线后发现严重问题
# 1. 立刻切回 blue
docker compose -p xiaopaw-blue start xiaopaw
# 2. 让飞书 WebSocket 切回 blue
docker compose -p xiaopaw-green stop xiaopaw
# 3. 复核 blue /health（v2.1：端口改 8090）
curl -s http://127.0.0.1:8090/health | jq
# 4. 若 schema 已变更，执行回滚脚本
psql "$XIAOPAW_PG_DSN" -f docker/initdb/rollback/to_v2.0.0.sql
# 5. 保留 green 容器 2 小时，`docker logs` 取证后清理
```

回滚预算（RTO）：**≤ 5 min**（v2.1 修正；从发现问题到 100% 流量回 blue）。原 v2.0 的 "≤ 2 min" 过激——实际含 WS 重连、green 优雅关闭、blue 健康探活等多环节，5 min 更现实。

### 7.5 Schema 变更规范

- 全部 DDL 必须幂等：`CREATE TABLE IF NOT EXISTS`、`CREATE INDEX IF NOT EXISTS`、`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。
- `ALTER TABLE ... SET NOT NULL` 在大表上会扫全表并持有 `AccessExclusiveLock`，**大表必走两阶段**（`ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT`，详见 [11-migration-v1-to-v2.md §4.2](./11-migration-v1-to-v2.md)）。
- 写在 `docker/initdb/NN-*.{sh,sql}`，序号递增；PG 容器**首次启动**执行，后续升级靠外部脚本。
- 升级脚本：`scripts/migrate.sh <target_version>`，执行前 `pg_dump` 全库（见 §9.3）。

---

## 8. Phase 0 Checklist

Phase 0 是 v1 → v2 切换前的**强制就绪清单**。未全部打勾，不允许启动 canary。详细条目见 [`docs/phase0-checklist.md`](./phase0-checklist.md)，此处列关键项：

### 8.1 凭证轮换

- [ ] 飞书 `FEISHU_APP_SECRET` 已轮换（v1 明文历史存在泄露嫌疑 → 新密钥）
- [ ] `FEISHU_VERIFICATION_TOKEN` 已轮换
- [ ] `FEISHU_ENCRYPT_KEY` 已轮换
- [ ] DashScope `QWEN_API_KEY` 已轮换
- [ ] pgvector root 密码已轮换；`xiaopaw_app` 独立账号已创建（最小权限）
- [ ] `XIAOPAW_METRICS_TOKEN` / `XIAOPAW_TESTAPI_TOKEN` 重新生成（≥32 字符 base64）
- [ ] 所有旧凭证已在飞书开放平台 / DashScope / PG 吊销
- [ ] Secret 管理器 `secret-rotation-runbook.md` 演练完成（见 [07-security.md §6](./07-security.md)）

### 8.2 Canary 就绪

- [ ] canary 节点独立 pgvector 实例（数据与 prod 隔离）
- [ ] canary 节点独立 aio-sandbox 实例
- [ ] canary 流量：0% 真实流量，合成负载脚本 `scripts/canary_load.py` 跑通
- [ ] pytest-memray 72h 长跑用例部署完毕，基线数据每日落 `canary-baseline/YYYY-MM-DD.json`
- [ ] Prometheus 接入 canary 的 `:8090`（/metrics 与 /health 同端口），Bearer Token 已配置
- [ ] canary 日志接入 Loki，label `env=canary`

### 8.3 Tokenizer 校准报告

- [ ] `scripts/tokenizer_calibration.py` 针对 Qwen3 跑通 1000 条真实对话样本
- [ ] 输出报告 `docs/reports/tokenizer-calibration-YYYY-MM-DD.md`
- [ ] `feature_flags.token_counter_mode = "qwen_official"` 的误差率 < 1.5%
- [ ] 异常长样本（>8k token）的对比曲线已归档
- [ ] 校准阈值写入 `config.yaml` 的 `token_budget.*`，与 canary baseline 一致

### 8.4 其他

- [ ] 容器硬化项（§11）全部通过
- [ ] 备份恢复演练（§9）跑通一次（恢复耗时 < RTO）
- [ ] 监控告警（[06-observability.md](./06-observability.md)）全部 dry-run 过
- [ ] 回滚脚本（§7.4）演练通过

---

## 9. 数据持久化与备份

### 9.1 bind mount 策略

| 挂载点 | 容器路径 | 权限 | 说明 | 是否越界风险 |
|-------|---------|------|------|------------|
| `./data/workspace/` | `/app/data/workspace` | xiaopaw RW | Agent 产出根 + 凭证文件 `.config/` | 低（xiaopaw 内 resolve 校验） |
| `./data/workspace/` | `/workspace`（沙盒，v2.1 整挂） | sandbox RW | 含 `sessions/{sid}/` + `.config/feishu.json` + `.config/baidu.json` + `cron/` | ⚠ 见下方说明 |
| `./data/sessions/` | `/app/data/sessions` | xiaopaw RW | session metadata / 复盘 | 低 |
| `./data/ctx/` | `/app/data/ctx` | xiaopaw RW | ContextCache 落盘 | 低 |
| `./data/traces/` | `/app/data/traces` | xiaopaw RW | 巡检稿 | 低 |
| `./data/cron/` | `/app/data/cron` | xiaopaw RW | filelock 保护 tasks.json（沙盒通过 `/workspace/cron` 访问同一 inode） | ⚠ 两个容器共享，锁用 fcntl |
| `./config.yaml` | `/app/config.yaml` | xiaopaw RO | 配置 | 低（RO） |
| `pgvector-data`（volume） | `/var/lib/postgresql/data` | PG own | 数据库 | 由 volume 托管 |

关键原则（v2.1 调整）：
- **workspace 整挂（T4/T5 修正）**：v2.1 把 `./data/workspace/` **整目录**挂入 sandbox `/workspace`。原因：CleanupService 需要写 `/workspace/.config/feishu.json` / `.config/baidu.json`（飞书/百度凭证注入沙盒 MCP 的唯一路径），若仅挂 `sessions/` 子目录，`.config/` 会在挂载覆盖点之外，CleanupService 写入失败。
- **替代越界防御**：Skill 代码触及其他 session 文件的风险改由**应用层防御**覆盖——SkillLoaderTool 强制 `session_id` → `Path.resolve()` 越界校验（见 [07-security.md](./07-security.md) T5），且 MCP 白名单（F7）限制 `file_operations` 的 path 前缀必须是 `/workspace/sessions/{sid}/` 或 `/workspace/.config/`。
- **RO 优先**：config / skills 目录除非业务需要，一律 RO。skill-creator 场景下 `skills` 开 RW，但不给 xiaopaw 主进程读（避免热加载风险）。
- **owner 校验**：宿主机 `./data/` owner 为 uid 65534 gid 65534（`chown -R 65534:65534 ./data`），与容器 `USER 65534:65534`（数字 uid/gid，不用 `nobody` 名称）一致。

### 9.2 pgvector 初始化（v2.1：shell wrapper 方案）

**v2.0 方案的问题**：原文档用 `CREATE USER xiaopaw_app WITH PASSWORD :'XIAOPAW_APP_DB_PASSWORD';`，依赖 psql `\set`/`:'var'` 变量——但 psql 变量**不会自动展开宿主进程环境变量**，只有通过 `-v` 显式传入或 `\set` 定义才生效，initdb 入口脚本不会做这层转换。

**v2.1 方案**：改用 shell wrapper 脚本 `docker/initdb/01-schema.sh`，在 shell 层 `$(cat ...)` 读取 secret 文件，再以 here-document 传给 `psql`：

```sh
#!/bin/bash
# docker/initdb/01-schema.sh（首次启动自动执行；需 chmod +x）
# PostgreSQL 官方 entrypoint 会按字典序执行 .sh / .sql 文件；.sh 直接走 bash。
set -euo pipefail

# 读应用账号密码（secret 文件由 compose 挂到 /run/secrets/pg_app_password）
: "${XIAOPAW_APP_DB_PASSWORD_FILE:?must be set}"
APP_PASSWORD="$(cat "${XIAOPAW_APP_DB_PASSWORD_FILE}")"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;

    CREATE TABLE IF NOT EXISTS memories (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id   TEXT NOT NULL,
        routing_key  TEXT NOT NULL,
        kind         TEXT NOT NULL,
        content      TEXT NOT NULL,
        embedding    vector(1536),
        tags         TEXT[],
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS idx_memories_embedding
        ON memories USING hnsw (embedding vector_cosine_ops);
    CREATE INDEX IF NOT EXISTS idx_memories_tags
        ON memories USING gin (tags);
    CREATE INDEX IF NOT EXISTS idx_memories_session
        ON memories (session_id, created_at DESC);

    -- 幂等创建应用账号（首次启动有效；再次启动 PG 跳过整个 initdb）
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xiaopaw_app') THEN
            CREATE USER xiaopaw_app WITH
                PASSWORD '${APP_PASSWORD}'
                LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                CONNECTION LIMIT 20;
        END IF;
    END
    \$\$;

    -- 授权（最小权限）
    GRANT CONNECT ON DATABASE xiaopaw_db TO xiaopaw_app;
    GRANT USAGE ON SCHEMA public TO xiaopaw_app;
    GRANT SELECT, INSERT, UPDATE ON memories TO xiaopaw_app;
    -- 不给 DELETE：删除由 TTL job 用高权限账号执行
EOSQL
```

**compose 传参**（在 `compose.base.yaml` 的 `pgvector.environment` 已声明 `XIAOPAW_APP_DB_PASSWORD_FILE: /run/secrets/pg_app_password`，见 §2.2）。

**安全提示**：`${APP_PASSWORD}` 会被 shell 展开到 here-document 里进入 psql 会话；不会出现在容器日志中（psql 不 echo `EOSQL` 内容）。如仍担心泄露，可改用 psql `\copy` 从文件读，或 `PGPASSWORD` 环境变量配合 `CREATE USER ... PASSWORD $$\$${APP_PASSWORD}$$`。

---

### 9.3 pgvector 备份

**策略**：每日凌晨 3 点 `pg_dump` + 每小时增量 WAL 归档（单节点简化方案）。

```bash
# scripts/backup_pg.sh，由宿主机 cron 调用
#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="/var/backups/xiaopaw/pg"
TODAY="$(date -u +%Y%m%d)"
DUMP="${BACKUP_ROOT}/${TODAY}.dump.gz"

mkdir -p "${BACKUP_ROOT}"

docker exec xiaopaw-pgvector pg_dump \
    -U postgres -d xiaopaw_db \
    --format=custom --compress=9 \
    | gzip -9 > "${DUMP}"

# 校验
docker exec xiaopaw-pgvector pg_restore --list < "${DUMP}" > /dev/null

# 保留 30 天
find "${BACKUP_ROOT}" -name "*.dump.gz" -mtime +30 -delete
```

- **RTO**：< 15 min（从最近 dump 恢复到 200GB 规模库）。
- **RPO**：≤ 24 h（纯 dump）；若启用 WAL 归档则 ≤ 1 h。
- **异地**：dump 文件每日 rsync 到 OSS 冷存储（由 SRE 加 cron）。

### 9.4 workspace 归档

`./data/workspace/sessions/{sid}/` 存 Agent 文件产物。策略：

- **日归档**：每日 02:00，`tar -czf workspace-YYYYMMDD.tar.gz ./data/workspace/sessions/`（仅归档 closed session）。
- **保留**：90 天在线 + 1 年冷存储（OSS Infrequent Access）。
- **隐私**：归档前扫描 PII（复用 [07-security.md §6](./07-security.md) 的 mask 规则），含 PII 的 session 加密归档。

### 9.5 恢复演练

每季度执行一次：
1. 从最近 dump 拉起临时 PG 实例（`docker compose -f compose.recover.yaml up -d`）
2. 导入 dump，验证 `SELECT count(*) FROM memories`
3. 抽样 5 条 session 从 tar.gz 还原，对比 hash
4. 记录耗时，更新 RTO 承诺。

---

## 10. 日志与监控接入

> 告警规则详见 [06-observability.md](./06-observability.md)；本节只涉及部署层集成方式。

### 10.1 Prometheus 端口暴露

```
Prometheus (host)  ──(Bearer Token)──▶  127.0.0.1:8090 /metrics  (xiaopaw 容器)
```

- `/metrics` 与 `/health` 同宿主端口 **8090**（aiohttp 同 Application，`/metrics` 走 Bearer middleware，`/health` 独立 handler）。
- Prometheus scrape 配置：

```yaml
# prometheus.yml
scrape_configs:
  - job_name: xiaopaw
    scrape_interval: 15s
    metrics_path: /metrics
    scheme: http
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/xiaopaw_metrics_token
    static_configs:
      - targets: ["127.0.0.1:8090"]
        labels:
          env: prod
          service: xiaopaw
```

- `build_info` gauge：`xiaopaw_build_info{version="v2.0.0", git_sha="a1b2c3d", env="prod"} 1`，便于 Grafana 面板标注升级时刻。

### 10.2 pgvector / sandbox exporter

- **pgvector**：部署 `postgres_exporter` sidecar（Bitnami image），连 `xiaopaw_readonly` 只读账号，仅暴露 pg_stat_* 视图。
- **sandbox**：沙盒自带 `/metrics`（Prom 格式），同样 bind 127.0.0.1。

### 10.3 日志聚合

选型：**Loki + Promtail**（轻量，与 Prometheus 同栈）。备选 ELK。

- **xiaopaw 输出**：structlog JSON 到 stdout；`tini` 不干预；docker driver `json-file`（默认）。
- **Promtail 采集**：从宿主机 `/var/lib/docker/containers/*/_json.log` 拉取，label `container=xiaopaw-app / xiaopaw-pgvector / xiaopaw-sandbox`。
- **trace_id 抽取**：Promtail pipeline 用 JSON 阶段提取 `trace_id` 作为 label，支持跨容器串联。
- **日志驱动配置**（可选加固）：

```yaml
# compose override
services:
  xiaopaw:
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "10"
        tag: "{{.Name}}"
```

避免磁盘被日志打满：xiaopaw 容器日志硬上限 500MB（`max-size * max-file`）。

### 10.4 集中式告警流转

```
Prometheus → Alertmanager → 飞书机器人 / PagerDuty
Loki       → LogQL alert → Alertmanager（同上）
```

单节点无 HA Alertmanager；SRE 需注意 Alertmanager 宕机时告警会丢，监控 host 存活另配 blackbox 外探。

---

## 11. 容器硬化

> 本节与 [07-security.md §12](./07-security.md) 严格一致。此处仅总结部署视角的 checklist，威胁模型详见该文档。

### 11.1 Checklist

| 加固项 | 实施位置 | 验证方式 |
|-------|---------|---------|
| `USER 65534:65534`（数字 uid/gid） | Dockerfile + compose `user: "65534:65534"` | `docker exec xiaopaw id` → `uid=65534` |
| `read_only: true` | compose | `docker exec xiaopaw touch /test` → EROFS |
| `tmpfs /tmp` | compose | 同上但 /tmp 可写 |
| `no-new-privileges: true` | compose security_opt | `docker inspect` 查 `SecurityOpt` |
| `cap_drop: [ALL]` | compose | 同上 |
| `seccomp: xiaopaw-profile.json` | compose + profile | 默认禁 `ptrace` / `mount` 等 |
| Image digest 钉版本 | `.env` + compose | `docker image ls --digests` 对比 |
| `.env` mode 0400 | 宿主机 | `stat -c "%a" .env` → `400` |
| docker secrets tmpfs 挂载 | compose | `docker exec xiaopaw ls -la /run/secrets` |
| 网络 `internal: false`；prod 只暴露 `8090` | compose ports | `ss -tnlp` 确认仅 `:8090` 对外（9090 无、8080/5432 无） |
| pgvector `xiaopaw_app` 最小权限 | initdb SQL | `\du` 查 rolattributes |
| 沙盒 bind mount 整挂 `./data/workspace/`（含 `.config/`）；越界防御由应用层实现 | compose | `docker exec sandbox ls /workspace/` 应含 `sessions/`、`.config/`、`cron/`；应用层 `Path.resolve()` 越界校验 + MCP 白名单路径前缀限制 |

### 11.2 镜像 digest 管理

```bash
# CI 构建后写回 .env
docker build -t xiaopaw:${VERSION} \
  --build-arg GIT_SHA=${GIT_SHA} \
  --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --build-arg XIAOPAW_VERSION=${VERSION} .

# push 后拉取 digest
docker push registry.example/xiaopaw:${VERSION}
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' \
         registry.example/xiaopaw:${VERSION} | cut -d@ -f2)

# 写入部署包
sed -i "s|^XIAOPAW_DIGEST=.*|XIAOPAW_DIGEST=${DIGEST}|" .env.prod
```

上游镜像（pgvector / sandbox）digest 由 Renovate 自动更新，每季度安全评审一次。

---

## 12. 资源预算

**假设**：单租户、日均 5k 条飞书消息、日新增 memories ≤ 50k 条、沙盒并发 ≤ 3、vector 维度 1536。

### 12.1 CPU

| 服务 | 预留 | 上限 | 峰值依据 |
|------|------|------|---------|
| xiaopaw | 1.0 | 4.0 | 飞书消息峰值 20 QPS × 平均 100ms 响应 |
| pgvector | 0.5 | 2.0 | HNSW 查询单次 ~30ms；并发 ≤ 10 |
| aio-sandbox | 0.5 | 2.0 | 单 Skill 占 1 核；并发上限 3 |
| 合计 | **2.0** | **8.0** | 建议宿主机 ≥ 8 vCPU（留 HEADROOM） |

### 12.2 内存

| 服务 | 预留 | 上限 | 说明 |
|------|------|------|------|
| xiaopaw | 1.0 GB | 4.0 GB | Python + CrewAI + trace 缓冲；canary 验过 72h 增长 < 1MB/h |
| pgvector | 512 MB | 2.0 GB | `shared_buffers=512MB`，HNSW 索引 ~1GB/百万向量 |
| aio-sandbox | 512 MB | 2.0 GB | Skill 执行空间 |
| 系统预留 | 1 GB | — | OS / docker daemon / exporter |
| **合计** | **3 GB** | **10 GB** | 建议宿主机 ≥ 16 GB |

内存保护：`deploy.resources.limits.memory` 设 4GB 对 xiaopaw；OOM 时由 docker 发 SIGKILL，Prometheus `xiaopaw_oom_total` 告警。

### 12.3 磁盘

| 项目 | 预估 | 说明 |
|------|------|------|
| pgvector 数据 | 30 GB / 百万 memories | vector(1536) + HNSW + tags GIN |
| workspace/sessions | 2 GB / 万 session | Agent 产出文件 + 上下文 |
| traces | 5 GB / 万次巡检 | JSON trace |
| 日志（docker json） | 1.5 GB（500MB × 3 容器） | 硬上限 |
| 备份（本地） | 30 × daily dump ~30GB | `/var/backups/xiaopaw` |
| 镜像层 | ~1.5 GB | xiaopaw 220MB + pgvector 600MB + sandbox 700MB |
| **合计（首年）** | **≈ 80 GB** | 建议系统盘 100GB + 数据盘 ≥ 200GB |

### 12.4 网络

| 流量方向 | 估算 | 说明 |
|---------|------|------|
| 飞书出站 | 200 KB/s 峰值 | WebSocket + 文件上传 |
| DashScope 出站 | 500 KB/s 峰值 | Qwen chat + embedding |
| Prometheus 入站 | 50 KB/15s | /metrics scrape |
| 内网 xiaopaw ↔ pgvector | 1 MB/s 峰值 | embedding 查询 |

对带宽没有硬要求；100 Mbps 足够。延迟敏感：DashScope 走就近 region（华东 2 对应 cn-shanghai）。

### 12.5 单节点宿主机推荐规格

| 档位 | vCPU | RAM | 磁盘 | 适用 |
|------|------|-----|------|------|
| 最小 | 4 | 8 GB | 100 GB SSD | dev |
| 推荐 | 8 | 16 GB | 200 GB SSD | canary / 小体量 prod |
| 充裕 | 16 | 32 GB | 500 GB SSD | 单租户高峰 prod（日 5 万消息+） |

---

## 13. 故障演练清单

所有演练以**不影响 prod 流量**为前提：在 canary 上触发，观察恢复行为；将预期/实际差异记入 `docs/reports/drill-YYYY-MM-DD.md`。

### 13.1 演练矩阵

| # | 故障类型 | 触发方式 | 预期恢复行为 | 预期 RTO |
|---|---------|---------|------------|---------|
| D1 | xiaopaw 进程 OOM | `stress-ng --vm 2 --vm-bytes 4G` in container | compose 自动重启；飞书 WebSocket 重连；队列消息不丢 | ≤ 30s |
| D2 | pgvector 宕机 | `docker compose kill pgvector` | xiaopaw 记忆搜索降级到 tags-only；metric `xiaopaw_pgvector_up=0` 告警；pgvector 重启后自动恢复 | ≤ 60s |
| D3 | aio-sandbox 宕机 | `docker compose kill aio-sandbox` | 当前 Skill 报 timeout；新事件快速失败；sandbox 重启后恢复；trace 显示 fallback | ≤ 30s |
| D4 | 飞书断网 | `iptables -A OUTPUT -d <feishu_ip> -j REJECT --reject-with tcp-reset`（v2.1：用 REJECT 不用 DROP；DROP 会让 TCP 僵死到 ~2h keepalive 才报错，REJECT 立即回 RST 触发重连） | WebSocket 收到 RST → 立即进入指数退避重连；`xiaopaw_feishu_reconnect_total` 上升；trace 中断可回溯 | ≤ 60s（网络恢复） |
| D5 | DashScope 429 | mock 返回 429 | 指数退避 + feature flag `enable_feishu_rate_limit_aware=true` 生效；metric `xiaopaw_llm_rate_limited_total` 上升 | ≤ 10s 单次 |
| D6 | 磁盘写满 | `fallocate -l 99G /data/dummy` | pgvector 告警；xiaopaw `/health` 仍 200（不查磁盘），但 trace 写失败降级到 in-memory buffer；释放磁盘后恢复 | ≤ 2 min |
| D7 | Skill 死循环 | 手工注入 Skill 跑 `while true: pass` | skill timeout（`enable_skill_timeout=true`）10s 后 SIGKILL；沙盒不积累 zombie（ADR-006） | ≤ 10s |
| D8 | Cron 并发双触发 | 手工同时 spawn 两份 xiaopaw（违反前提，仅测防御） | filelock 拒绝第二份；`xiaopaw_cron_lock_denied_total` 自增 | 即时 |
| D9 | `config.yaml` 错误 | 写一个坏 YAML 后 SIGHUP | 保留旧配置；log error；无进程退出；metric `xiaopaw_config_reload_error_total` 自增 | 即时（回滚） |
| D10 | 凭证轮换演练 | 按 secret-rotation-runbook.md 切换 | 新凭证生效；旧凭证在宽限期内共存；无 5xx | ≤ 2 min |
| D11 | 数据库恢复 | `docker compose down -v` 后从 dump 恢复 | 见 §9.5 | ≤ 15 min |
| D12 | 蓝绿回滚 | 伪造 green 异常 | 按 §7.4 回滚 | ≤ 2 min |

### 13.2 演练产出

每次演练必须产出：
- 时间轴（秒级，含触发 / 首次告警 / 首次恢复信号 / 完全恢复）
- 预期与实际差距
- Action items（若 RTO 超标或有副作用）
- 追加到 [06-observability.md](./06-observability.md) 对应 metric 的 baseline 表

### 13.3 频率

- **D1 / D2 / D3 / D7 / D9**：每次 v2.x 发布前必跑（CI 自动）
- **D4 / D5 / D6 / D8**：每季度人工跑
- **D10 / D11 / D12**：每半年
- **D11（恢复演练）**：与 §9.5 合并

---

## 14. 上线 Checklist

> **用法**：prod 首次上线 / 每次蓝绿切换前完整过一遍。未全部 ✅ 不允许 `docker compose up -d`。

### 14.1 代码与镜像

- [ ] 主分支 tag `v2.x.y`，CI 全绿
- [ ] 镜像已推至私有 registry：`docker pull registry.example/xiaopaw:v2.x.y`
- [ ] `XIAOPAW_DIGEST` / `PGVECTOR_DIGEST` / `SANDBOX_DIGEST` 写入 `.env.prod`
- [ ] 镜像 CVE 扫描通过（trivy HIGH+ 零告警）
- [ ] `GIT_SHA` ARG 已注入，`/health` 返回正确 sha

### 14.2 配置与凭证

- [ ] `config.yaml` 从 `config.yaml.example` diff 审阅，所有敏感项为占位符或经 secrets 注入
- [ ] `.env.prod` mode `0400`，owner 部署用户
- [ ] `docker/secrets/*.txt` 全部就位，mode `0400`
- [ ] `XIAOPAW_METRICS_TOKEN` ≥ 32 字符
- [ ] `XIAOPAW_TESTAPI_TOKEN` prod 可为空（TestAPI 禁用），canary 必填
- [ ] Phase 0 checklist（§8）全部 ✅

### 14.3 依赖服务

- [ ] pgvector 能 `pg_isready`
- [ ] aio-sandbox `/healthz` 200
- [ ] Prometheus scrape 配置含 Bearer Token
- [ ] Loki / Promtail 已接入容器日志
- [ ] Alertmanager 飞书机器人测试通过

### 14.4 数据

- [ ] pgvector schema 已 migrate 到 `v2.x.y`（幂等检查）
- [ ] 备份脚本 `scripts/backup_pg.sh` 挂入宿主机 cron
- [ ] `./data/*` owner 为 `65534:65534`
- [ ] 沙盒 mount 整挂 `./data/workspace/`（验证 `docker exec sandbox ls /workspace` 含 `sessions/` + `.config/` + `cron/`）；越界防御由应用层 `Path.resolve()` + MCP 白名单路径前缀覆盖

### 14.5 容器硬化

- [ ] `docker inspect xiaopaw-app | grep -E "ReadonlyRootfs|SecurityOpt|CapDrop"` 与 §11 一致
- [ ] `docker exec xiaopaw-app id` 返回 `uid=65534`
- [ ] `docker exec xiaopaw-app touch /x` 报 EROFS
- [ ] prod 只暴露 `:8090`；9090 TestAPI / 8080 sandbox / 5432 pgvector 公网均不可达（`ss -tnlp | grep -E ':(9090|8080|5432)'` 应为空）

### 14.6 启动与验证

- [ ] `docker compose up -d` 后 3 min 内全部服务 `healthy`
- [ ] `curl -s 127.0.0.1:8090/health | jq '.git_sha'` 返回最新 sha
- [ ] `curl -sH "Authorization: Bearer $T" 127.0.0.1:8090/metrics | grep xiaopaw_build_info` 返回正确 version
- [ ] 飞书发一条测试消息，`docker logs xiaopaw` 可见完整 trace_id 流
- [ ] canary 72h baseline（内存 / 延迟）落盘

### 14.7 回滚预案

- [ ] 上一版本 blue 容器保留运行，`docker ps -a` 可见
- [ ] `scripts/rollback.sh` 已演练，RTO < 2 min
- [ ] Schema 回滚 SQL `rollback/to_<prev_version>.sql` 存在且幂等
- [ ] on-call 轮值确认；回滚 runbook 已发 SRE 群

---

**文档维护**：部署流程调整、容器加固项新增、Phase 0 变更、资源预算重估，必须同步更新本文件与 [DESIGN.md §12](../DESIGN.md)。最近一次变更见 git blame。
