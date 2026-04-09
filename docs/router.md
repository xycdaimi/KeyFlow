# KeyFlow 路由说明

## 总览

- 应用默认 API 前缀为 `/api`，可通过环境变量 `API_PREFIX` 覆盖。
- 健康检查接口不走 `/api` 前缀。
- **`/api/internal/*` 与全部 `/api/providers/*`、`/api/keys/*` 管理类接口**均需要请求头 `X-Internal-Key`，其值需与环境变量 `INTERNAL_API_KEY` 一致；缺失或不匹配时返回 `401`。
- 除上述范围外，本文档未列出的接口请以代码为准。

### HTTP 状态与 `detail` 约定

业务错误统一为 JSON：`{"detail": "<字符串码>"}`（与 FastAPI `HTTPException` 一致）。常见取值如下。

| 状态码 | 典型 `detail` | 说明 |
| --- | --- | --- |
| `401` | `invalid internal key` | `X-Internal-Key` 未传或与配置不一致 |
| `404` | `no_available_key` | 当前没有可分配的 Key（候选为空或租约层未分配到） |
| `404` | `key_not_found` | 指定 `key_id` 不存在，或与路径中的 `provider` 不匹配 |
| `404` | `provider_not_found` | 未注册的 `provider`（多见于创建/更新 Key） |
| `409` | `duplicate_credential` | 同一 `provider` 下已存在相同凭据内容 |
| `409` | `provider_not_ready` | 插件未就绪（如依赖未安装），无法创建/更新该供应商的 Key |
| `503` | `upstream_unreachable` | 创建/更新凭据时探测供应商根地址失败 |
| `422` | （结构体） | 请求体字段类型、必填项或枚举不合法时，由 FastAPI/Pydantic 返回标准校验 `detail`（多为数组） |

说明：`404` + `no_available_key` 表示**资源池层面**暂无可分配项，并非 REST 意义上的「URL 路径不存在」。

`GET /health` 不使用 Internal Key；依赖未就绪时返回 **`503`**，响应体为 `{"status":"degraded","checks":{...}}`，见下文。

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

**成功返回示例**（全部检查通过，HTTP `200`）

```json
{
  "status": "ok",
  "checks": {
    "app": { "status": "ok", "detail": null },
    "database": { "status": "ok", "detail": null },
    "redis": { "status": "ok", "detail": null }
  }
}
```

**降级示例**（任一项检查失败或未配置，HTTP **`503`**）

```json
{
  "status": "degraded",
  "checks": {
    "app": { "status": "ok", "detail": null },
    "database": { "status": "error", "detail": "检查数据库失败" },
    "redis": { "status": "error", "detail": "Connection refused" }
  }
}
```

`checks` 中各组件的 `detail` 随运行环境变化，以实际响应为准。

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
  "provider_model": "gpt-4o",
  "credential": {
    "api_key": "sk-test"
  }
}
```

说明：

- `model` 是调用方传入的规范模型名（canonical name）
- `provider_model` 是本次选中凭据对应的 provider 原生模型名；上游调用 provider API 时应使用该值

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "no_available_key"}`（当前无可分配 Key）
- `422`: 请求体校验失败（例如缺少 `provider`、类型错误）

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
  "provider_model": "openai/gpt-4o",
  "credential": {
    "api_key": "sk-or-best"
  }
}
```

说明：

- `provider`: 本次被选中凭证所属的 provider 名称
- `provider_model`: 本次被选中凭证对应的 provider 原生模型名（来自 alias 映射或直连匹配）
- `credential`: 该 provider 对应的认证凭据内容

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "no_available_key"}`
- `422`: 请求体校验失败（例如缺少 `model`）

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
- `422`: 请求体校验失败（例如缺少 `key_id`、`error_type`）

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
- `422`: 请求体校验失败（例如 `tokens_used` 为负数）

## 5. `POST /api/providers/{provider}/keys`

**功能**

为指定供应商新增一个凭据账号。创建时会先通过插件刷新可用性/容量缓存（`is_credential_available` / `get_capacity_signal`），再同步支持的模型列表（`fetch_models`），接口响应返回成功状态及新建 Key 的 `key_id`。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "provider_not_found"}`（`provider` 未注册）
- `409`: `{"detail": "duplicate_credential"}`（同供应商下凭据已存在）
- `409`: `{"detail": "provider_not_ready"}`（插件依赖未满足等）
- `503`: `{"detail": "upstream_unreachable"}`（探测供应商根地址失败）
- `422`: 请求体校验失败（例如缺少 `credential` 或结构不符合约定）

## 6. `GET /api/providers/{provider}/keys`

**功能**

列出某个供应商下的所有 Key。返回数组中的每一项只包含 `key_id`、凭据本身和 `status`；没有数据时返回空数组。若 `provider` 在库中无任何 Key，返回 **`[]`**（HTTP `200`），**不会**因此返回 `404`。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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
    "status": "available"
  }
]
```

