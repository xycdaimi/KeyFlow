# Report-Driven Key State Design

## Background

`KeyStatus.DISABLED_REPORT`, `RATE_LIMITED`, and `COOLDOWN` exist in the domain model, allocation stores, scorer, and recovery code. Current report handling still does not drive these states: `KeyService.report_error(key_id, lease_id, error_type)` records aggregate error counters, calls the provider callback, releases the lease, and syncs the allocation store, but it does not classify the error or update `status`.

`DISABLED_REPORT` is not a permanent disable state. It means the program has enough report evidence to isolate a credential from allocation because it is unlikely to recover quickly. The program may automatically restore it later through stale credential refresh when provider checks prove the credential is usable again.

The current runtime refresh code has two distinct paths:

- stale refresh: `refresh_keys()` selects keys from `list_keys_needing_refresh(cutoff)` plus stale `PENDING` keys; this path skips `DISABLED_ADMIN` but intentionally does not skip `DISABLED_REPORT`, because a real credential refresh should be allowed to reset a report-disabled key when provider checks prove the credential is usable again
- fresh availability check: `refresh_keys()` also checks non-stale keys from a single `list_keys()` snapshot; this path skips `DISABLED_ADMIN`, `DISABLED_REPORT`, and active temporary blocks where `status in {RATE_LIMITED, COOLDOWN}` and `cooldown_until > now`

There is one important implementation constraint: even though the stale refresh candidate path now includes `DISABLED_REPORT`, the existing persistence and merge helpers still protect it:

- `_merge_refresh_signals_into_status()` returns immediately for `DISABLED_REPORT`
- `_merge_runtime_mutation()` does not overwrite `DISABLED_REPORT`
- `update_background_runtime_snapshot_if_locked()` refuses to persist background writes when the current row is `DISABLED_REPORT`

The implementation must relax those protections only for explicit stale credential refresh of `DISABLED_REPORT`, while keeping `DISABLED_ADMIN` protected.

## Goals

- Make report errors classify `error_type` and update key runtime state.
- Use `RATE_LIMITED` for provider rate-limit responses, not local lease saturation.
- Use `COOLDOWN` for repeated transient execution failures.
- Escalate a key to `DISABLED_REPORT` after 3 cooldown rounds.
- Directly set `DISABLED_REPORT` for fatal credential errors.
- Allow stale credential refresh to automatically recover `DISABLED_REPORT` when provider checks prove the credential is healthy.
- Keep fresh availability checks from interrupting active `RATE_LIMITED` / `COOLDOWN` protection windows.
- Keep `DISABLED_ADMIN` manual and protected from automatic report or refresh recovery.
- Keep lease release behavior unchanged for success and error reports.

## Non-Goals

- Do not represent `max_concurrent_uses` saturation as `RATE_LIMITED`; allocation stores already enforce active lease limits.
- Do not replace existing provider refresh outcomes for `DISABLED_UPSTREAM` and `EXHAUSTED`.
- Do not add an error event table.
- Do not expose `disabled_report` as a normal admin-writable status through `UpdateKeyRequest`.
- Do not reintroduce the deleted `ReportSuccessCommand` / `ReportErrorCommand` wrappers; the report entrypoint is API route -> `KeyService`.

## Status Semantics

`AVAILABLE` means the key can be allocated.

`RATE_LIMITED` means the provider rejected use because the credential is temporarily rate limited. It has `cooldown_until` and is eligible for automatic recovery after the deadline.

`COOLDOWN` means the key had repeated transient execution failures. It has `cooldown_until` and is eligible for automatic recovery after the deadline.

`DISABLED_REPORT` means report evidence indicates the key is unhealthy enough to remove from normal allocation for longer than a short cooldown. It is not an admin lock and it does not mean the credential is permanently invalid. Stale credential refresh can automatically restore it when provider checks succeed.

`DISABLED_ADMIN` means an administrator manually disabled the key. Background refresh, report success, and report error must not automatically restore it.

