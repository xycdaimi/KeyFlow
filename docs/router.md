# KeyFlow 路由说明

## 总览

- 应用默认 API 前缀为 `/api`，可通过环境变量 `API_PREFIX` 覆盖。
- 健康检查接口不走 `/api` 前缀。
- `/api/internal/*` 路由需要请求头 `X-Internal-Key`，其值需与 `INTERNAL_API_KEY` 一致。
- 管理类接口当前代码中未额外做鉴权，调用方需要自行放在受保护环境中。

## 路由清单

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查，供 Docker / K8s 探活 |
| `POST` | `/api/internal/allocate-key` | 为指定供应商分配一个当前可用的 Key |
| `POST` | `/api/internal/allocate-by-model` | 仅按模型名跨 provider 选择当前得分最高的可用 Key |
| `POST` | `/api/internal/report-error` | 上报一次失败结果，驱动 Key 状态机更新 |
| `POST` | `/api/internal/report-success` | 上报一次成功结果并累计 token 使用量 |
| `POST` | `/api/providers/{provider}/keys` | 为某个供应商新增一个凭据，返回成功状态及 `key_id` |
| `GET` | `/api/providers/{provider}/keys` | 列出某个供应商下的所有 Key，只返回 `key_id`、`credential`、`status` |
| `GET` | `/api/keys/{key_id}` | 通过 `key_id` 获取凭据内容与状态 |
| `PUT` | `/api/keys/{key_id}` | 更新某个 Key 的凭据或状态，返回成功或失败 |
| `DELETE` | `/api/keys/{key_id}` | 删除某个 Key，返回成功或失败 |
| `GET` | `/api/providers/{provider}/keys/{key_id}/models` | 通过供应商和 `key_id` 获取可用模型名称列表 |
| `GET` | `/api/keys/{key_id}/explain` | 返回插件生成的安全摘要，不暴露原始凭据 |
| `GET` | `/api/providers` | 列出系统中已注册的供应商插件元信息 |

## 1. `GET /health`

**功能**

返回服务存活状态。

**输入**

无路径参数、无请求体、无鉴权要求。

**成功返回示例**

```json
{
  "status": "ok"
}
```

## 2. `POST /api/internal/allocate-key`

**功能**

按供应商和可选模型，从池中分配一个当前可用的 Key，并返回该 Key 的凭据内容。

**请求头**

```http
X-Internal-Key: dev-internal-key
```

**请求体**

```json
{
  "provider": "openai",
  "model": "gpt-4o"
}
```

字段说明：

- `provider`: 必填，供应商标识，例如 `openai`、`anthropic`、`gemini`、`openrouter`、`gemini-web-proxy`
- `model`: 可选，指定期望模型；若已同步模型列表，会先做本地预过滤

**成功返回示例**

```json
{
  "key_id": "key-1",
  "credential": {
    "api_key": "sk-test"
  }
}
```

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "no_available_key"}`

## 2.1 `POST /api/internal/allocate-by-model`

**功能**

只传模型名，从多个 provider 的候选 Key 中统一筛选并评分，返回当前得分最高且可分配的 Key。

与 `POST /api/internal/allocate-key` 的区别：

- `allocate-key`：调用方先指定 `provider`，系统只在该 provider 内选优
- `allocate-by-model`：调用方只指定 `model`，系统在所有支持该模型的 provider 中跨 provider 选优

**请求头**

```http
X-Internal-Key: dev-internal-key
```

**请求体**

```json
{
  "model": "gpt-4o"
}
```

字段说明：

- `model`: 必填，目标模型名称；系统会在所有 provider 的 Key 中筛选支持该模型且当前可用的候选

**成功返回示例**

```json
{
  "key_id": "key-openrouter",
  "provider": "openrouter",
  "credential": {
    "api_key": "sk-or-best"
  }
}
```

说明：

- `provider`: 本次被选中凭证所属的 provider 名称
- `credential`: 该 provider 对应的认证凭据内容

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "no_available_key"}`

## 3. `POST /api/internal/report-error`

**功能**

上报某个已分配 Key 的失败结果，触发状态机更新，并释放该 Key 的分配租约。

**请求头**

```http
X-Internal-Key: dev-internal-key
```

**请求体**

```json
{
  "key_id": "key-1",
  "error_type": "network_timeout"
}
```

字段说明：

- `key_id`: 必填，待上报的 Key ID
- `error_type`: 必填，错误类型字符串，例如 `network_timeout`、`rate_limit`、`auth_error`

**成功返回示例**

```json
{
  "id": "key-1",
  "provider": "openai",
  "status": "available",
  "supported_models": [
    "gpt-4o",
    "gpt-4o-mini"
  ],
  "quota_used": 0,
  "last_used_at": "2026-03-19T10:00:00Z",
  "cooldown_until": null
}
```

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`

## 4. `POST /api/internal/report-success`

**功能**

上报某个已分配 Key 的成功结果，累计 token 使用量，并释放该 Key 的分配租约。

**请求头**

```http
X-Internal-Key: dev-internal-key
```

**请求体**

```json
{
  "key_id": "key-1",
  "tokens_used": 12
}
```

字段说明：

- `key_id`: 必填，待上报的 Key ID
- `tokens_used`: 可选，默认 `0`，最小值为 `0`

**成功返回示例**

```json
{
  "id": "key-1",
  "provider": "openai",
  "status": "available",
  "supported_models": [
    "gpt-4o",
    "gpt-4o-mini"
  ],
  "quota_used": 12,
  "last_used_at": "2026-03-19T10:00:00Z",
  "cooldown_until": null
}
```

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`