**常见错误**

- `401`: `{"detail": "invalid internal key"}`

## 13. Status Contract

系统当前对外暴露的 `status` 一共有 7 个：

- `available`
- `rate_limited`
- `cooldown`
- `disabled_upstream`
- `disabled_admin`
- `disabled_report`
- `exhausted`

这些状态的写入来源必须区分清楚：

- 管理端 `PUT /api/keys/{key_id}` 只能写 `available`、`disabled_admin`
- 上游如果承担管理后台职责，可以通过管理端 `PUT /api/keys/{key_id}` 显式设置 `disabled_admin`
- 上游如果承担管理后台职责，也可以通过同一个接口把 `disabled_admin` 改回 `available`
- 上游网关不能通过管理端接口直接写 `rate_limited`、`cooldown`、`disabled_upstream`、`disabled_report`、`exhausted`
- 上游网关在运行期应通过 `POST /api/internal/report-error` 和 `POST /api/internal/report-success` 驱动运行态状态变化
- 后台刷新任务会通过插件 `is_credential_available` 与 `get_capacity_signal` 自动合成 `disabled_upstream`、`exhausted`

管理场景示例：

- 管理员手工禁用某个 key：`PUT /api/keys/{key_id}`，请求体 `{"status": "disabled_admin"}`
- 管理员恢复某个 key：`PUT /api/keys/{key_id}`，请求体 `{"status": "available"}`

推荐给上游网关的状态驱动方式：

- 请求成功后调用 `POST /api/internal/report-success`
- 命中限流后调用 `POST /api/internal/report-error`，`error_type=rate_limit`
- 明确额度耗尽后调用 `POST /api/internal/report-error`，`error_type=quota_exhausted`
- 明确该 key 应业务禁用后调用 `POST /api/internal/report-error`，`error_type=disabled`

不要把“管理禁用”与“运行态上报”混为一谈：

- `disabled_admin` 是管理动作
- `disabled_report` 是运行态上报动作
- `disabled_upstream` 是刷新探测动作

**空列表示例**

```json
[]
```

## 7. `GET /api/keys/{key_id}`

**功能**

通过 `key_id` 获取某个 Key 的凭据内容和状态，只返回这两个字段。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`

## 8. `PUT /api/keys/{key_id}`

**功能**

更新某个 Key 的凭据或状态。若更新了凭据，会先刷新可用性/容量缓存，再重新同步支持模型列表；接口响应只返回成功或失败状态。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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
- `status`: 可选，仅允许设置为 `available`、`disabled_admin`

**成功返回示例**

```json
{
  "status": "ok"
}
```

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`
- `404`: `{"detail": "provider_not_found"}`（Key 存在但关联的 provider 未注册等边界情况）
- `409`: `{"detail": "duplicate_credential"}`
- `409`: `{"detail": "provider_not_ready"}`
- `503`: `{"detail": "upstream_unreachable"}`（更新凭据时上游探测失败）
- `422`: 请求体校验失败（例如 `status` 只能为 `available` 或 `disabled_admin`）

## 9. `DELETE /api/keys/{key_id}`

**功能**

删除某个 Key，并从分配缓存中移除；接口响应只返回成功或失败状态。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

**路径参数**

- `key_id`: Key 唯一标识

**成功返回示例**

```json
{
  "status": "ok"
}
```

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`

## 10. `GET /api/providers/{provider}/keys/{key_id}/models`

**功能**

通过供应商名称和 `key_id` 获取该 Key 当前已同步的可用模型列表。接口只返回模型名称列表。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`（含 `key_id` 存在但该 Key 不属于路径中的 `provider`）

## 11. `GET /api/keys/{key_id}/explain`

**功能**

返回插件生成的安全摘要，用于后台展示。该接口返回内容由具体 provider 插件决定，但约定不能暴露原始 credential。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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

- `401`: `{"detail": "invalid internal key"}`
- `404`: `{"detail": "key_not_found"}`

说明：Key 存在但无对应插件时，HTTP 仍为 `200`，响应体可能为 `{"provider":"...","status":"no_plugin"}` 等，见上文「成功返回示例」说明。

## 12. `GET /api/providers`

**功能**

列出当前系统已注册的 provider 插件元信息，用于前端或运维侧决定可新增哪些类型的 Key。

**请求头**

```http
X-Internal-Key: <与 INTERNAL_API_KEY 一致>
```

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

**常见错误**

- `401`: `{"detail": "invalid internal key"}`