## Error Classification

`KeyService.report_error(key_id, lease_id, error_type)` should normalize `error_type` to lower-case snake-like text and classify it.

Rate-limit errors:

- `rate_limit`
- `too_many_requests`
- `429`

Fatal credential errors:

- `invalid_api_key`
- `unauthorized`
- `forbidden`
- `account_disabled`
- `credential_revoked`
- `oauth_refresh_failed`

Transient errors:

- `network_timeout`
- `upstream_5xx`
- `connection_error`
- `execution_failed`
- unknown values by default

Unknown errors default to transient so a new or poorly mapped error does not immediately disable a usable credential.

## Required Data

The current `ApiKey` only has aggregate `success_count` and `error_count`. Add report-state fields:

- `consecutive_error_count: int = 0`
- `cooldown_failure_rounds: int = 0`
- `rate_limit_rounds: int = 0`
- `last_report_error_type: str | None = None`

Persistence changes:

- Add matching columns to `ApiKeyModel`.
- Map the fields in `SqlAlchemyKeyRepository._to_entity()` and `upsert_key()`.
- Include the fields in runtime/background snapshot persistence when report state should be copied.
- Extend PostgreSQL bootstrap with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for the new columns and backfill integer columns to `0`.
- Extend SQLite bootstrap with `_ensure_sqlite_column()` calls and backfill integer columns to `0`.
- Update `tests/fakes.py` to store and copy the fields.

Response schema changes are optional. If admins need visibility, add the fields to `KeyResponse`; otherwise keep them internal.

## Settings

Add settings:

- `REPORT_TRANSIENT_FAILURE_THRESHOLD=5`
- `REPORT_COOLDOWN_DISABLE_ROUNDS=3`
- `REPORT_BACKOFF_MINUTES=1,2,5,10`

Use the same backoff sequence for `RATE_LIMITED` and `COOLDOWN`.

`KeyStateMachine` should own report-state transitions. `container.py` should pass the new settings into `KeyStateMachine`.

## Backoff Rules

For `RATE_LIMITED`, increment `rate_limit_rounds` and use it as the backoff index. Cap the index at the last configured value.

For `COOLDOWN`, increment `cooldown_failure_rounds` whenever transient failures reach the threshold. Use the round count as the backoff index and cap it at the last configured value.

Current `recover_if_ready()` should preserve the existing temporary status when `cooldown_until > now`. It should not convert `RATE_LIMITED` into `COOLDOWN`.

Expired `RATE_LIMITED` / `COOLDOWN` keys recover to `AVAILABLE` and clear `cooldown_until`.

## Report Error Flow

Current flow:

1. Load key.
2. `repository.record_error()`.
3. Sync allocation store.
4. Call `plugin.mark_error(...)`.
5. Release the lease.

New flow:

1. Load key.
2. Increment aggregate error fields.
3. If current status is `DISABLED_ADMIN`, preserve status and only persist aggregate counters.
4. Classify `error_type`.
5. Apply report-state transition.
6. Persist aggregate counters, report counters, `status`, `cooldown_until`, `last_report_error_type`, and `updated_at`.
7. Sync allocation store using the persisted key.
8. Call `plugin.mark_error(...)`.
9. Release the lease.

State transitions:

- fatal credential error: set `DISABLED_REPORT`, clear `cooldown_until`, set `last_report_error_type`
- rate-limit error: set `RATE_LIMITED`, set `cooldown_until = now + backoff`, increment `rate_limit_rounds`, set `last_report_error_type`
- transient error: increment `consecutive_error_count`; when it reaches 5, reset it to 0, increment `cooldown_failure_rounds`, and either set `COOLDOWN` or upgrade to `DISABLED_REPORT` when the new round count is at least 3

When upgrading to `DISABLED_REPORT`, clear `cooldown_until`.

## Report Success Flow

