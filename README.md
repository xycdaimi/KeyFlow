# KeyFlow

`KeyFlow` 是一个面向单个 `provider` 账户池的 API Key 调度服务。

## 边界
- 负责 `provider` 内 key 调度、状态管理、冷却恢复和管理接口。
- 不负责 `provider fallback`、任务路由、模型选择，这些职责应保留在上游系统。

## 技术主线
- `DDD + CQRS`
- `FastAPI` 提供接口
- `Redis + Lua` 提供高并发原子分配
- `SQLAlchemy` 持久化 key 元数据

## 运行时（v1）

- **双运行时、单镜像**：同一镜像提供两个启动命令，`uvicorn main:app --app-dir src` 负责 HTTP，`python src/worker_main.py` 负责轻量周期后台扫描。
- **同一部署单元**：本地 Docker 以一个 compose stack 中的 `keyflow-api + keyflow-worker` 两个服务表达；生产目标是 **same Pod, two containers**，不是两个彼此漂移的独立 Deployment。
- **后台任务归 worker**：`recover_cooldowns` 与 `refresh_keys` 由 sidecar worker 周期执行，API `lifespan` 不再自启后台循环。
- **共享真相源**：worker 保持无状态，只通过现有 PostgreSQL + Redis 仓储/缓存抽象工作。
- **默认并发**：`.env.example` 与 `docker/src/Dockerfile` 中 `UVICORN_WORKERS` 默认为 **1**；需要扩展 HTTP 吞吐时只增加 API 进程，后台周期任务仍由 worker 负责。

## 本地开发

### 启动顺序

多服务器部署时推荐顺序是：

1. 先启动 Gateway 控制面服务。
2. 再启动各个子节点 KeyFlow 服务。

原因：子节点是主动向 gateway 注册和发送心跳，不是由 gateway 扫描发现子节点。子节点如果先启动，会按重试逻辑继续尝试注册，但正式部署和本地联调都应先让 gateway 可达。

### Gateway 控制面服务

Gateway 是独立服务，使用 `.env.gateway`，不使用子节点的 `.env`。它只负责 ai_router 管理面：节点注册、心跳、能力聚合和凭证管理请求转发，不负责运行时分配。

```bash
pip install -e .[dev]
cp .env.gateway.example .env.gateway
uvicorn gateway.main:app --reload --app-dir src --host 0.0.0.0 --port 8001
```

Gateway 的入口是：

```text
gateway.main:app
```

Gateway 配置文件中的关键变量：

```env
APP_NAME=KeyFlow Gateway
APP_VERSION=0.1.0
API_PREFIX=/api/gateway
KEYFLOW_RUNTIME_MODE=local
LOCAL_SQLITE_PATH=data/keyflow_gateway.db
GATEWAY_INTERNAL_KEY=change-me-gateway-admin
GATEWAY_REGISTER_KEY=change-me-gateway-register
```

### 子节点 KeyFlow 服务

子节点服务使用根目录 `.env`。这是原有账户池服务，负责本机/同出口 IP 的凭证存储、分配、上报和供应商插件调用。

```bash
cp .env.example .env
uvicorn main:app --reload --app-dir src
python src/worker_main.py
```

子节点如果需要注册到 gateway，在 `.env` 中配置这一组可选变量：

```env
GATEWAY_URL=http://keyflow-gateway:8001
GATEWAY_REGISTER_KEY=change-me-gateway-register
NODE_ID=node-shanghai-01
NODE_DISPLAY_NAME=Shanghai Node 01
NODE_PUBLIC_BASE_URL=http://keyflow-node-01:8000
NODE_TAGS=shanghai,telecom
NODE_HEARTBEAT_INTERVAL_SECONDS=30
```

这些变量不完整时，子节点不会启动 gateway 注册客户端，原有本地分配、上报和管理接口不受影响。

容器部署时也按两个 env 文件拆开：

```bash
# 1. Gateway 容器
docker run --env-file .env.gateway <keyflow-image> \
  uvicorn gateway.main:app --host 0.0.0.0 --port 8001 --app-dir src

# 2. 子节点容器
docker run --env-file .env <keyflow-image> \
  uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir src
```

测试建议：

- 日常本地回归优先使用当前默认安全的 targeted 集合（避免将「需外网或本地凭证」的 opt-in 集成测试或手工脚本误当作默认 CI 回归）：

