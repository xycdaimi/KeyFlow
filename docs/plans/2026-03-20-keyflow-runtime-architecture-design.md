# KeyFlow Runtime Architecture Design

**Goal:** 为 `KeyFlow` 生成一张当前运行时部署视角的架构设计图，落盘到 `docs/keyflow.drawio`。

**Scope:** 聚焦当前实际运行形态与关键交互链路，不展开 DDD 分层实现细节。

## Design

- 图类型：运行时总览图
- 核心节点：上游调用方、`keyflow-api`、`keyflow-worker`、`PostgreSQL`、`Redis + Lua`、外部 `Provider APIs`
- 核心关系：
  - 上游通过内部接口调用 `keyflow-api`
  - `keyflow-api` 负责 `allocate / report / admin / health`
  - `keyflow-worker` 周期执行 `recover_cooldowns / refresh_keys`
  - `keyflow-api` 与 `keyflow-worker` 共享 `PostgreSQL` 和 `Redis`
  - `keyflow-worker` 访问外部 `Provider APIs` 获取模型、可用性与容量信息
  - `keyflow-api` 在新增或更新 key 时也可能触发 provider 插件同步

## Layout

- 使用一个 `same Pod, two containers` 容器包裹 `keyflow-api` 与 `keyflow-worker`
- 左侧放置上游系统，中间放置 KeyFlow 运行时，右侧放置外部 Provider
- 底部放置共享基础设施 `PostgreSQL` 与 `Redis + Lua`
- 连线使用正交折线，避免交叉

## Notes

- 图中明确标注 `Redis + Lua atomic allocation`
- 图中明确标注 Worker 无状态，仅依赖现有 PostgreSQL 与 Redis
- 图中明确标注本图表达的是 README 中当前架构，而非未来扩展架构
