# 项目闭环评估验收记录

> 更新时间：2026-03-19
> 范围：本轮针对“API 主测试闭环、插件管理面收口、文档状态同步”的修正验收。

## 本轮完成项

- 修复 `tests/fakes.py` 与 `ProviderPlugin` 新契约不一致的问题，补齐测试桩的管理面元信息字段。
- 修复测试容器未注册 `ProviderRegistry` 的问题，使 `/api/providers` 路径可以纳入回归。
- 为管理面新增回归覆盖：
  - `/api/providers`
  - `/api/keys/{id}/explain`
- 统一 `OpenRouterPlugin.explain_credential()` 的说明输出，补齐脱敏后的 `credential_hint`。
- 回写 `docs/plan.md`，将“已验证通过”和“仍未验收”拆开描述。
- 将核心凭证模型从单字符串 `api_key` 重构为结构化 `credential: dict[str, str]`。
- 管理端新增/更新请求与内部 `allocate-key` 响应统一切换为 `credential` 字典。
- 各 provider 插件统一改为从 `credential` 字典读取字段；`gemini-web-proxy` 改为显式要求 `secure_1psid` 与 `secure_1psidts` 双 cookie。

## 本轮验证结果

### 自动化测试

已执行：

```text
python -m pytest tests/test_api.py -q -rA
python -m pytest tests/test_provider_plugins.py -q -rA
python -m pytest -q -rA
```

结果：

- `tests/test_api.py`：15 passed
- `tests/test_provider_plugins.py`：13 passed
- 全量 `pytest`：未通过，阻塞于 `tests/test_gemini_webapi.py`

全量测试阻塞说明：

- `tests/test_gemini_webapi.py` 在模块导入阶段直接执行 `asyncio.run(main())`
- 文件内包含硬编码的 Gemini Web Cookie 凭证
- 该文件会触发真实网络请求，不适合作为稳定的默认自动化回归测试

### 已验证能力

- API 默认启动路径可通过测试容器创建
- `allocate-key / report-error / report-success` 主链路可用
- 带 `model` 的本地辅助过滤和插件透传可用
- 管理端 CRUD 可用
- `/api/providers` 可返回插件元信息
- `/api/keys/{id}/explain` 可返回脱敏说明信息
- 单字段 provider 已统一读取 `credential["api_key"]`
- `allocate-key` 已返回结构化 `credential` 字典而非裸字符串 `api_key`
- `gemini-web-proxy` 缺依赖时按不可分配降级
- `gemini-web-proxy` 缺少 `secure_1psidts` 时会安全失败
- `gemini-web-proxy` 在双 cookie 齐全时会按双字段初始化 `GeminiClient`
- `openrouter` 的 `explain_credential` 输出包含统一的脱敏字段

## 当前仍未验收项

- Redis + Lua 在真实并发场景下的不重复分配验证
- PostgreSQL 持久化真实链路验证
- Docker 本地联调验证
- `/health` 端到端验收
- 若坚持“独立 worker”口径，则当前实现与文档仍需进一步统一；现实现为“cooldown 内联恢复”
- `tests/test_gemini_webapi.py` 的定位需要明确：应改为手工脚本、受控集成测试，或从默认测试套件中隔离

## 当前结论

本轮完成后，KeyFlow 已从“API 主测试失败、插件管理面回归缺失”的状态，推进到“核心 API 回归可通过、插件管理面已有自动化保护”的状态。

但项目仍不能宣称 `v1` 已整体验收完成。当前更准确的表述是：

```text
主链路代码可验证，管理面关键接口已有回归；
外部依赖联调与真实并发验收仍未完成。
```
