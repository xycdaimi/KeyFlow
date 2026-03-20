# KeyFlow Runtime Architecture Diagram Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 产出一张反映当前 KeyFlow 运行时部署与关键交互关系的 draw.io 架构图。

**Architecture:** 采用运行时总览图，以 `same Pod, two containers` 为中心容器，展示 `keyflow-api`、`keyflow-worker`、共享的 `PostgreSQL` 与 `Redis + Lua`，以及上游调用方和外部 Provider APIs 的交互关系。图中只保留当前实现已存在的职责与链路，避免引入 README 未落地的扩展角色。

**Tech Stack:** draw.io XML, user-drawio-mcp, FastAPI, Redis, PostgreSQL

---

### Task 1: 整理图中节点与关系

**Files:**
- Modify: `README.md`
- Modify: `src/interfaces/api/app.py`
- Modify: `src/worker_main.py`
- Modify: `src/application/services/key_service.py`

**Step 1: 提取运行时节点**

确认图中包含以下节点：
- Upstream Services
- KeyFlow Pod
- keyflow-api
- keyflow-worker
- PostgreSQL
- Redis + Lua
- Provider APIs

**Step 2: 提取关键链路**

确认图中包含以下关系：
- Upstream -> API: allocate/report/admin/health
- API -> PostgreSQL: metadata persistence
- API -> Redis + Lua: atomic allocation and lease
- Worker -> PostgreSQL: load/update key state
- Worker -> Redis + Lua: refresh cache/sync score
- Worker -> Provider APIs: availability/capacity/models refresh
- API -> Provider APIs: create/update key sync

### Task 2: 生成 draw.io XML

**Files:**
- Modify: `docs/keyflow.drawio`

**Step 1: 设计布局**

使用容器表示 `same Pod, two containers`，确保 API 与 Worker 位于同一运行时边界内。

**Step 2: 编写 XML**

使用正交边、带标题的泳道容器和底部基础设施节点生成 `mxGraphModel`。

**Step 3: 写入文件**

将 XML 保存到 `docs/keyflow.drawio`。

### Task 3: 用 drawio-mcp 打开验证

**Files:**
- Modify: `docs/keyflow.drawio`

**Step 1: 调用 `open_drawio_xml`**

把生成的 XML 交给 `user-drawio-mcp.open_drawio_xml` 打开。

**Step 2: 验证可视化结构**

检查容器、节点、边是否正常显示，确认没有无效 edge geometry。

**Step 3: 必要时调整**

如果存在布局重叠或箭头不清晰，调整坐标与 waypoints 后重新写入。
