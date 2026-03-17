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
