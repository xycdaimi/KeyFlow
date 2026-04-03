# Key 状态与探测合并修正计划

## 1. 背景与纠错

旧计划存在根本性错误，不能作为实现依据：

1. 把 `status` 过度绑定到单一探测结果，没有把“凭据本身是否可用”和“额度是否可用”拆开。
2. 试图删除 `key.cached_available`，这是错误的。现有语义里，`key.cached_available` 表示“凭据本身的可用性缓存”，不是最终 `status`。
3. 在抽象接口外部试图通过获取模型列表、列表接口结果等旁路信息判断 key 是否可用，这是禁止的。核心层只能依赖插件契约暴露的标准信号，不能自己发明规则。
4. `fetch_models` 只能用于同步 `supported_models`，不能作为设置 `status` 的依据。
5. `status` 的来源、优先级、恢复条件、管理端可写边界都没有写清楚，容易把项目再次做乱。

本计划替代旧计划。后续实现必须以本文为唯一准则。

---





## 2. 不可违背的领域结论

### 2.1 `status` 必须由两类信号共同决定

供应商凭据的真实可用性至少包含两个维度：

1. **凭据本身可用性**
   - 由 `plugin.is_credential_available(credential, model)` 决定。
   - 语义是：凭据本身是否仍然有效、可鉴权、可访问 provider。
   - 这不是额度判断。

2. **额度可用性**
   - 由插件标准容量信号决定。
   - 语义是：该凭据在 provider 侧是否还有可继续使用的额度。
   - 这不是鉴权判断。

`status` 必须由这两个维度合成，不能只看其中一个。

### 2.2 核心层禁止绕过插件契约判活

核心层禁止使用以下手段判断 key 是否可用：

- `fetch_models()` 的成功或失败
- 任何列表接口返回结果
- `supported_models` 是否为空
- 插件私有字段、插件私有缓存、插件内部临时状态
- 在 `KeyService`、路由层、仓储层、调度层自行拼接 provider 规则

允许的唯一来源：

- `plugin.is_credential_available(...)`
- `plugin.get_capacity_signal(...)` 返回的标准化额度信号
- 核心自己的状态机事件：`rate_limit`、`quota_exhausted`、`disabled`
- 管理端明确写入的 `disabled_admin`

注意：

- 插件**内部实现**可以因为 provider 差异，使用任意远端接口完成 `is_credential_available` 或 `get_capacity_signal`。
- 但**抽象接口外部**绝不允许这么做。

### 2.3 `fetch_models` 不参与状态判断

`fetch_models` 的唯一职责：

- 同步 `supported_models`

`fetch_models` 失败时：

- 记录日志
- 清空或保留 `supported_models`，按实现方便处理
- **绝不**据此修改 `status`
- **绝不**据此修改 `cached_available`
- **绝不**据此推断 `exhausted` / `disabled_upstream`

---

## 3. 修正后的状态模型

### 3.1 对外 `KeyStatus`

保留并明确以下状态：

| 值 | 来源 | 含义 |
|----|------|------|
| `available` | 核心合成 | 可调度 |
| `rate_limited` | 核心状态机 | 运行期限流退避 |
| `cooldown` | 核心状态机 | 运行期冷却中 |
| `exhausted` | 核心合成 / 运行回报 | 凭据有效，但额度不可用 |
| `disabled_upstream` | 核心合成 | 凭据本身不可用 |
| `disabled_admin` | 管理端 | 管理员手工禁用 |
| `disabled_report` | 运行回报 | 业务回报禁用 |

删除泛型 `disabled`。

### 3.2 内部缓存字段语义

本任务中：

- **保留** `cached_available`
  - 语义固定为：`plugin.is_credential_available` 的缓存结果
  - 它表示“凭据本身可用性”，不是最终 `status`
