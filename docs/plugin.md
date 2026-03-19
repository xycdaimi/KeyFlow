# KeyFlow 插件系统规范

## 1. 设计目标

插件系统的目标是把供应商差异封装起来，让核心调度逻辑保持稳定。

插件负责：

- 识别 provider 差异
- 获取支持模型
- 判断凭证当前是否可用
- 处理 provider 私有认证、余额、套餐、价格等逻辑
- 返回安全的说明信息供管理界面展示

插件不负责：

- 替代核心数据库保存全部账户
- 成为系统正式状态的真相源
- 主导管理端账户 CRUD

---

## 2. 核心原则

### 2.1 账户归核心管理

- `credential` 凭证载荷由核心数据库存储
- 账户 CRUD 由核心接口负责
- 插件接收的是已有账户中的凭证值

### 2.2 状态归核心管理

核心正式状态由系统维护，例如：

- `AVAILABLE`
- `RATE_LIMITED`
- `COOLDOWN`
- `DISABLED`
- `EXHAUSTED`

插件可以维护私有临时状态或缓存，但不替代核心正式状态机。

### 2.3 provider 差异归插件

插件负责的典型差异包括：

- HTTP Header 形式不同
- API Host 不同
- 模型列表接口不同
- 鉴权失败判定不同
- Cookie / session 凭证可用性判断不同
- provider 私有余额或套餐规则不同

### 2.4 计费逻辑私有化

以下逻辑只能保留在插件内部：

- `get_balance()`
- `get_usage()`
- `calculate_cost()`
- `calculate_remaining_quota()`

核心不能直接依赖这些私有逻辑作为统一接口前提。

### 2.5 容量信号可选暴露

`v1.1` 开始，插件可以**可选**暴露一个标准化容量信号，供核心调度在健康度之外做轻量额度感知排序。

注意：

- 这是可选能力，不是强制能力
- 核心只接收归一化后的 `capacity_score`
- 核心仍然不直接读取 provider 私有余额、套餐、价格、成本细节
- 没有可靠额度数据的 provider 应返回 `None`

---

## 3. 插件契约

`v1` 插件契约如下：

```python
class ProviderPlugin(Protocol):
    @property
    def name(self) -> str:
        ...

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        """返回该凭证支持的模型列表。"""

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        """判断该凭证当前是否可用于指定 model。"""

    async def mark_success(self, credential: dict[str, str], meta: dict | None = None) -> None:
        """可选：成功回调。默认允许为空实现。"""

    async def mark_error(self, credential: dict[str, str], error_meta: dict | None = None) -> None:
        """可选：失败回调。默认允许为空实现。"""

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        """返回不含敏感字段的安全说明信息。"""

    async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
        """可选：返回标准化后的容量信号。"""
```

其中可选容量信号可理解为：

```python
@dataclass(slots=True)
class CapacitySignal:
    has_capacity_signal: bool
    capacity_score: float | None
    capacity_kind: str
    reason: str
```

约束：

- `capacity_score` 范围应归一化到 `0 ~ 1`
- `1` 表示容量最充足
- `0` 表示容量已耗尽或极低
- `None` 表示 provider 没有可靠容量数据，核心会走中性回退

---

## 4. 为什么不使用 list/create 凭证接口

以下设计不作为 `v1` 插件契约的一部分：

- `list_credentials()`
- `create_credential(...)`

原因：

- 当前系统已经明确由核心维护账户池
- 管理接口已经以核心数据库为真相源
- 若插件也持有完整凭证列表，会形成双真相源

因此 `v1` 的职责划分是：

- 核心：保存和管理账户
- 插件：解释和验证账户

---

## 5. 可用性判断与容量信号规范

### 5.1 插件必须实现

```python
is_credential_available(credential, model)
```

这是核心调度时使用的唯一 provider 侧可用性信号。

### 5.2 插件可用性判断可以依赖

- 认证是否成功
- 轻量模型接口是否可访问
- Cookie 是否过期
- Token 是否失效
- provider 私有额度是否耗尽
- 指定模型是否可用

### 5.3 核心如何使用

