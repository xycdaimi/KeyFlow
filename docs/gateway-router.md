# KeyFlow Gateway 控制面路由说明

## 总览

本文档描述独立部署的 KeyFlow gateway 控制面服务。

gateway 只服务 ai_router 的管理面：

- 接收子节点主动注册和心跳。
- 保存子节点的 `node_id`、展示名、标签、节点地址和该节点的 Internal Key。
- 聚合各子节点的 provider 管理能力。
- 按 `node_id` 将凭证管理请求转发到对应子节点。

gateway 不参与运行面：

- 不提供 `allocate-key`。
- 不提供 `allocate-by-model`。
- 不接收 `report-error`。
- 不接收 `report-success`。
- 不保存凭证内容作为权威数据。

凭证分配和执行结果上报必须由执行器调用同出口 IP 的子节点完成。原因是账户池凭证与执行器出口 IP 必须一致，gateway 不能成为运行时中转层。

默认 API 前缀为 `/api/gateway`，由 `.env.gateway` 中的 `API_PREFIX` 配置。

## 鉴权

gateway 有两类请求头：

| 请求头 | 用途 | 对应环境变量 |
| --- | --- | --- |
| `X-Gateway-Register-Key` | 子节点注册和心跳 | `GATEWAY_REGISTER_KEY` |
| `X-Gateway-Internal-Key` | ai_router 管理面访问 gateway | `GATEWAY_INTERNAL_KEY` |

鉴权失败时返回：

```json
{"detail": "invalid gateway register key"}
```

或：

```json
{"detail": "invalid gateway internal key"}
```

## 路由清单

| 方法 | 路径 | 调用方 | 功能 |
| --- | --- | --- | --- |
| `POST` | `/api/gateway/nodes/register` | 子节点 | 注册或更新子节点资料 |
| `POST` | `/api/gateway/nodes/{node_id}/heartbeat` | 子节点 | 上报子节点心跳 |
| `GET` | `/api/gateway/nodes` | ai_router 管理面 | 列出已注册子节点 |
| `PATCH` | `/api/gateway/nodes/{node_id}` | ai_router 管理面 | 更新子节点展示名、标签或启用状态 |
| `GET` | `/api/gateway/capabilities` | ai_router 管理面 | 聚合各子节点 provider 管理能力 |
| `POST` | `/api/gateway/nodes/{node_id}/providers/{provider}/keys` | ai_router 管理面 | 在指定子节点指定 provider 创建凭证 |
| `GET` | `/api/gateway/nodes/{node_id}/providers/{provider}/keys` | ai_router 管理面 | 列出指定子节点指定 provider 的凭证，包含 `max_concurrent_uses` |
| `GET` | `/api/gateway/nodes/{node_id}/keys/{key_id}` | ai_router 管理面 | 查询指定子节点上的凭证详情，包含 `max_concurrent_uses` |
| `PUT` | `/api/gateway/nodes/{node_id}/keys/{key_id}` | ai_router 管理面 | 更新指定子节点上的凭证、管理状态或 `max_concurrent_uses` |
| `PUT` | `/api/gateway/nodes/{node_id}/keys/{key_id}/pool` | ai_router 管理面 | 迁移指定子节点上的凭证池子 |
| `DELETE` | `/api/gateway/nodes/{node_id}/keys/{key_id}` | ai_router 管理面 | 删除指定子节点上的凭证 |
| `GET` | `/api/gateway/nodes/{node_id}/providers/{provider}/keys/{key_id}/models` | ai_router 管理面 | 查询指定凭证已同步模型 |
| `GET` | `/api/gateway/nodes/{node_id}/keys/{key_id}/explain` | ai_router 管理面 | 查询指定凭证安全摘要 |

## 1. `POST /api/gateway/nodes/register`

**功能**

子节点启动后主动向 gateway 注册。该接口是 upsert 语义：同一个 `node_id` 再次注册会更新节点地址、Internal Key、展示名、标签和版本。

**请求头**

```http
X-Gateway-Register-Key: <与 GATEWAY_REGISTER_KEY 一致>
```

**请求体**

```json
{
  "node_id": "node-shanghai-01",
  "display_name": "上海节点 01",
  "base_url": "http://keyflow-node-01:8000",
  "internal_key": "child-node-internal-key",
  "tags": ["shanghai", "vip"],
  "version": "0.1.0"
}
```

字段说明：

- `node_id`: 子节点全局唯一标识。
- `display_name`: 管理界面展示名。
- `base_url`: gateway 访问该子节点的 HTTP base URL。
- `internal_key`: gateway 转发管理请求到子节点时使用的 `X-Internal-Key`。
- `tags`: 节点标签，用于管理界面筛选。
- `version`: 子节点版本，可为空。

**成功返回**

```json
{
  "status": "registered",
  "node_id": "node-shanghai-01"
}
```

**常见错误**