- **新增** `cached_quota_available`
  - 语义固定为：额度是否可用
  - `True` 表示额度可用
  - `False` 表示额度明确不可用，应合成为 `exhausted`
  - `None` 表示插件无法提供稳定额度判断，不能据此设为 `exhausted`
- **保留** `cached_capacity_score`
  - 仅用于排序增强
  - 不能单独决定 `status`
- **删除** `disabled_reason`
  - 该字符串字段继续存在只会制造脏语义

### 3.3 状态优先级

当核心根据缓存信号与运行时状态合成最终 `status` 时，优先级如下：

1. `disabled_admin`
2. `disabled_report`
3. `disabled_upstream`
   - 条件：`cached_available is False`
4. `exhausted`
   - 条件：`cached_available is True` 且 `cached_quota_available is False`
5. `rate_limited` / `cooldown`
   - 条件：运行态窗口仍有效
6. `available`
   - 条件：`cached_available is True` 且 `cached_quota_available in {True, None}`，且不处于运行态锁定窗口

补充规则：

- `cached_available is False` 时，必须落到 `disabled_upstream`，不能落到 `exhausted`
- `cached_quota_available is False` 只有在 `cached_available is True` 时才有意义
- `cached_quota_available is None` 不能制造 `exhausted`
- 已经处于 `exhausted` 的 key，只有在插件明确返回 `cached_quota_available is True` 时才能被探测刷新自动解除；仅返回 `None` 不足以解除

---

## 4. 插件契约修正

### 4.1 `is_credential_available` 语义修正

从本任务开始，插件的 `is_credential_available` 语义固定为：

- 只回答“凭据本身是否可用”
- 不再把“额度耗尽”混进这个布尔值

因此，下列实现需要修正：

- `openrouter`
- `openai`
- 其他任何把 quota/balance 耗尽直接返回为 `False` 的 provider

目标是把“凭据是否有效”和“额度是否可用”拆开。

### 4.2 `CapacitySignal` 契约扩展

扩展 `infrastructure/plugins/base.py` 中的 `CapacitySignal`：

```python
@dataclass(slots=True)
class CapacitySignal:
    has_capacity_signal: bool
    capacity_score: float | None
    quota_available: bool | None
    capacity_kind: str
    reason: str
```

语义：

- `quota_available=True`：额度明确可用
- `quota_available=False`：额度明确不可用，应合成 `exhausted`
- `quota_available=None`：无可靠额度判断，只能用于排序，不得生成 `exhausted`

### 4.3 provider 行为要求

1. `openrouter`
   - `is_credential_available` 仅判断凭据本身是否有效、接口是否可鉴权
   - `get_capacity_signal` 返回 `quota_available`
   - 余额为 0 时应返回 `quota_available=False`

2. `openai`
   - 若 provider 响应能明确识别 `insufficient_quota` 等额度耗尽语义，则放入 `get_capacity_signal`
   - 不再把该语义直接混进 `is_credential_available=False`

3. 其他 provider
   - 若没有稳定额度接口，可返回 `quota_available=None`
   - 但不得伪造 `False`

---

## 5. 核心服务修正

### 5.1 刷新逻辑职责

`refresh_keys` 与 `_refresh_single_key` 必须完成三件事：

1. 刷新 `cached_available`
2. 刷新 `cached_quota_available` 与 `cached_capacity_score`
3. 根据缓存结果合成最终 `status`

必须新增统一合成函数，推荐命名：

- `_merge_refresh_signals_into_status`

参考逻辑：

```python
def _merge_refresh_signals_into_status(self, key: ApiKey, now: datetime) -> None:
    if key.status == KeyStatus.DISABLED_ADMIN:
        return
    if key.status == KeyStatus.DISABLED_REPORT:
        return

    if key.cached_available is False:
        key.status = KeyStatus.DISABLED_UPSTREAM
        return

    if key.cached_available is True and key.cached_quota_available is False:
        key.status = KeyStatus.EXHAUSTED
        return

    if key.status == KeyStatus.DISABLED_UPSTREAM and key.cached_available is True:
        key.status = KeyStatus.AVAILABLE

    if key.status == KeyStatus.EXHAUSTED and key.cached_quota_available is True:
        key.status = KeyStatus.AVAILABLE

    self._state_machine.recover_if_ready(key, now)
```