Current success reporting should still increment `success_count`, add `tokens_used`, update `last_used_at`, call `plugin.mark_success(...)`, release the lease, and sync the allocation store.

Add report counter cleanup:

- set `consecutive_error_count = 0`
- set `rate_limit_rounds = 0`
- keep `cooldown_failure_rounds` unchanged
- do not automatically restore `DISABLED_ADMIN`
- do not restore `DISABLED_REPORT`; credential refresh is responsible for proving recovery

Keeping `cooldown_failure_rounds` unchanged prevents one late success report from erasing the escalation history that leads to `DISABLED_REPORT`.

## Refresh Semantics

`refresh_keys()` has two runtime paths and they should remain separate.

Stale refresh path:

- candidates come from `list_keys_needing_refresh(cutoff)` plus stale `PENDING` keys
- after acquiring the runtime lock and reading latest state, skip `DISABLED_ADMIN`
- do not skip `DISABLED_REPORT`
- do not skip active `RATE_LIMITED` / `COOLDOWN`
- run provider preflight, availability, capacity, and model sync as appropriate
- allow refresh results to replace `DISABLED_REPORT`, `RATE_LIMITED`, `COOLDOWN`, `DISABLED_UPSTREAM`, `EXHAUSTED`, or `PENDING` with the provider-derived state

Fresh availability check path:

- candidates come from the same `list_keys()` snapshot used to find stale pending keys
- skip keys already in stale refresh candidates
- skip `DISABLED_ADMIN` and `DISABLED_REPORT`
- skip active temporary blocks where `status in {RATE_LIMITED, COOLDOWN}` and `cooldown_until > now`
- after acquiring the runtime lock, re-read latest state and repeat the same skip checks
- if the plugin is missing, skip without changing status; this path is only an availability check and should not mark plugin-missing keys unavailable

The function docstring should describe both paths, not only stale cache refresh.

## DISABLED_REPORT Refresh Recovery

The stale refresh path is the automatic recovery path for `DISABLED_REPORT`; a separate `refresh_report_disabled_keys()` is not required.

`DISABLED_REPORT` must remain non-allocatable while it is in that state. Recovery happens only after a stale refresh acquires the runtime lock, re-reads the latest row, runs provider checks, persists the new runtime snapshot, and syncs the allocation store.

Implementation requirements:

- `_merge_refresh_signals_into_status()` must allow `DISABLED_REPORT` to transition based on fresh provider signals when called from stale credential refresh.
- `_merge_runtime_mutation()` or the background persist path must allow a stale refresh mutation to overwrite `DISABLED_REPORT`.
- `update_background_runtime_snapshot_if_locked()` needs a variant or flag that allows background writes when the current row is `DISABLED_REPORT`, but only for the stale refresh path and only while the caller holds the lock.
- `DISABLED_ADMIN` must stay protected in every path.

Recovery result:

- credential unavailable or preflight failed: set `DISABLED_UPSTREAM`
- credential available and quota unavailable: set `EXHAUSTED`
- credential available and quota available or unknown: set `AVAILABLE`

Fresh provider signals replace `DISABLED_REPORT`, because `DISABLED_REPORT` is not a manual lock.

## Repository Changes

`record_error()` currently cannot persist status transitions. Keep state policy out of repository internals and add explicit report persistence methods:

`record_error_report_state(key: ApiKey, now: datetime) -> ApiKey | None`

`record_success_report_state(key: ApiKey, now: datetime) -> ApiKey | None`

`record_error_report_state` should persist:

- aggregate error fields
- report counters
- `last_report_error_type`
- `status`
- `cooldown_until`
- `updated_at`

`record_success_report_state` should persist:

- aggregate success fields
- `quota_used`
- `last_used_at`
- cleared report counters
- `updated_at`

The existing `update_status()` remains for admin status changes and cooldown recovery.

For `DISABLED_REPORT` stale refresh, add one of:

- `update_background_runtime_snapshot_if_locked(..., allow_report_disabled=True)`
- `update_report_disabled_runtime_snapshot_if_locked(...)`