- `401`: `{"detail": "invalid gateway register key"}`
- `422`: 请求体字段缺失、类型错误，或 `base_url` 非法

## 2. `POST /api/gateway/nodes/{node_id}/heartbeat`

**功能**

子节点周期性上报心跳。gateway 使用最近心跳时间判断节点 `online` / `stale`。

如果心跳返回 `404 node_not_found`，子节点应重新调用注册接口。

**请求头**

```http
X-Gateway-Register-Key: <与 GATEWAY_REGISTER_KEY 一致>
```

**请求体**

```json
{
  "version": "0.1.0",
  "runtime_status": "ok"
}
```

**成功返回**

```json
{
  "status": "ok"
}
```

**常见错误**

- `401`: `{"detail": "invalid gateway register key"}`
- `404`: `{"detail": "node_not_found"}`
- `422`: 请求体字段类型错误

## 3. `GET /api/gateway/nodes`

**功能**

列出 gateway 当前保存的子节点资料。该接口不探测子节点 provider，只返回注册、心跳和最近一次探测结果。

**请求头**

```http
X-Gateway-Internal-Key: <与 GATEWAY_INTERNAL_KEY 一致>
```

**成功返回**

```json
[
  {
    "node_id": "node-shanghai-01",
    "display_name": "上海节点 01",
    "tags": ["shanghai", "vip"],
    "enabled": true,
    "status": "online",
    "registered_at": "2026-05-21T10:00:00",
    "last_heartbeat_at": "2026-05-21T10:01:00",
    "last_runtime_status": "ok",
    "last_probe_status": "healthy",
    "last_probe_at": "2026-05-21T10:01:05",
    "last_probe_error": null,
    "has_internal_key": true
  }
]
```

`status` 取值：

- `unknown`: 已注册但尚无心跳。
- `online`: 最近心跳未超时。
- `stale`: 最近心跳已超时。
- `disabled`: 管理端禁用了该节点。

**常见错误**

- `401`: `{"detail": "invalid gateway internal key"}`

## 4. `PATCH /api/gateway/nodes/{node_id}`

**功能**

更新 gateway 侧的子节点管理元数据。该接口不修改子节点本地配置。

**请求头**

```http
X-Gateway-Internal-Key: <与 GATEWAY_INTERNAL_KEY 一致>
```

**请求体**

```json
{
  "display_name": "上海节点 01",
  "tags": ["shanghai", "vip"],
  "enabled": true
}
```

字段均可选。未传字段保持原值。

**成功返回**

返回更新后的节点对象，结构同 `GET /api/gateway/nodes` 的单个元素。

**常见错误**

- `401`: `{"detail": "invalid gateway internal key"}`
- `404`: `{"detail": "node_not_found"}`
- `422`: 请求体字段类型错误

## 5. `GET /api/gateway/capabilities`

**功能**

聚合所有已启用子节点的 provider 管理能力。gateway 会调用子节点的 `GET /api/providers`，并缓存短时间探测结果。

该接口按节点降级：某个子节点不可达时，只将该节点标记为 `unreachable` 或 `timeout`，不会让其他节点失败。

**请求头**

```http
X-Gateway-Internal-Key: <与 GATEWAY_INTERNAL_KEY 一致>
```

**成功返回**

```json
{
  "nodes": [
    {
      "node_id": "node-shanghai-01",
      "display_name": "上海节点 01",
      "status": "healthy",
      "providers": [
        {
          "name": "openai",
          "available": true,
          "auth_type": "bearer_api_key",
          "model_source": "remote",
          "credential_hint": "{\"api_key\": \"sk-...\"} (OpenAI API Key, Bearer token)",
          "actions": ["create_key", "list_keys"]
        }
      ],
      "error": null
    },
    {
      "node_id": "node-us-01",
      "display_name": "美国节点 01",
      "status": "timeout",
      "providers": [],
      "error": "node_timeout"
    }
  ]
}
```

节点探测 `status` 取值：

- `healthy`
- `degraded`
- `unreachable`
- `timeout`
- `disabled`
- `unknown`

**常见错误**

- `401`: `{"detail": "invalid gateway internal key"}`

## 6. 凭证管理转发接口

gateway 的凭证管理接口都带 `node_id`。ai_router 管理界面必须先选择子节点，再选择该子节点的 provider 或 key。

gateway 转发时会：

- 根据 `node_id` 查询子节点。
- 检查节点是否启用。
- 使用注册时保存的 `internal_key` 调用子节点管理接口。
- 原样透传子节点返回的业务状态码和响应体。
- 凭证管理请求体使用透传模型，不裁剪子节点支持的字段；例如 `max_concurrent_uses` 会原样转发给子节点账户池调度服务。

gateway 不会转发以下运行面接口：

- `/api/internal/allocate-key`
- `/api/internal/allocate-by-model`
- `/api/internal/report-error`
- `/api/internal/report-success`

