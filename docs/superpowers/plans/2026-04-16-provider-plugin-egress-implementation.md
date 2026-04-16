# Provider Plugin Egress Implementation Plan

> For execution: implement task-by-task, keep provider plugin ownership clear, and do not move proxy decisions into worker or application layers.

**Goal:** Add fixed plugin-level egress strategy so domestic providers stay direct, overseas providers use the fixed proxy from `.env`, and network failures continue to surface as upstream unreachable rather than credential failure.

**Architecture:** Keep egress decisions inside provider plugins and existing plugin-side HTTP utilities. Extend runtime settings with proxy/timeout fields, centralize the reusable HTTP client construction in the plugin infrastructure that already exists, and update provider plugins to declare their fixed egress mode and use the shared request path consistently.

**Tech Stack:** Python 3.13, `httpx`, pydantic-settings, pytest, existing `ProviderPlugin` contract

---

## File Structure

**Create**
- `docs/superpowers/plans/2026-04-16-provider-plugin-egress-implementation.md`

**Modify**
- `src/infrastructure/config/settings.py`
- `src/infrastructure/plugins/base.py`
- `src/infrastructure/plugins/providers/openai.py`
- `src/infrastructure/plugins/providers/anthropic.py`
- `src/infrastructure/plugins/providers/gemini.py`
- `src/infrastructure/plugins/providers/gemini_web_proxy.py`
- `src/infrastructure/plugins/providers/openrouter.py`
- `src/infrastructure/plugins/providers/codex_oauth.py`
- `src/infrastructure/plugins/providers/codex_openai.py`
- `tests/test_provider_plugins.py`
- `tests/test_container_plugins.py` or the nearest plugin wiring test file if that is the actual coverage point
- `.env.example`

**Responsibilities**
- `src/infrastructure/config/settings.py`: add fixed proxy URL and timeout fields sourced from `.env`
- `src/infrastructure/plugins/base.py`: expose the existing shared plugin-side HTTP construction path with egress-aware defaults, without pushing decisions into worker or service layers
- `src/infrastructure/plugins/providers/*.py`: declare per-plugin fixed egress mode and route all upstream HTTP calls through the shared plugin-side path
- `tests/test_provider_plugins.py`: verify direct/proxy selection, timeout wiring, and network failure semantics
- `.env.example`: document the new runtime variables

---

## Task 1: Add Runtime Settings For Fixed Proxy And Timeouts

**Files:**
- Modify: `src/infrastructure/config/settings.py`
- Modify: `.env.example`
- Test: nearest settings coverage file if present, otherwise extend plugin tests with a focused settings parse case

- [ ] Add settings fields for:
  - fixed proxy URL
  - connect timeout seconds
  - read timeout seconds
  - total timeout seconds
- [ ] Keep names aligned with the approved spec, for example:
  - `GLOBAL_HTTP_PROXY`
  - `HTTP_CONNECT_TIMEOUT`
  - `HTTP_READ_TIMEOUT`
  - `HTTP_TOTAL_TIMEOUT`
- [ ] Update `.env.example` with safe commented defaults
- [ ] Add one focused test that proves the settings object reads these values correctly
- [ ] Verify no existing settings callers break due to the new optional fields

## Task 2: Wire Egress-Aware Shared HTTP Construction In Plugin Infrastructure

**Files:**
- Modify: `src/infrastructure/plugins/base.py`
- Test: `tests/test_provider_plugins.py`

- [ ] Identify the existing shared plugin-side HTTP construction path and extend it instead of adding a parallel abstraction
- [ ] Add a minimal plugin-level egress representation, limited to `direct` and `proxy`
- [ ] Make the shared HTTP path derive `httpx` proxy and timeout settings from:
  - plugin fixed egress mode
  - runtime settings
- [ ] Keep worker, `KeyService`, and route handlers completely unaware of this logic
- [ ] Add tests that validate:
  - direct mode does not attach the proxy
  - proxy mode attaches the fixed proxy URL
  - configured timeouts are passed through

## Task 3: Declare Fixed Egress Mode In Provider Plugins

**Files:**
- Modify provider plugins that perform outbound HTTP calls
- Test: `tests/test_provider_plugins.py`

- [ ] For each provider plugin, declare its fixed egress mode in code
- [ ] Mark domestic providers as `direct`
- [ ] Mark overseas providers as `proxy`
- [ ] Keep the decision hard-coded in plugin classes, with no extra config override layer
- [ ] Review current provider list and explicitly classify each plugin touched in container registration
- [ ] Add one test per representative plugin class to lock this down

## Task 4: Route All Provider HTTP Calls Through The Shared Plugin Path

**Files:**
- Modify provider plugins that currently instantiate `httpx.AsyncClient` directly
- Test: `tests/test_provider_plugins.py`

- [ ] Replace scattered raw `httpx.AsyncClient(timeout=...)` usage with the shared plugin-side request/client path
- [ ] Cover all upstream HTTP behavior inside each plugin, not just availability probes:
  - root reachability checks
  - model fetches
  - credential availability probes
  - capacity probes
  - OAuth refresh calls
- [ ] Keep existing request semantics unchanged apart from egress and timeout injection
- [ ] Verify overseas plugins can use proxy for all outbound requests consistently

## Task 5: Preserve Error Semantics

**Files:**
- Modify: affected provider plugins and shared plugin infrastructure
- Test: `tests/test_provider_plugins.py`

- [ ] Normalize network-layer failures from direct or proxy paths into the existing upstream unreachable / website unreachable semantics already used by the project
- [ ] Do not convert proxy/network failures into credential invalidation
- [ ] Add regression tests for:
  - proxy unavailable
  - connect timeout
  - DNS/request error
  - auth failure remains auth failure
- [ ] Confirm `KeyService.refresh_keys()` behavior remains unchanged: plugin failure still maps to cached availability/capacity handling already in place

## Task 6: Run Focused Regression Suite

**Files:**
- Modify tests only if failures show missing coverage

- [ ] Run focused plugin tests
- [ ] Run container/plugin registration tests
- [ ] Run any existing refresh/worker runtime tests that touch provider probing
- [ ] Record any plugin still bypassing the shared path and fix it before finishing

Suggested commands:

```bash
pytest tests/test_provider_plugins.py -v
pytest tests/test_container_plugins.py -v
pytest tests/test_worker_runtime.py -v
```

---

## Self-Review

**Scope check**
- Plan stays inside provider plugins, plugin infrastructure, settings, and tests.
- Plan does not push proxy logic into worker, application service, or allocation flow.

**Consistency check**
- Fixed egress is plugin-owned.
- `.env` only provides proxy and timeout runtime values.
- Network failure semantics remain upstream unreachable.

**Risk check**
- Main risk is incomplete migration if some plugin still creates raw `httpx.AsyncClient` instances.
- Tests must explicitly catch that to avoid half-direct, half-proxy behavior within one provider.