The explicit method is clearer and avoids accidentally weakening ordinary background write protections.

## Allocation Store Impact

Allocation stores should not set `RATE_LIMITED` for local concurrency saturation.

Redis allocation already checks active leases and `max_concurrent_uses` in `allocate.lua`. SQLite and DB allocation do the same with `KeyLeaseModel`.

Allocation stores only need to receive synced `RATE_LIMITED`, `COOLDOWN`, and `DISABLED_REPORT` statuses from `KeyService`. `RedisKeyCache.sync_key()` already writes `status` and `cooldown_until`.

Before `cooldown_until`, `RATE_LIMITED` and `COOLDOWN` are not allocatable. `DISABLED_REPORT` is not allocatable. `DISABLED_ADMIN` is not allocatable.

## API Contract

`ReportErrorRequest` keeps `key_id`, `lease_id`, and `error_type`.

`ReportSuccessRequest` keeps `key_id`, `lease_id`, and `tokens_used`.

`UpdateKeyRequest` should continue allowing only `AVAILABLE` and `DISABLED_ADMIN`.

The deleted `ReportSuccessCommand` and `ReportErrorCommand` wrappers should stay deleted unless a real caller is added.

## Testing Plan

Domain tests:

- `report_error("rate_limit")` sets `RATE_LIMITED`, sets `cooldown_until`, increments `rate_limit_rounds`, and releases the lease
- repeated `rate_limit` reports use 1/2/5/10 minute backoff
- five transient reports set `COOLDOWN`
- three cooldown rounds upgrade to `DISABLED_REPORT`
- fatal credential error directly sets `DISABLED_REPORT`
- `report_success()` clears `consecutive_error_count` and `rate_limit_rounds` without clearing `cooldown_failure_rounds`
- `report_success()` does not restore `DISABLED_REPORT` or `DISABLED_ADMIN`
- `recover_cooldowns()` restores expired `RATE_LIMITED` and `COOLDOWN`
- `recover_if_ready()` preserves `RATE_LIMITED` when `cooldown_until > now`
- stale refresh can restore `DISABLED_REPORT` to `AVAILABLE`
- stale refresh can move `DISABLED_REPORT` to `EXHAUSTED`
- stale refresh can replace `DISABLED_REPORT` with `DISABLED_UPSTREAM` when provider checks fail
- fresh availability check skips active `RATE_LIMITED` / `COOLDOWN`
- fresh availability check skips `DISABLED_REPORT`

Repository tests:

- SQLAlchemy maps new report fields both directions
- PostgreSQL bootstrap adds and backfills new columns
- SQLite bootstrap adds and backfills new columns
- in-memory fake preserves report fields across runtime snapshot updates
- report-disabled stale refresh persistence can write when the lock is held and current status is still `DISABLED_REPORT`
- ordinary background persistence still refuses to overwrite `DISABLED_ADMIN`

API tests:

- `/internal/report-error` returns the new status for rate-limit, cooldown, and disabled-report transitions
- `/internal/report-success` accepts `lease_id` and releases only that lease
- `/api/keys/{id}` still rejects `disabled_report` in the admin update payload

Allocation tests:

- a key at `max_concurrent_uses` is not allocated again and its status remains unchanged
- `RATE_LIMITED` and `COOLDOWN` keys are not allocated before `cooldown_until`
- `DISABLED_REPORT` keys are not allocated
- after stale refresh restores a `DISABLED_REPORT` key to `AVAILABLE`, it can be allocated again

## Self-Review

- The design keeps local lease saturation separate from provider rate limiting.
- The design reflects the current two-path `refresh_keys()` structure.
- `DISABLED_REPORT` is recoverable through stale credential refresh.
- `DISABLED_ADMIN` remains protected from automatic recovery.
- The implementation requirements name the existing helpers that must change for `DISABLED_REPORT` refresh recovery to work.