要求：

- `refresh_keys` 与 `_refresh_single_key` 共用同一套合成规则
- 不得复制两份相似但不一致的逻辑
- 不得在别处偷偷再写一套 `if/else` 判活逻辑

### 5.2 `create_key` / `update_key`

规则如下：

1. 创建 key 时
   - 先刷新缓存信号
   - 再合成 `status`
   - 再同步 `supported_models`

2. 更新 `credential` 时
   - 先重跑刷新
   - 再重跑 `supported_models`
   - 不得因为换 credential 自动解除 `disabled_admin`
   - 不得因为换 credential 自动解除 `disabled_report`

3. 管理端写状态时
   - 只允许写 `available` 与 `disabled_admin`
   - 禁止管理端直接写 `disabled_upstream`
   - 禁止管理端直接写 `disabled_report`
   - 禁止管理端直接写 `exhausted`
   - 禁止管理端直接写 `rate_limited` / `cooldown`

因此需要收紧：

- `src/interfaces/schemas/request.py`
- `src/application/services/key_service.py`

### 5.3 `_collect_candidates`

`_collect_candidates` 禁止再直接用下面任何条件判断可用性：

- `key.cached_available is True`
- `key.cached_quota_available`
- `supported_models` 为空与否
- 重新调用 provider 探测

它只能依赖：

- `self._is_cache_fresh(key, now)`
- `key.is_available(now)`
- 请求模型与 `supported_models` 的匹配关系

其中：

- `is_available(now)` 看的是**已经合成好的正式状态**
- 不是直接看缓存字段

### 5.4 `ApiKey.is_available(now)`

修正规则：

- `available` 为 `True`
- `rate_limited` / `cooldown` 在 `cooldown_until <= now` 时可视为可调度
- `disabled_upstream` / `disabled_admin` / `disabled_report` / `exhausted` 为 `False`

### 5.5 `KeyStateMachine`

状态机保持运行态职责，不再承担 provider 探测职责：

- `on_error("rate_limit")` -> `rate_limited`
- `on_error("quota_exhausted")` -> `exhausted`
- `on_error("disabled")` -> `disabled_report`
- `recover_if_ready` 只处理 `rate_limited` / `cooldown`

`on_success` 规则：

- 若当前 `status == disabled_admin`，必须短路保留
- 若当前 `status == disabled_report`，成功后恢复为 `available`
- 若当前 `status == exhausted`，成功后可恢复为 `available`
- 不负责解除 `disabled_upstream`，该行为只能来自刷新探测

---

## 6. `fetch_models` 与模型列表的边界

以下规则必须写死并落实到代码：

1. `fetch_models` 只更新 `supported_models`
2. `supported_models` 只用于模型筛选，不用于判活
3. 核心层任何地方都不能因为：
   - `fetch_models` 成功
   - `fetch_models` 失败
   - 返回列表为空
   - 某模型不在列表中
   来推导 `disabled_upstream`、`exhausted`、`available`

如果某 provider 必须依赖模型列表接口判断凭据有效性，那也必须封装在插件自己的 `is_credential_available` 或 `get_capacity_signal` 内部，不能泄漏到核心层。

---

## 7. 允许修改的文件范围

实现模型 `gpt-5.3-codex` 只允许修改下列文件；超出范围必须先停下并请求确认，不得擅自扩散：

### 7.1 核心代码

- `src/domain/value_objects/key_status.py`
- `src/domain/entities/api_key.py`
- `src/domain/services/state_machine.py`
- `src/domain/services/scorer.py`
- `src/application/services/key_service.py`
- `src/interfaces/schemas/request.py`
- `src/interfaces/schemas/response.py`

