📌 强制规定：
✔ 插件必须实现 *账户可用性判断接口*
✔ 插件内部私有 *余额/用量/价格逻辑，不可对外或给核心使用*
✔ 核心服务只能看到 *是否可用*
✔ 自动跳过不可用账户，被禁用的账户也要跳过
✔ 支持任意凭证形式（API‑Key / Cookie / auth.json / token / session）
✔ 插件契约明确定义、强约束

---

## 📄 KeyFlow 插件系统规范（完善版）

````markdown
# KeyFlow 插件系统规范 文档（正式版）

---

## 一、总体设计宗旨

### 1.1 核心目标
KeyFlow 插件系统用于接入任意第三方供应商账户（例如 OpenAI、Gemini Web API、OpenRouter、极客智坊等），实现：

🔹 多供应商凭证池管理  
🔹 动态调度最优凭证  
🔹 自动跳过不可用凭证  
🔹 插件负责账户可/不可用判断  
🔹 插件内部私有实现 *余额/用量/计费逻辑*，禁止核心调用

> 插件内部可根据供应商真实规则计算余额、消耗、套餐计费等，但这些必须封装在插件内部，不得暴露给 KeyFlow 核心或外部调用者。

---

## 二、术语定义

| 术语 | 含义 |
|------|------|
| Plugin | 供应商适配插件，为 KeyFlow 接入供应商实现空间 |
| Credential | 供应商凭证，不论是 API Key / Cookie / Auth JSON / OAuth Token，都是 opaque token |
| isAvailable | 表示凭证是否当前 *可用* 的布尔判断（插件必须实现） |
| Balance/Usage/Price | 供应商内部计费逻辑，仅插件内部使用，不对外暴露给核心或调用者 |

---

## 三、插件契约与接口规范

### 3.1 插件必须实现的接口契约

**插件暴露给核心的接口必须严格实现以下协议：**

```python
class Credential:
    """
    Opaque credential object.
    Core does not inspect internal fields.
    """

class ProviderPlugin(Protocol):
    name: str                          # 供应商唯一标识
    supported_models: list[str]       # 插件能支持的模型清单（可选）

    async def list_credentials() -> list[Credential]
        """列出插件内部储存的凭证"""

    async def create_credential(**opts) -> Credential
        """
        创建/新增凭证
        opts 由插件内部解析（例如 Cookie、auth.json、token 等）
        """

    async def is_credential_available(cred: Credential, model: str) -> bool
        """
        判断凭证是否 *可用*（核心调度使用该结果判断是否跳过）
        该方法必须由插件内部实现凭证有效性逻辑。
        """

    async def mark_success(cred: Credential, meta: dict) -> None
        """上报成功（插件内部可更新失败计数、限流状态等）"""

    async def mark_error(cred: Credential, error_meta: dict) -> None
        """上报失败（插件内部可决定是否进入冷却/不可用）"""
````

---

## 四、禁止暴露给核心/外部的逻辑

**核心服务不得调用或访问以下内容**

❌ 不得直接调用以下逻辑：

```
get_balance()
get_usage()
calculate_cost()
calculate_remaining_quota()
```

理由：不同供应商计费规则不同，有的甚至无余额概念（如使用 Cookie / session 方式，仅判断登录凭证是否可用），不能统一核心处理。

---

## 五、账户可用性判断（isAvailable）

### 5.1 为什么必须插件负责

有些供应商根本不存在余额/额度 API（例如 Cookie 形式登录凭证），这类凭证“是否可用”仅由 *供应商实际行为判断* 决定，例如：

✔ 能否用当前凭证成功访问一次轻量验证 API
✔ 是否认证失败（401/403）
✔ Cookie 是否过期
✔ OAuth Token 是否已失效

核心服务无法统一这种判断逻辑，必须插件内部实现。

### 5.2 举例说明（伪代码）

```python
class GeminiWebPlugin(ProviderPlugin):
    async def is_credential_available(self, cred, model):
        try:
            # 访问轻量验证端点，检查返回码
            result = await gemini_api.check_session(cred)
            return result.success
        except AuthenticationError:
            return False
        except TransportError:
            return False
```

📌 这种逻辑属于 *插件内部凭证可用性判断*，核心 Service 只根据布尔结果决定是否调度该凭证。

---

## 六、调度系统调用流程

以下是 Core 调用插件的顺序及跳过逻辑：

1. 调用 `plugin.list_credentials()` 获取凭证候选列表
2. 遍历候选凭证：

```
if not plugin.is_credential_available(cred, model):
    skip this credential  # 自动跳过不可用
else:
    add candidate to available list
```

3. 如果 available list 为空：

```
newCred = plugin.create_credential(...)
if plugin.is_credential_available(newCred, model):
    use newCred
else:
    no available credential
```

4. 按调度算法（如 score 排序等）选择最佳凭证

5. 调用方执行 API 调用，最后根据结果调用：

```
plugin.mark_success(cred, meta)
plugin.mark_error(cred, error_meta)
```

---

## 七、余额/用量/价格逻辑规范（插件私有）

插件可以实现供应商内部的计费规则，例如：

```
if supplier has token/paid plans:
    plugin内部获取余额/使用量
else:
    skip balance check entirely
```

但这一逻辑必须满足：

🔒 **核心服务不得访问或调用余额/用量/价格逻辑**
🔒 插件内部仅用于自身状态判断或监控用途

---

## 八、凭证生命周期规范

凭证可能进入以下内部状态：

| 内部状态        | 说明              |
| ----------- | --------------- |
| AVAILABLE   | 当前可用            |
| UNAVAILABLE | 当前不可用           |
| COOLDOWN    | 由于限流或失败进入冷却     |
| INVALID     | 永久无效（例如过期/认证失败） |

插件内部负责维护状态变化和存储。

---

## 九、错误上报与状态变化策略

当凭证被标记失败时（error）：

✔ 插件可根据 `error_meta` 决定是否进入冷却/永久失效
✔ 核心只传递失败信息，不判断内部原因
✔ 插件负责更新自身状态

---

## 十、插件版本兼容与加载策略

### 10.1 插件元信息

插件必须声明：

```
PLUGIN_NAME = "gemini_web"
PLUGIN_VERSION = "1.0.0"
PLUGIN_INTERFACE_VERSION = "1.0.0"
```

### 10.2 核心加载规则

* 插件加载时需兼容 CORE 接口版本
* 否则拒绝加载并记录错误

---

## 十一、管理界面/监控集成规范

插件可以提供可选方法：

```
async explain_credential(cred: Credential) -> dict
```

用于管理界面展示凭证状态、类型、最后检查时间等，但**不可包含敏感凭证内部字段**。

示例返回结构：

```json
{
  "provider": "gemini_web",
  "status": "AVAILABLE",
  "last_checked_at": "...",
  "model_support": ["*"],
  "remark": "login cookie valid"
}
```

---

## 十二、总结

### 核心服务职责

✔ 调用 list_credentials
✔ 通过 is_credential_available 过滤不可用
✔ 调度可用凭证
✔ 反馈成功/失败结果到插件

### 插件内部职责

✔ 实现凭证实际可用性检测
✔ 管理凭证状态生命周期
✔ 可实现余额/用量/计费逻辑（但禁止外部访问）
✔ 解析并存储凭证数据格式（API Key/Cookie/auth etc）