```bash
python -m pytest tests/test_api.py tests/test_provider_plugins.py tests/test_redis_lua_integration.py tests/test_worker_runtime.py -q
```

- 若要验证 PostgreSQL 仓储集成，请在**可达数据库**的前提下单独执行 `tests/test_postgres_repository_integration.py`；宿主机默认 `127.0.0.1:5432` 在部分机器上可能失败，Docker 网络内环境为当前已验证路径。

## Docker 本地联调（拆分 Compose）

1) 准备环境变量：

```bash
cp .env.example .env
cp .env.gateway.example .env.gateway
```

`docker/gateway/docker-compose.yml` 使用 `.env.gateway`；`docker/src/docker-compose.yml` 使用 `.env`。
启动脚本会通过 `--env-file` 显式传入对应 env 文件。

2) 启动 Gateway 控制面：

```bash
./scripts/start_gateway.sh
```

3) 启动子节点依赖：

```bash
./scripts/start_postgre.sh
./scripts/start_redis.sh
```

4) 启动子节点 API/worker：

```bash
./scripts/start_src.sh
```

`keyflow-gateway` 先启动并等待子节点主动注册；`keyflow-api` 启动时会读取 `.env` 中的 `GATEWAY_URL`、`GATEWAY_REGISTER_KEY`、`NODE_ID`、`NODE_PUBLIC_BASE_URL`，配置完整时向 gateway 注册并持续发送心跳。`keyflow-worker` 复用同一镜像与 `.env`，单独执行周期后台任务。

5) 手动检查服务健康：

```bash
curl http://localhost:8001/openapi.json
curl http://localhost:8000/health
docker compose -f docker/gateway/docker-compose.yml ps
docker compose -f docker/src/docker-compose.yml ps
```

本地 compose 中 gateway 是独立控制面服务；子节点仍表达为“一个整体部署单元里的两个运行时角色”。生产 Kubernetes 中，每个子节点仍是 **same Pod, two containers**（API + worker），gateway 单独部署。

6) 关闭：

```bash
./scripts/stop_src.sh
./scripts/stop_redis.sh
./scripts/stop_postgre.sh
./scripts/stop_gateway.sh
```

## scripts 便捷命令（按组件）

```bash
./scripts/start_gateway.sh
./scripts/start_postgre.sh
./scripts/start_redis.sh
./scripts/start_src.sh
```

停止：

```bash
./scripts/stop_src.sh
./scripts/stop_redis.sh
./scripts/stop_postgre.sh
./scripts/stop_gateway.sh
```

Windows（cmd/powershell）：

```bat
scripts\start_gateway.bat
scripts\start_postgre.bat
scripts\start_redis.bat
scripts\start_src.bat
scripts\stop_src.bat
scripts\stop_redis.bat
scripts\stop_postgre.bat
scripts\stop_gateway.bat
```

启动/停止可选参数：

```bash
./scripts/start_gateway.sh --no-build
./scripts/start_src.sh --no-build
./scripts/stop_src.sh --volumes
./scripts/stop_redis.sh --volumes
./scripts/stop_postgre.sh --volumes
./scripts/stop_gateway.sh --volumes
```

```bat
scripts\start_gateway.bat --no-build
scripts\start_src.bat --no-build
scripts\stop_src.bat --volumes
scripts\stop_redis.bat --volumes
scripts\stop_postgre.bat --volumes
scripts\stop_gateway.bat --volumes
```

## Model Alias Config

KeyFlow can load canonical-to-provider model aliases from a YAML file.

- Env var: `MODEL_ALIAS_CONFIG_PATH`
- If not set: startup continues with empty alias mapping.
- If set but file is missing / unreadable / invalid YAML: startup fails immediately.

Example:

```yaml
version: 1
models:
  gpt-4o:
    providers:
      openai:
        - gpt-4o
      openrouter:
        - openai/gpt-4o
        - openai/gpt-4o-2024-11-20
```

Docker compose mount example:

```yaml
services:
  keyflow-api:
    environment:
      MODEL_ALIAS_CONFIG_PATH: /config/model_aliases.yaml
    volumes:
      - ./config/model_aliases.yaml:/config/model_aliases.yaml:ro
```

When request payload includes `model`, allocation responses include `provider_model`.
Use `provider_model` as the upstream provider-native model name.
