# Gemini OAuth 额度探测与可用性探测修复计划

> 执行要求：按任务顺序实现。所有 Gemini Code Assist 细节必须留在 `GeminiOauthPlugin` 内部，不允许把 provider 专属逻辑塞进 `KeyService`。

**目标：** 修复 `gemini_oauth` 当前因为 `retrieveUserQuota` 失败就被判定为 `disabled_upstream` 的问题。正确逻辑是：先尝试获取额度；额度获取成功则按额度判断容量；额度获取失败则立即发起一次真实模型调用；模型调用成功表示凭证可用，模型调用失败表示凭证不可用。

**核心规则：**

- `is_runtime_credential_available(...)` 只能返回 `bool`，不能引入 `unknown` 或任何新状态。
- `get_runtime_capacity_signal(...)` 只负责额度信号，无法获取额度时返回 `None`。
- `retrieveUserQuota` 失败的范围是所有失败，不限于 `401` 或 `403`。
- 只要 `retrieveUserQuota` 没有成功返回可解析的有效额度数据，就必须走真实模型调用兜底。
- 真实模型调用成功，`is_runtime_credential_available(...)` 返回 `True`。
- 真实模型调用失败，`is_runtime_credential_available(...)` 返回 `False`。
- `KeyService` 现有状态合成逻辑不改：`cached_available=False` 仍然进入 `disabled_upstream`，`cached_available=True 且 cached_quota_available=False` 仍然进入 `exhausted`。

---

## 背景

当前 `gemini_oauth` 的 worker 日志显示：

- `loadCodeAssist` 返回 `200`
- `retrieveUserQuota` 返回失败，例如 `403`
- 后台刷新把 key 变成 `disabled_upstream`

但同一份 OAuth 凭证在 `AIClient-2-API-main` 中可以正常调用模型。这说明凭证本身不一定不可用，当前 KeyFlow 的问题是把“额度接口失败”错误等价成了“模型调用不可用”。

`AIClient-2-API-main` 的行为基线是：

- `loadCodeAssist` / `onboardUser` 用于发现 `projectId`
- `generateContent` / `streamGenerateContent` 才是真实模型调用
- `retrieveUserQuota` 用于获取额度信息
- `retrieveUserQuota` 失败不会直接证明模型不可用

---

## 文件范围

**修改**

- `src/infrastructure/plugins/providers/gemini_oauth.py`
- `tests/test_provider_plugins.py`
- 必要时修改 `tests/test_domain.py`

**不修改**

- 不修改 `KeyService` 的状态机语义，除非实现过程中发现现有测试无法覆盖必要行为。
- 不引入新的 key status。
- 不改变 `ProviderPlugin` 公共接口。

---

## 目标状态映射

### 额度获取成功

`retrieveUserQuota` 成功，并且返回可解析的有效 `buckets`：

- 至少一个支持模型的 `remainingFraction > 0`
  - `is_runtime_credential_available(...) -> True`
  - `get_runtime_capacity_signal(...) -> CapacitySignal(quota_available=True, capacity_score=max_remaining)`
  - 最终状态应为 `available`

- 所有支持模型的 `remainingFraction <= 0`
  - `is_runtime_credential_available(...) -> True`
  - `get_runtime_capacity_signal(...) -> CapacitySignal(quota_available=False, capacity_score=0.0)`
  - 最终状态应为 `exhausted`

### 额度获取失败

以下情况都视为额度获取失败：

- `retrieveUserQuota` 返回任意非 2xx
- 请求异常
- 超时
- 响应不是 JSON
- JSON 中没有 `buckets`
- `buckets` 不是数组
- `buckets` 中没有任何可用于支持模型的有效额度数据

额度获取失败后：

- 必须调用一次最小真实 `generateContent` 探测。
- 探测成功：
  - `is_runtime_credential_available(...) -> True`
  - `get_runtime_capacity_signal(...) -> None`
  - 最终状态应为 `available`
- 探测失败：
  - `is_runtime_credential_available(...) -> False`
  - `get_runtime_capacity_signal(...) -> None`
  - 最终状态应为 `disabled_upstream`

---

## 实现设计

### 1. 拆分请求函数

当前 `_code_assist_post(...)` 同时服务项目发现、额度探测和未来模型探测，导致请求语义混在一起。需要拆分：

