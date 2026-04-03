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
```bash
pip install -e .[dev]
uvicorn main:app --reload --app-dir src
python src/worker_main.py
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
```

`docker/src/docker-compose.yml` 只使用根 `.env` 中的原始变量名；
启动脚本会通过 `--env-file .env` 显式传入。

2) 使用原有脚本手动启动基础依赖：

```bash
./scripts/start_postgre.sh
./scripts/start_redis.sh
```

3) 使用原有脚本手动启动应用栈：

```bash
./scripts/start_src.sh
```

4) `keyflow-api` 启动时自动检查并创建缺失表（已存在表不会重复创建）；`keyflow-worker` 复用同一镜像与配置，单独执行周期后台任务。

5) 手动检查服务健康：

```bash
curl http://localhost:8000/health
docker compose -f docker/src/docker-compose.yml ps
```

本地 compose 表达的是“一个整体部署单元里的两个运行时角色”；生产 Kubernetes 目标是 **same Pod, two containers**，而不是单独的 worker Deployment。

6) 关闭：

```bash
docker compose -f docker/src/docker-compose.yml down
docker compose -f docker/redis/docker-compose.yml down
docker compose -f docker/postgresql/docker-compose.yml down
```

## scripts 便捷命令（按组件）

```bash
./scripts/start_postgre.sh
./scripts/start_redis.sh
./scripts/start_src.sh
```

停止：

```bash
./scripts/stop_src.sh
./scripts/stop_redis.sh
./scripts/stop_postgre.sh
```

Windows（cmd/powershell）：

```bat
scripts\start_postgre.bat
scripts\start_redis.bat
scripts\start_src.bat
scripts\stop_src.bat
scripts\stop_redis.bat
scripts\stop_postgre.bat
```

启动/停止可选参数：

```bash
./scripts/start_src.sh --no-build
./scripts/stop_src.sh --volumes
./scripts/stop_redis.sh --volumes
./scripts/stop_postgre.sh --volumes
```

```bat
scripts\start_src.bat --no-build
scripts\stop_src.bat --volumes
scripts\stop_redis.bat --volumes
scripts\stop_postgre.bat --volumes
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