核心只根据布尔结果处理：

```text
False -> 跳过该凭证
True  -> 允许进入调度候选
```

核心不要求插件解释全部内部原因。

### 5.4 插件可选实现容量信号

```python
get_capacity_signal(credential)
```

语义：

- 这是排序增强信号，不是硬门槛
- 插件若没有稳定额度模型，应返回 `None`
- 插件若有可靠剩余额度/预算窗口数据，应自行在插件内部换算成 `0 ~ 1`

推荐做法：

- `openrouter` 这类 provider：在插件内部基于预算窗口换算 `remaining / limit`
- `openai / anthropic / gemini` 这类缺少可靠 per-key 额度数据的 provider：返回 `None`
- `gemini-web-proxy` 这类无计费概念 provider：返回 `None`

---

## 6. 插件与核心的交互流程

### 6.1 新增账户时

1. 管理端创建账户
2. 核心调用 `fetch_models(credential)`
3. 核心保存 `supported_models`

### 6.2 分配时

1. 核心从数据库加载 provider 下候选账户
2. 核心先按正式状态过滤
3. 核心调用 `is_credential_available(credential, model)`
4. 核心可选调用 `get_capacity_signal(credential)`
5. 核心对可用候选执行统一评分和原子分配

### 6.3 回写时

1. 核心更新正式状态和统计字段
2. 核心可选调用 `mark_success / mark_error`
3. 插件可内部更新临时缓存、provider 私有计数或诊断信息

---

## 7. 插件能做什么，不能做什么

### 7.1 可以做

- 拉取模型列表
- 判断 cookie / token 是否有效
- 访问 provider 的余额接口
- 根据 provider 规则判断“是否还可继续用”
- 记录 provider 私有告警或统计
- 生成管理端说明信息

### 7.2 不能做

- 直接覆盖核心数据库里的正式状态
- 假定自己是账户池的主存储
- 向核心暴露统一的成本/额度接口并要求核心强依赖
- 在管理界面说明里暴露原始密钥、cookie、token

---

## 8. 内部私有状态与核心正式状态的关系

插件内部可以有自己的私有状态，例如：

- `token_invalid`
- `cookie_expired`
- `billing_blocked`
- `provider_cooldown`

但这些状态只作为插件内部判断依据，不直接取代核心正式状态。

推荐关系：

```text
插件私有状态 -> 提供判断信号 -> 核心状态机落正式状态
```

例如：

- 插件判断认证失效 -> 核心后续可将账户置为 `DISABLED`
- 插件判断短期限流 -> 核心可进入 `RATE_LIMITED / COOLDOWN`

---

## 9. explain_credential 规范

插件应提供安全说明信息，供管理界面或排障使用。

示例：

```json
{
  "provider": "gemini-web-proxy",
  "status": "unknown",
  "auth_type": "cookie",
  "model_support": ["gemini-2.5-pro"],
  "remark": "credential appears valid"
}
```

禁止返回：

- 原始 `api_key`
- 原始 cookie
- token 全值
- 任何可直接复用的敏感凭证字段

---

## 10. 版本兼容

插件至少应声明：

```python
PLUGIN_NAME = "openai"
PLUGIN_VERSION = "1.0.0"
PLUGIN_INTERFACE_VERSION = "1.0.0"
```

核心加载策略：

- 接口版本兼容才允许注册
- 不兼容时应拒绝加载并记录日志

---

## 11. v2 扩展点

如果未来要做更强能力，可增加可选扩展接口，而不是污染 `v1` 主契约。

例如：

- `QuotaAwareCapability`
- `CostAwareCapability`
- `CredentialFactoryCapability`
- `HealthProbeCapability`

这些能力只有在系统整体边界确认后再纳入。

---

## 12. 总结

插件系统的统一口径如下：

```text
核心管理账户，核心维护正式状态，核心负责调度；
插件封装 provider 差异，插件判断凭证是否可用；
余额、价格、套餐等业务逻辑留在插件内部；
如果要参与调度，插件只能暴露标准化后的可选容量信号，而不是把私有计费模型直接泄漏给核心。
```
