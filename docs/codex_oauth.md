# Codex OAuth 与上游调用流程说明

本文档梳理 **OpenAI Codex（ChatGPT 后端 Codex API）** 在开源项目 [AIClient-2-API-main](../AIClient-2-API-main/) 中的完整链路，便于在 keyflow 中实现「上游注入 JSON 凭证 → 刷新 → 列模型 → 调模型 → 查用量」的对等行为。  
实现细节以该仓库源码为准，路径均相对于 `AIClient-2-API-main/`。

---

## 1. 总览

| 环节 | 作用 | 主要源码 |
|------|------|----------|
| OAuth / 凭证落盘 | 浏览器授权或粘贴 Token，得到可持久化的 JSON | `src/auth/codex-oauth.js` |
| 凭证加载与刷新 | 读文件、判断过期、`refresh_token` 换新 token 并写回 | `src/providers/openai/codex-core.js` |
| 模型列表 | 返回内置模型 ID 列表（非上游动态 discovery） | `src/providers/openai/codex-core.js`、`src/providers/provider-models.js` |
| 模型调用 | `POST .../codex/responses`，SSE 流式或非流式聚合 | `src/providers/openai/codex-core.js` |
| 协议转换 | 客户端 OpenAI/Responses 等与 Codex 事件互转 | `src/converters/strategies/CodexConverter.js` |
| 用量额度 | `GET .../wham/usage`，解析 `rate_limit` 并归一化 | `src/providers/openai/codex-core.js`、`src/services/usage-service.js` |
| 号池 | 多账号通过 `CODEX_OAUTH_CREDS_FILE_PATH` 等与 Pool 绑定 | `src/providers/provider-pool-manager.js`、`src/services/service-manager.js` |

---

## 2. OAuth 手动授权流程

### 2.1 配置常量

定义于 `src/auth/codex-oauth.js` 的 `CODEX_OAUTH_CONFIG`：

- **client_id**：`app_EMoamEEZ73f0CkXaXp7hrann`（与官方 Codex CLI 一致）
- **authorize**：`https://auth.openai.com/oauth/authorize`
- **token**：`https://auth.openai.com/oauth/token`
- **redirect_uri**：`http://localhost:1455/auth/callback`
- **本地回调端口**：`1455`（需未被占用）
- **scope**：`openid email profile offline_access`

OAuth 请求走 **PKCE**：

1. 生成 `code_verifier`（96 字节随机 → base64url）
2. `code_challenge` = SHA256(verifier) 的 base64url
3. 授权 URL 携带：`response_type=code`、`code_challenge`、`code_challenge_method=S256`、`state`、`prompt=login`、`id_token_add_organizations=true`、`codex_cli_simplified_flow=true`

### 2.2 本地回调服务器

- 在 `1455` 端口启动 HTTP 服务，监听路径 `/auth/callback`
- 成功时 query 带 `code`、`state`；失败时带 `error`、`error_description`
- Web UI 流程中还会把 `state` 作为 session id，配合轮询/事件完成后续换 token（见 `handleCodexOAuth`）

### 2.3 用授权码换 Token

`POST https://auth.openai.com/oauth/token`  

`Content-Type: application/x-www-form-urlencoded`，字段示例：

- `grant_type=authorization_code`
- `client_id=...`
- `code=...`
- `redirect_uri=...`（必须与授权时一致）
- `code_verifier=...`（PKCE）

响应中包含至少：`access_token`、`id_token`、`refresh_token`、`expires_in`（具体以 OpenAI 返回为准）。

### 2.4 从 id_token 解析账户信息

对 **id_token**（JWT）解析 payload（base64url 第二段）：

- **email**：`claims.email`
- **account_id**：优先 `claims['https://api.openai.com/auth'].chatgpt_account_id`，否则退回 `claims.sub`

该 `account_id` 后续作为请求头 **`chatgpt-account-id`** 使用，与 Bearer `access_token` 配对。

### 2.5 落盘凭据 JSON 结构（CLIProxyAPI 兼容形态）

OAuth 成功或批量导入后，保存的对象字段如下（与 `completeOAuthFlow` / `batchImportCodexTokensStream` 一致）：

| 字段 | 说明 |
|------|------|
| `id_token` | OpenID id_token（JWT） |
| `access_token` | 访问 ChatGPT/Codex API 的 Bearer |
| `refresh_token` | 刷新用（建议始终保存） |
| `account_id` | 见上，对应请求头 |
| `email` | 邮箱，用于默认文件名等 |
| `type` | 固定 `'codex'` |
| `last_refresh` | ISO 时间，上次刷新/导入时间 |
| `expired` | **access_token 过期时间**的 ISO 字符串（由 `expires_in` 推算，默认缺省按 3600 秒） |

保存位置：

- 若配置 **`CODEX_OAUTH_CREDS_FILE_PATH`**：写入该路径
- 否则：`configs/codex/{timestamp}_codex-{email}.json`（权限建议 `0o600`）