## 5. `POST /api/providers/{provider}/keys`

**功能**

为指定供应商新增一个凭据账号。创建时会调用对应插件同步支持的模型列表，接口响应返回成功状态及新建 Key 的 `key_id`。

**路径参数**

- `provider`: 供应商标识，例如 `openai`

**请求体**

```json
{
  "credential": {
    "api_key": "sk-new"
  }
}
```

不同供应商的 `credential` 结构示例：

- `openai`: `{"api_key": "sk-..."}`
- `anthropic`: `{"api_key": "sk-ant-..."}`
- `gemini`: `{"api_key": "AIza..."}`
- `openrouter`: `{"api_key": "sk-or-..."}`
- `gemini-web-proxy`: `{"secure_1psid": "...", "secure_1psidts": "..."}`

**成功返回示例**

```json
{
  "status": "ok",
  "key_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

## 6. `GET /api/providers/{provider}/keys`

**功能**

列出某个供应商下的所有 Key。返回数组中的每一项只包含 `key_id`、凭据本身和 `status`；没有数据时返回空数组。

**路径参数**

- `provider`: 供应商标识，例如 `openai`

**成功返回示例**

```json
[
  {
    "key_id": "key-1",
    "credential": {
      "api_key": "sk-test"
    },
    "status": "available"
  },
  {
    "key_id": "0dd6d8f6-0cff-4023-b30f-a7dca90995b2",
    "credential": {
      "api_key": "sk-new"
    },
    "status": "available",
  }
]
```

**空列表示例**

```json
[]
```

## 7. `GET /api/keys/{key_id}`

**功能**

通过 `key_id` 获取某个 Key 的凭据内容和状态，只返回这两个字段。

**路径参数**

- `key_id`: Key 唯一标识

**成功返回示例**

```json
{
  "credential": {
    "api_key": "sk-updated"
  }
  "status": "available"
}
```

**常见错误**

- `404`: `{"detail": "key_not_found"}`

## 8. `PUT /api/keys/{key_id}`

**功能**

更新某个 Key 的凭据或状态。若更新了凭据，会重新同步支持模型列表；接口响应只返回成功或失败状态。

**路径参数**

- `key_id`: Key 唯一标识

**请求体**

```json
{
  "credential": {
    "api_key": "sk-updated"
  },
  "status": "available"
}
```

字段说明：

- `credential`: 可选，新的结构化凭据
- `status`: 可选，可设置为 `available`、`rate_limited`、`cooldown`、`disabled`、`exhausted`

**成功返回示例**

```json
{
  "status": "ok"
}
```

**常见错误**

- `404`: `{"detail": "key_not_found"}`

## 9. `DELETE /api/keys/{key_id}`

**功能**

删除某个 Key，并从分配缓存中移除；接口响应只返回成功或失败状态。

**路径参数**

- `key_id`: Key 唯一标识

**成功返回示例**

```json
{
  "status": "ok"
}
```

**常见错误**

- `404`: `{"detail": "key_not_found"}`

## 10. `GET /api/providers/{provider}/keys/{key_id}/models`

**功能**

通过供应商名称和 `key_id` 获取该 Key 当前已同步的可用模型列表。接口只返回模型名称列表。

**路径参数**

- `provider`: 供应商标识，例如 `openai`
- `key_id`: Key 唯一标识

**成功返回示例**

```json
{
  "models": [
    "gpt-4o",
    "gpt-4o-mini"
  ]
}
```

**常见错误**

- `404`: `{"detail": "key_not_found"}`

## 11. `GET /api/keys/{key_id}/explain`

**功能**

返回插件生成的安全摘要，用于后台展示。该接口返回内容由具体 provider 插件决定，但约定不能暴露原始 credential。

**路径参数**

- `key_id`: Key 唯一标识

**成功返回示例**

以下示例为 `openrouter` 插件可能返回的结构：

```json
{
  "provider": "openrouter",
  "status": "ok",
  "model_source": "remote",
  "auth_type": "bearer_api_key",
  "credential_hint": "sk-or-se***",
  "is_free_tier": false,
  "remaining_usd": 8.5,
  "label": "test-key"
}
```

说明：

- 返回字段是插件定义的动态结构，不同 provider 可能不同
- 兜底情况下也可能返回 `{"provider": "...", "status": "no_plugin"}` 或插件错误摘要

**常见错误**

- `404`: `{"detail": "key_not_found"}`

## 12. `GET /api/providers`

**功能**

列出当前系统已注册的 provider 插件元信息，用于前端或运维侧决定可新增哪些类型的 Key。

当前代码里注册的 provider 为：

- `openai`
- `anthropic`
- `gemini`
- `openrouter`
- `gemini-web-proxy`

**成功返回示例**

```json
[
  {
    "name": "openai",
    "description": "OpenAI 官方 API（api.openai.com）。可用性取决于 API Key 是否有效且账户余额大于 0。",
    "auth_type": "bearer_api_key",
    "credential_hint": "{\"api_key\": \"sk-...\"}（OpenAI API Key，Bearer 令牌）",
    "model_source": "remote",
    "available": true
  },
  {
    "name": "gemini-web-proxy",
    "description": "通过 gemini-webapi 库以浏览器 Cookie 方式访问 Gemini Web 界面（非官方）。需要额外安装 gemini-webapi 依赖（pip install gemini-webapi）。可用性取决于 Cookie 是否仍有效，Cookie 会定期轮换，失效后需手动更新。",
    "auth_type": "cookie",
    "credential_hint": "{\"secure_1psid\": \"...\", \"secure_1psidts\": \"...\"}",
    "model_source": "static",
    "available": false
  }
]
```