### 7.2 存储与启动

- `src/infrastructure/db/models.py`
- `src/infrastructure/db/repository_impl.py`
- `src/interfaces/api/app.py`
- 如有与本任务直接相关的初始化 SQL，可修改对应 SQL 文件，但必须在最终报告中明确列出

### 7.3 插件抽象与 provider 实现

- `src/infrastructure/plugins/base.py`
- `src/infrastructure/plugins/providers/openrouter.py`
- `src/infrastructure/plugins/providers/openai.py`
- 只有在确认某 provider 当前确实把 quota 语义混入了 `is_credential_available` 时，才允许追加修改对应 provider 文件

### 7.4 测试

- `tests/test_domain.py`
- `tests/test_worker_runtime.py`
- `tests/test_provider_plugins.py`
- `tests/test_api.py`
- `tests/test_postgres_repository_integration.py`
- 必要时新增与本任务直接对应的新测试文件

### 7.5 文档

- `docs/plugin.md`
- `docs/router.md`
- `docs/调度算法.md`
- `docs/v1.1.0.md`
- `docs/plan.md`
- 本计划文件自身仅在用户再次要求修订时才允许修改

---

## 8. 对实现模型的强制约束

以下要求不是建议，是强制约束。

### 8.1 禁止偷偷改文件

禁止以下行为：

- 通过终端命令拼接文本直接覆盖源码而不说明
- 把修改指令藏在 shell 命令、脚本、临时 python、临时 node 程序里批量改文件
- 修改未在“允许修改的文件范围”中的文件
- 顺手修 unrelated issue
- 借格式化、整理 import、重命名等理由扩大 diff

允许的修改方式：

- 明确、可审计的代码编辑
- 最终 `git diff` 中可以清楚看到每个文件改了什么

### 8.2 禁止绕过计划扩 scope

如果在实现过程中发现必须修改未列出的文件，必须：

1. 停止实现
2. 列出原因
3. 说明为什么现有 allowlist 不够
4. 等待确认

在得到确认前，不得修改。

### 8.3 每一步都要可审计

最终交付必须明确列出：

1. 修改了哪些文件
2. 每个文件修改目的是什么
3. 跑了哪些测试
4. 哪些测试没跑，为什么没跑
5. 是否存在未解决风险

不得只汇报“已完成”。

### 8.4 禁止借文档之名改需求

实现模型只能完成本计划明确要求的修正，不能擅自增加：

- 新状态
- 新 provider 接口
- 新管理端行为
- 新数据库结构

除非这些变更已经在本文明确列出。

---

## 9. 具体任务拆分

### Task 1：修正状态枚举与领域对象

目标：

- 删除 `KeyStatus.DISABLED`
- 引入 `DISABLED_UPSTREAM` / `DISABLED_ADMIN` / `DISABLED_REPORT`
- `ApiKey` 保留 `cached_available`
- 新增 `cached_quota_available`
- 删除 `disabled_reason`
- 修正 `ApiKey.is_available(now)`

允许修改：

- `src/domain/value_objects/key_status.py`
- `src/domain/entities/api_key.py`
- `src/infrastructure/db/models.py`
- `src/infrastructure/db/repository_impl.py`
- `src/interfaces/api/app.py`

验收：

- 仓储映射、实体字段、DB 列一致
- 不再存在 `KeyStatus.DISABLED`
- 不再存在 `disabled_reason`

### Task 2：修正插件抽象契约

目标：

- `CapacitySignal` 增加 `quota_available`
- 文档与注释明确：
  - `is_credential_available` 只管凭据本身
  - `get_capacity_signal` 负责额度信号

允许修改：

- `src/infrastructure/plugins/base.py`
- `docs/plugin.md`

验收：

- 基类契约与文档一致
- 不再鼓励把 quota 混进 `is_credential_available`

### Task 3：修正 provider 实现