号池场景下通常为 **每个账号一条配置**，指向各自的 `CODEX_OAUTH_CREDS_FILE_PATH`。

---

## 3. 不跑 OAuth：仅注入 JSON（批量导入）

`batchImportCodexTokensStream`（`codex-oauth.js`）约定：

- 每个元素必须含 **`access_token`** 与 **`id_token`**
- 可选：`refresh_token`、`expires_in`
- 用 `id_token` 解析得到 `account_id`、`email`，再组装与上表相同的凭据对象并 `saveCredentials`
- 可去重：同一 `account_id` 或同一 `refresh_token` 已存在则跳过（可配置跳过检查）

这与 Web UI 文案一致：从浏览器或 CLI 复制的 Token JSON 可直接导入。

---

## 4. Token 刷新

### 4.1 接口

`POST https://auth.openai.com/oauth/token`

- `grant_type=refresh_token`
- `client_id=...`
- `refresh_token=...`

### 4.2 刷新后对象

与 OAuth 成功类似，再次解析新 `id_token` 更新 `account_id`、`email`，并写回 **同一凭据文件**（`codex-core.js` 中通过 `credsPath` / `getCredentialsPath()` 保证写回路径一致）。

### 4.3 触发时机（codex-core）

- **`isExpiryDateNear()`**：在过期前约 **20 分钟**（`nearMinutes = 20`）视为临近过期，会打日志并返回 true
- 临近过期：**异步**标记号池需刷新 `markProviderNeedRefresh`（不阻塞当前请求）
- 无 token / 强制刷新：`initializeAuth(forceRefresh)` 会同步 `refreshAccessToken()`
- **401**：模型请求或用量请求返回 401 时，触发后台刷新并打上换号/不健康标记（`shouldSwitchCredential` 等），由 Pool 切换账号

封装重试：`refreshCodexTokensWithRetry`（默认最多 3 次，指数退避）。

---

## 5. 模型列表获取

Codex 在 AIClient 中 **不向 Codex 上游请求「动态模型 discovery」**，而是：

1. 在 `src/providers/provider-models.js` 中为 `'openai-codex-oauth'` 配置一组 **静态 model id**（如 `gpt-5`、`gpt-5-codex-mini` 等，随项目版本迭代）
2. `CodexApiService.listModels()`（`codex-core.js`）在此基础上再为每个模型名生成一个带 **`-fast`** 后缀的别名（去重后合并），用于映射 **priority 档位**（见下节）

返回形态为 OpenAI 风格：`{ object: 'list', data: [ { id, object: 'model', created, owned_by: 'openai' }, ... ] }`。

**结论**：自建插件若要对齐行为，需维护一份 **可配置的模型 ID 列表**；若需与上游完全一致，应以 AIClient 的 `provider-models` 或你方产品约定为准。

---

## 6. 模型调用

### 6.1 上游 Base URL

- 默认 **`https://chatgpt.com/backend-api/codex`**
- 可通过配置 **`CODEX_BASE_URL`** 覆盖（末尾不要重复路径片段）

### 6.2 HTTP

- **URL**：`POST {CODEX_BASE_URL}/responses`
- **流式**：`Accept: text/event-stream`，axios `responseType: 'stream'`，按行解析 `data: {...}` SSE
- **非流式**：仍请求 SSE，但 `responseType: 'text'`，再在本地 `parseNonStreamResponse` 里拼出完整 `response.completed` 事件

### 6.3 请求头（与官方 CLI 行为对齐）

核心字段（`buildHeaders`，`codex-core.js`）：

- `authorization: Bearer {access_token}`
- `chatgpt-account-id: {account_id}`
- `content-type: application/json`
- `version: 0.111.0`（代码内常量 `CODEX_VERSION`，可能随项目更新）
- `x-codex-beta-features: powershell_utf8`
- `x-oai-web-search-eligible: true`
- `user-agent: codex_cli_rs/{CODEX_VERSION} (Windows 10.0.26100; x86_64) WindowsTerminal`
- `originator: codex_cli_rs`
- `host: chatgpt.com`
- `Connection: Keep-Alive`

若存在 **`prompt_cache_key`**（见下），还会设置：

- `Conversation_id: {prompt_cache_key}`
- `Session_id: {prompt_cache_key}`

### 6.4 请求体要点（`prepareRequestBody`）

在传入的 OpenAI Responses 风格 body 上加工：

- 从 `metadata` 取会话维度：`session_id` → `conversation_id` → `user_id` → `'default'`
- **模型名 `-fast` 后缀**：表示使用 **priority** 档位  
  - 上游真实 `model` 字段会 **去掉 `-fast`**
  - 默认 `service_tier`：`-fast` 为 `priority`，否则为 `default`；非 `priority` 时 **删除** `service_tier` 字段
  - 默认 `reasoning.effort`：`-fast` 为 `xhigh`，否则沿用请求内已有或 `medium`