- `_code_assist_post(...)`
  - 保留给 `loadCodeAssist`、`onboardUser`、`generateContent`
  - 使用 Gemini CLI 风格 header

- `_retrieve_user_quota(...)`
  - 专门请求 `retrieveUserQuota`
  - 请求体为 `{"project": runtime_credential["project_id"]}`
  - 先按 AIClient-2-API 的 quota 路径对齐请求行为
  - 至少保留 `Authorization: Bearer <access_token>` 与 `Content-Type: application/json`

### 2. 增加真实模型探测函数

新增内部函数，例如：

```python
async def _probe_generate_content(self, runtime_credential: dict[str, str], model: str | None = None) -> bool:
    ...
```

请求方法：

```text
POST https://cloudcode-pa.googleapis.com/v1internal:generateContent
```

最小请求体：

```json
{
  "model": "gemini-2.5-flash",
  "project": "<project_id>",
  "request": {
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "ping"
          }
        ]
      }
    ],
    "generationConfig": {
      "maxOutputTokens": 1
    }
  }
}
```

判定规则：

- HTTP 2xx：返回 `True`
- 其他结果或异常：返回 `False`

不再额外区分 401、403、5xx、超时等失败类型。模型调用失败就是不可用。

### 3. 重写可用性判断

`is_runtime_credential_available(...)` 改成：

```text
调用 retrieveUserQuota
如果 retrieveUserQuota 成功并返回可解析有效额度数据:
    return True
否则:
    return await _probe_generate_content(...)
```

注意：

- `retrieveUserQuota` 失败不直接返回 `False`
- `retrieveUserQuota` 非 2xx 不直接返回 `False`
- `retrieveUserQuota` 数据结构不符合预期不直接返回 `False`
- 只有 fallback 的真实模型调用失败才返回 `False`

### 4. 重写容量判断

`get_runtime_capacity_signal(...)` 保持只查额度：

```text
调用 retrieveUserQuota
如果 retrieveUserQuota 成功并返回可解析有效额度数据:
    返回 CapacitySignal
否则:
    返回 None
```

注意：

- 这里不调用真实模型探测。
- 这里不判断凭证可用性。
- 这里不返回 `disabled_upstream` 相关语义。

### 5. 额度解析函数

新增内部解析函数，例如：

```python
def _quota_signal_from_payload(self, payload: dict[str, Any]) -> CapacitySignal | None:
    ...
```

规则：

- 只统计 `_BASE_MODELS` 中的模型。
- `remainingFraction` 解析失败则忽略该 bucket。
- 有效 bucket 为空则返回 `None`。
- `capacity_score = max(remaining_values)`。
- `quota_available = capacity_score > 0.0`。

---

## 任务清单

## Task 1: 添加当前问题的失败测试

**文件：**

- 修改：`tests/test_provider_plugins.py`

- [ ] 添加测试：`retrieveUserQuota` 返回非 2xx，随后 `generateContent` 返回 2xx。
- [ ] 断言 `is_runtime_credential_available(...) is True`。
- [ ] 断言 `get_runtime_capacity_signal(...) is None`。
- [ ] 测试在当前实现下应失败，因为当前实现会直接把 quota 非 2xx 判为不可用。

## Task 2: 添加额度成功测试

**文件：**

- 修改：`tests/test_provider_plugins.py`

- [ ] 添加测试：`retrieveUserQuota` 返回 supported model bucket，`remainingFraction > 0`。
- [ ] 断言 `is_runtime_credential_available(...) is True`。
- [ ] 断言 `get_runtime_capacity_signal(...)` 返回 `quota_available=True`。
- [ ] 断言 `capacity_score` 等于支持模型中的最大 `remainingFraction`。

## Task 3: 添加额度耗尽测试

**文件：**

- 修改：`tests/test_provider_plugins.py`

- [ ] 添加测试：`retrieveUserQuota` 返回 supported model bucket，但所有 `remainingFraction` 都是 `0`。
- [ ] 断言 `is_runtime_credential_available(...) is True`。
- [ ] 断言 `get_runtime_capacity_signal(...)` 返回 `quota_available=False`。
- [ ] 断言 `capacity_score == 0.0`。

## Task 4: 添加额度无效数据 fallback 测试

**文件：**

- 修改：`tests/test_provider_plugins.py`