目标：

- 纠正当前把 quota 语义混入 `is_credential_available` 的 provider
- 把额度耗尽转移到 `get_capacity_signal().quota_available`

允许修改：

- `src/infrastructure/plugins/providers/openrouter.py`
- `src/infrastructure/plugins/providers/openai.py`
- 必要时的其他 provider 文件，但必须先确认其确实存在同类问题

验收：

- provider 侧职责拆分清楚
- 核心不需要解析 provider 私有余额字段

### Task 4：修正状态机与服务合成逻辑

目标：

- 新增统一状态合成函数
- `refresh_keys` / `_refresh_single_key` 共用
- `on_error("disabled")` -> `disabled_report`
- `on_success` 按本文规则修正

允许修改：

- `src/domain/services/state_machine.py`
- `src/domain/services/scorer.py`
- `src/application/services/key_service.py`

验收：

- 不再存在只按 `probe_ok` 覆盖全部状态的逻辑
- `disabled_upstream` 与 `exhausted` 可被明确区分

### Task 5：收紧管理端可写状态边界

目标：

- 管理端只能写 `available` 与 `disabled_admin`
- 其他状态只能由系统生成

允许修改：

- `src/interfaces/schemas/request.py`
- `src/application/services/key_service.py`
- `docs/router.md`
- `docs/v1.1.0.md`

验收：

- API 文档与实际校验一致
- 管理端不能伪造系统态

### Task 6：修正候选收集逻辑

目标：

- `_collect_candidates` 只依赖 cache freshness + 正式状态
- 不直接看 `cached_available`
- 不直接看 `cached_quota_available`
- 不把模型列表当判活逻辑

允许修改：

- `src/application/services/key_service.py`
- `docs/调度算法.md`

验收：

- 外层判活完全回到正式状态模型

### Task 7：测试补齐

必须覆盖以下场景：

1. `cached_available=False` -> `disabled_upstream`
2. `cached_available=True` 且 `cached_quota_available=False` -> `exhausted`
3. `cached_available=True` 且 `cached_quota_available=None` -> 不生成 `exhausted`
4. `disabled_admin` 不被刷新覆盖
5. `disabled_report` 不被刷新覆盖
6. `disabled_upstream` 在凭据恢复后可解除
7. `exhausted` 只有在 quota 明确恢复时才自动解除
8. `fetch_models` 失败不改 `status`
9. 管理端不能写入非法系统态
10. `_collect_candidates` 不再依赖 `cached_available`

允许修改：

- `tests/test_domain.py`
- `tests/test_worker_runtime.py`
- `tests/test_provider_plugins.py`
- `tests/test_api.py`
- `tests/test_postgres_repository_integration.py`
- 必要时新增测试文件

### Task 8：最终验证

至少执行：

```bash
cd d:\py\keyflow
python -m pytest tests/test_domain.py -q
python -m pytest tests/test_provider_plugins.py -q
python -m pytest tests/test_worker_runtime.py -q
python -m pytest tests/test_api.py -q
python -m pytest tests/test_postgres_repository_integration.py -q
python -m pytest tests/ -q --tb=short
```

如有任何测试因环境原因无法运行，必须明确说明，不得跳过不报。

---

## 10. 交付标准

实现完成后，交付结果必须同时满足：

1. 代码逻辑上，`status` 明确由“凭据本身可用性 + 额度可用性 + 运行态状态机 + 管理端锁定态”共同决定。
2. 架构边界上，核心层不再绕过插件抽象接口判活。
3. 数据模型上，保留 `cached_available`，新增 `cached_quota_available`，删除 `disabled_reason`。
4. 行为上，`fetch_models` 不再影响 `status`。
5. 管理边界上，管理端只能控制 `disabled_admin`，不能伪造系统态。
6. 审计上，所有修改文件、执行步骤、测试结果都清楚可见。

如果做不到以上任一条，就不算完成。