- **`prompt_cache_key`**：按会话 key 缓存一个 UUID（默认 1 小时过期），用于多轮对话对齐；`sessionId === 'default'` 时用 `{model}-default` 作 key 前缀以隔离
- **`stream`**：与调用方式一致
- 移除 `metadata`，避免原样透传上游

### 6.5 SSE 事件与非流式聚合

流式：逐行解析 `data: ` 后 JSON，交由上层 Converter 转成 OpenAI/Gemini/Claude 等协议。

非流式：`parseNonStreamResponse` 扫描 SSE 文本，关注例如：

- `response.output_item.added`
- `response.output_text.delta` / `response.output_text.done`
- `response.completed`

最终必须出现 **`response.completed`**，否则视为流异常。

### 6.6 适配器入口

`src/providers/adapter.js` 中 `CodexApiServiceAdapter` 将 `generateContent` / `generateContentStream` / `listModels` / `getUsageLimits` 接到统一服务层；对外请求体往往先经 **CodexConverter** 等转为 Responses 形态再进入 `CodexApiService`。

---

## 7. 用量额度（WHAM）

### 7.1 请求

- **GET** `https://chatgpt.com/backend-api/wham/usage`
- 头与模型调用类似：`Authorization: Bearer {access_token}`、`chatgpt-account-id: {account_id}`、`user-agent` 等与 CLI 一致、`host: chatgpt.com` 等

### 7.2 响应与归一化（`getUsageLimits`）

上游 JSON 中主要使用 **`rate_limit`**：

- **`primary_window`**（主窗口）：含 `used_percent`、`reset_at`（Unix 秒）等
- 可选 **`secondary_window`**

AIClient 归一化为内部通用结构：

```text
{
  lastUpdated: <毫秒时间戳>,
  models: {
    "default": {
      remaining: <0~1 小数，表示剩余配额比例>,
      resetTime: <ISO 字符串或 null>,
      resetTimeRaw: <Unix 秒，与 primary_window.reset_at 对应>
    }
  },
  raw: {
    planType: <来自 plan_type>,
    rateLimit: <原始 rate_limit 对象>,
    codeReviewRateLimit: ...,
    credits: ...
  }
}
```

**剩余比例计算**（主窗口）：

```text
remaining = 1 - (primary_window.used_percent / 100)
```

并 `clamp` 到 `[0, 1]`。当前实现把 **`default`** 作为统一模型 key（未按具体 model id 拆分多条额度）。

### 7.3 UI 展示层（`formatCodexUsage`）

`src/services/usage-service.js` 中 `formatCodexUsage` 将上述结构转为仪表盘用的 `usageBreakdown` 等：

- 将 `remaining`（0~1）转为 **已用百分比** `currentUsage = round((1 - remaining) * 100)`，`usageLimit = 100`
- 展示 `planType`、重置时间等

**注意**：展示代码中部分字段名可能与 `raw.rateLimit` 内 **蛇形命名**（如 `primary_window`）不一致，若自行对接前端，应以 `getUsageLimits` 返回的 **`models` + `raw`** 为准做解析。

### 7.4 401 处理

与模型调用相同：401 时触发后台刷新与换号标记，不单独赘述。

---

## 8. 号池与配置键

- 提供商标识：`MODEL_PROVIDER.CODEX_API` → `'openai-codex-oauth'`（`src/utils/constants.js`）
- 号池默认模型等：`src/providers/provider-pool-manager.js` 中 `openai-codex-oauth` 条目
- 凭据路径映射：`src/services/service-manager.js` 中 `'openai-codex-oauth': 'CODEX_OAUTH_CREDS_FILE_PATH'`

每个池内实例通常有独立 `uuid` 与 `CODEX_OAUTH_CREDS_FILE_PATH`，`CodexApiService` 用 `config.uuid` 与 `ProviderPoolManager` 联动刷新与健康状态。

---

## 9. 在 keyflow 中落地的建议摘要

1. **凭证**：与本文 **§2.5** 同结构；由上游提供 JSON 时至少保证 `access_token` + `id_token`，建议带 `refresh_token` 与可信的 `expired`（或自行按 `expires_in` 计算）。
2. **刷新**：实现 `POST token` 的 refresh_grant，并写回同一存储单元。
3. **调用**：`POST {base}/responses`，携带 **§6.3** 头与 **§6.4** 体规则；正确处理 SSE。
4. **模型列表**：静态列表 + 可选 `-fast` 别名，与 AIClient 策略一致。
5. **用量**：`GET .../wham/usage`，按 **§7.2** 将 `used_percent` 转为 `remaining` 比例，便于与现有「容量信号」统一。

---

## 10. 文档修订记录

| 日期 | 说明 |
|------|------|
| 2026-04-09 | 初版：根据 AIClient-2-API-main 源码整理 OAuth、调用、用量与号池配置 |