- [ ] 添加测试：`retrieveUserQuota` 返回 2xx，但 payload 没有有效 `buckets`。
- [ ] 断言会继续调用 `generateContent`。
- [ ] `generateContent` 成功时，断言 `is_runtime_credential_available(...) is True`。
- [ ] 断言 `get_runtime_capacity_signal(...) is None`。

## Task 5: 添加模型探测失败测试

**文件：**

- 修改：`tests/test_provider_plugins.py`

- [ ] 添加测试：`retrieveUserQuota` 失败，`generateContent` 也失败。
- [ ] 断言 `is_runtime_credential_available(...) is False`。
- [ ] 不区分失败状态码，任意非成功响应或异常都应得到 `False`。

## Task 6: 实现请求拆分与额度解析

**文件：**

- 修改：`src/infrastructure/plugins/providers/gemini_oauth.py`

- [ ] 添加 `_retrieve_user_quota(...)`。
- [ ] 添加 `_quota_signal_from_payload(...)`。
- [ ] 保留 `_code_assist_post(...)` 给项目发现与模型调用。
- [ ] 确保 quota 请求失败时不会抛出到 `KeyService`，而是在插件内部转为 fallback 或 `None`。

## Task 7: 实现真实模型调用 fallback

**文件：**

- 修改：`src/infrastructure/plugins/providers/gemini_oauth.py`

- [ ] 添加 `_probe_generate_content(...)`。
- [ ] 使用 `model` 参数传入的模型；如果为空或不在 `_BASE_MODELS`，使用 `"gemini-2.5-flash"`。
- [ ] 使用最小请求体，限制 `maxOutputTokens` 为 `1`。
- [ ] HTTP 2xx 返回 `True`。
- [ ] 非 2xx 或异常返回 `False`。

## Task 8: 重写 `is_runtime_credential_available`

**文件：**

- 修改：`src/infrastructure/plugins/providers/gemini_oauth.py`

- [ ] 先调用 `_retrieve_user_quota(...)`。
- [ ] 如果 quota 响应成功且 `_quota_signal_from_payload(...)` 返回非 `None`，返回 `True`。
- [ ] 否则调用 `_probe_generate_content(...)`。
- [ ] 返回 `_probe_generate_content(...)` 的 bool 结果。

## Task 9: 重写 `get_runtime_capacity_signal`

**文件：**

- 修改：`src/infrastructure/plugins/providers/gemini_oauth.py`

- [ ] 只调用 `_retrieve_user_quota(...)`。
- [ ] 成功解析出额度则返回 `CapacitySignal`。
- [ ] 任意失败或无效数据返回 `None`。
- [ ] 不调用 `_probe_generate_content(...)`。

## Task 10: 验证服务层状态映射

**文件：**

- 必要时修改：`tests/test_domain.py`

- [ ] 插件返回 `available=True` 且 `capacity=None` 时，key 最终应为 `available`。
- [ ] 插件返回 `available=True` 且 `quota_available=False` 时，key 最终应为 `exhausted`。
- [ ] 插件返回 `available=False` 时，key 最终应为 `disabled_upstream`。

---

## 验证命令

```powershell
pytest tests/test_provider_plugins.py -k "gemini_oauth" -v
pytest tests/test_domain.py -k "refresh" -v
```

如果有可用的真实 `gemini_oauth` 凭证，再做一次手动验证：

- `retrieveUserQuota` 成功，有余额：状态为 `available`
- `retrieveUserQuota` 成功，无余额：状态为 `exhausted`
- `retrieveUserQuota` 失败，真实模型调用成功：状态为 `available`
- `retrieveUserQuota` 失败，真实模型调用失败：状态为 `disabled_upstream`

---

## 验收标准

- `retrieveUserQuota` 失败不再直接导致 `disabled_upstream`。
- `retrieveUserQuota` 失败时一定会触发一次真实 `generateContent` 探测。
- 真实模型调用成功时，`is_runtime_credential_available(...)` 返回 `True`。
- 真实模型调用失败时，`is_runtime_credential_available(...)` 返回 `False`。
- 额度成功且有剩余时，最终状态为 `available`。
- 额度成功且无剩余时，最终状态为 `exhausted`。
- 额度失败但模型调用成功时，最终状态为 `available`，容量信号为 `None`。
- 额度失败且模型调用失败时，最终状态为 `disabled_upstream`。
- 不引入 `unknown` 状态。
- 不改变 `ProviderPlugin` 公共接口。
- 不改变 `KeyService` 的状态合成规则。
