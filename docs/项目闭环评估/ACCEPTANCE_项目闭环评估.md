# 项目闭环评估验收记录

> 更新时间：2026-03-20
> 范围：验收台账与本轮**实测**验证命令（Task 6：文档与 ledger 对齐）。

## 本轮完成项（文档）

- 更新 `docs/plan.md`、`TODO_项目闭环评估.md`、本文件，使台账与下列证明一致：**Redis+Lua 并发**、**PostgreSQL 仓储集成（含 Docker 网络内实测通过）**、**依赖感知 `/health`**、**API + sidecar worker 双运行时**、**Docker 分步启动与人工验证路径**。
- `README.md`：仅在健康检查小节补充 `/health` 响应形态说明（与实现一致）。

## 本轮验证结果

### 自动化测试（ targeted；2026-03-20，Windows 宿主机）

已执行：

```text
python -m pytest tests/test_redis_lua_integration.py tests/test_postgres_repository_integration.py tests/test_worker_runtime.py -q -v
python -m pytest tests/test_api.py::test_health_reports_ready_when_dependencies_ok tests/test_api.py::test_health_returns_503_degraded_when_dependency_fails -q -v
docker run --rm --network keyflow_keyflow-network -v "D:/py/keyflow/.worktrees/keyflow-v1-closure:/ws" -w /ws python:3.13-bookworm bash -lc "pip install -q '.[dev]' && KEYFLOW_PG_ADMIN_URL=postgresql://keyflow:keyflow@keyflow-postgres:5432/keyflow KEYFLOW_INTEGRATION_PG_BASE=postgresql+asyncpg://keyflow:keyflow@keyflow-postgres:5432 python -m pytest tests/test_postgres_repository_integration.py -q -v --tb=short"
```

结果摘要：

| 范围 | 结果 | 说明 |
|------|------|------|
| `tests/test_redis_lua_integration.py` | **2 passed** | 真实 Redis + Lua 并发不重复分配 |
| `tests/test_postgres_repository_integration.py` | **2 errors**（宿主机默认地址） | setup 连接 `127.0.0.1:5432` 失败（连接被重置）；说明本机宿主机默认前提不可靠 |
| Docker 网络内 `tests/test_postgres_repository_integration.py` | **2 passed in 1.26s** | 使用 `keyflow_keyflow-network` + `keyflow-postgres` 实测通过，证明目标部署拓扑下仓储链路成立 |
| `tests/test_worker_runtime.py` | **worker 契约通过** | sidecar worker 单次迭代、阶段容错、入口配置读取 |
| `tests/test_api.py` health 两例 | **2 passed** | `/health` 在依赖全好时为 200 + `status: ok`；依赖失败时为 503 + `degraded`，**非固定静态体** |

**结论**：不宣称「PostgreSQL 集成测试在本机宿主机默认地址下已通过」；该路径在本机仍受环境/端口影响。但同一测试已在预期的 Docker 网络环境中实测通过。本地运行层验证由人工按既有启动脚本执行并确认。

### 与运行时口径一致的表述（v1）

- **双运行时、单部署单元**：`uvicorn` 启动 API，`python src/worker_main.py` 启动 sidecar worker；默认 `UVICORN_WORKERS=1`，避免单容器内重复起多份 API 进程。
- **后台任务**：`recover_cooldowns`、`refresh_keys` 由 sidecar worker 周期执行；API `lifespan` 不再拥有后台循环。
- **部署目标**：本地是同一 compose stack 的 `keyflow-api + keyflow-worker`；Kubernetes 目标是 **same Pod, two containers**，不是独立 worker Deployment。

### Docker 冒烟

- **文档**：`README.md`（拆分 compose、原有 `start_*` / `stop_*` 脚本、健康检查 URL）。
- **可重复性**：运行层验证改为人工执行；仓库不再保留单独 smoke 脚本及其契约测试。

### 全量 pytest

- 全量 `pytest` 若纳入「导入即访问外网 / 依赖本地密钥」的 opt-in 集成测试或独立脚本，结果可能不稳定；本 worktree 的 `tests/` 目录下**未**包含此类独立测试文件；默认回归建议使用 README 中的 targeted 集合，或对后续新增的 opt-in 用例显式排除（如 `pytest` 的 `-m` / `--ignore`）。

## 当前结论

```text
依赖类证明已落地为可运行测试与文档；PostgreSQL 集成在本仓库默认宿主机前提下可能失败，需按测试说明配置环境。
但其 Docker 网络内验证已通过；本地 sidecar 运行层验证由你按既有启动脚本手动完成。
若后续引入依赖 `gemini_webapi` 等外网或本地凭证的集成资产，应继续按 opt-in 治理，不纳入默认稳定回归。
```
