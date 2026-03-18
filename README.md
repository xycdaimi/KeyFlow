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

## 本地开发
```bash
pip install -e .[dev]
uvicorn main:app --reload --app-dir src
pytest
```

## Docker 本地联调（拆分 Compose）

1) 准备环境变量：

```bash
cp .env.example .env
```

`docker/src/docker-compose.yml` 只使用根 `.env` 中的原始变量名；
启动脚本会通过 `--env-file .env` 显式传入。

2) 启动基础依赖：

```bash
docker compose -f docker/postgresql/docker-compose.yml up -d
docker compose -f docker/redis/docker-compose.yml up -d
```

3) 启动应用容器：

```bash
docker compose --env-file .env -f docker/src/docker-compose.yml up -d --build
```

4) 启动时自动检查并创建缺失表（已存在表不会重复创建）。

5) 检查服务健康：

```bash
curl http://localhost:8000/health
docker compose -f docker/src/docker-compose.yml ps
```

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

可选参数：

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