### 6.1 `POST /api/gateway/nodes/{node_id}/providers/{provider}/keys`

在指定子节点的指定 provider 创建凭证。

请求体和响应体与子节点 `POST /api/providers/{provider}/keys` 一致。

示例：

```json
{
  "credential": {
    "api_key": "sk-new"
  },
  "pool": "vip",
  "max_concurrent_uses": 2
}
```

字段语义与子节点 `POST /api/providers/{provider}/keys` 一致：

- `credential`: 必填，provider 私有认证载荷。
- `pool`: 可选，Key 池级别；默认 `default`。
- `max_concurrent_uses`: 可选，该凭证同一时间允许被分配出去的最大使用数；整数，最小值为 `1`，默认 `1`。

### 6.2 `GET /api/gateway/nodes/{node_id}/providers/{provider}/keys`

列出指定子节点指定 provider 下的凭证。

响应体与子节点 `GET /api/providers/{provider}/keys` 一致。

响应项包含 `max_concurrent_uses`：

```json
[
  {
    "key_id": "key-1",
    "credential": {
      "api_key": "sk-test"
    },
    "pool": "default",
    "max_concurrent_uses": 2,
    "status": "available"
  }
]
```

### 6.3 `GET /api/gateway/nodes/{node_id}/keys/{key_id}`

查询指定子节点上的凭证详情。

响应体与子节点 `GET /api/keys/{key_id}` 一致。

响应包含 `max_concurrent_uses`：

```json
{
  "credential": {
    "api_key": "sk-test"
  },
  "pool": "default",
  "max_concurrent_uses": 2,
  "status": "available"
}
```

### 6.4 `PUT /api/gateway/nodes/{node_id}/keys/{key_id}`

更新指定子节点上的凭证、管理状态或凭证级并发使用上限。

请求体和响应体与子节点 `PUT /api/keys/{key_id}` 一致。

可单独修改凭证级并发使用上限：

```json
{
  "max_concurrent_uses": 3
}
```

也可以与凭证或管理状态一起提交：

```json
{
  "credential": {
    "api_key": "sk-updated"
  },
  "status": "available",
  "max_concurrent_uses": 3
}
```

### 6.5 `PUT /api/gateway/nodes/{node_id}/keys/{key_id}/pool`

迁移指定子节点上的凭证池子。

请求体和响应体与子节点 `PUT /api/keys/{key_id}/pool` 一致。

### 6.6 `DELETE /api/gateway/nodes/{node_id}/keys/{key_id}`

删除指定子节点上的凭证。

响应体与子节点 `DELETE /api/keys/{key_id}` 一致。

### 6.7 `GET /api/gateway/nodes/{node_id}/providers/{provider}/keys/{key_id}/models`

查询指定子节点上某个凭证已同步的模型列表。

响应体与子节点 `GET /api/providers/{provider}/keys/{key_id}/models` 一致。

### 6.8 `GET /api/gateway/nodes/{node_id}/keys/{key_id}/explain`

查询指定子节点上某个凭证的安全摘要。

响应体与子节点 `GET /api/keys/{key_id}/explain` 一致。

### 转发接口常见错误

gateway 自己产生的错误：

- `401`: `{"detail": "invalid gateway internal key"}`
- `404`: `{"detail": "node_not_found"}`
- `409`: `{"detail": "node_disabled"}`
- `503`: `{"detail": "node_unreachable"}`
- `504`: `{"detail": "node_timeout"}`
- `422`: gateway 路径或请求体校验失败

子节点产生的业务错误会被 gateway 原样返回，例如：

- `400`: `{"detail": "<provider 校验错误文本>"}`
- `404`: `{"detail": "provider_not_found"}`
- `404`: `{"detail": "key_not_found"}`
- `409`: `{"detail": "duplicate_credential"}`
- `409`: `{"detail": "provider_not_ready"}`
- `409`: `{"detail": "key_runtime_locked"}`
- `503`: `{"detail": "upstream_unreachable"}`

## 管理界面调用顺序

ai_router 管理界面应按以下顺序使用 gateway：

1. 调用 `GET /api/gateway/nodes` 展示子节点列表和在线状态。
2. 调用 `GET /api/gateway/capabilities` 展示每个子节点可管理的 provider。
3. 管理员选择一个子节点。
4. 管理员选择该子节点下的 provider。
5. 通过 `/api/gateway/nodes/{node_id}/...` 凭证管理接口对该子节点执行增删改查。

运行时执行器不走 gateway：

1. 执行器选择与自己同出口 IP 的子节点。
2. 执行器调用该子节点的 `/api/internal/allocate-key` 或 `/api/internal/allocate-by-model`。
3. 执行器调用 provider。
4. 执行器将结果上报给同一个子节点的 `/api/internal/report-success` 或 `/api/internal/report-error`。
