# 项目闭环评估待办

> 更新时间：2026-03-20

## 仍需补齐

- 若引入针对在线 provider 的集成测试或本地脚本：避免硬编码凭证与导入即外呼；应作为 opt-in、受控集成测试或独立手工脚本，勿纳入默认 `pytest` 套件。
- 若要继续推进结构化凭证重构，需补数据库迁移策略与线上/现有库切换方案；当前代码已按 `break-now` 口径切换，不保留旧 `api_key` 字符串兼容。

## 已对齐（文档与证明）

- **Redis + Lua 真实并发**：`tests/test_redis_lua_integration.py`（默认 `redis://localhost:6379/9` 可达时）。
- **PostgreSQL 仓储集成**：`tests/test_postgres_repository_integration.py` 已在 Docker 网络内 PostgreSQL 实测通过（`2 passed`）；宿主机默认 `127.0.0.1:5432` 在本机仍可能因环境/端口问题失败，可通过 `KEYFLOW_INTEGRATION_PG_URL` 等覆盖连接。
- **`/health`**：依赖感知（`status` + `checks`），非固定静态体；API 回归见 `tests/test_api.py`。
- **运行时**：单镜像双运行时；API 与 sidecar worker 组成同一部署单元，worker 周期执行 `recover_cooldowns` / `refresh_keys`，共享 PostgreSQL + Redis 真相源。
- **Docker 本地验证**：`README.md` 提供分步启动与人工检查说明；由人工确认 `/health` 与 `keyflow-worker` 存活，不再维护单独 smoke 脚本。

## 建议顺序

1. 延续上文 opt-in 口径：若新增针对在线 provider 的集成测试或本地脚本，保持显式标记/排除路径，勿纳入默认 `pytest` 套件
2. 在 CI/固定环境为 PostgreSQL 集成测试提供稳定 `KEYFLOW_*` 或 Docker 服务
