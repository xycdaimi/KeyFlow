## Background

Key registration currently blocks on provider-side validation before persisting the key. For slow providers, especially OAuth-based providers such as `gemini_oauth` and `codex_oauth`, this causes unstable request latency, upstream timeouts, duplicated runtime preparation, and confusing behavior where registration and runtime health probing are mixed into one synchronous path.

The goal of this design is to separate registration from runtime validation:

- key registration succeeds once the key is stored
- newly registered keys enter a temporary `pending` state
- an immediate async validation pass converges the key into a usable runtime state
- background refresh acts only as a fallback for stale `pending` keys and ongoing maintenance
- token refresh is forbidden by default and only allowed when the credential is expired or within a 5-minute expiry window

## Goals

- Make `POST /api/providers/{provider}/keys` return quickly after successful registration.
- Introduce a `pending` state for newly registered keys.
- Ensure `pending` keys never participate in allocation.
- Trigger immediate async validation after registration using `asyncio.create_task(...)`.
- Make background refresh skip fresh `pending` keys and only take over stale `pending` keys after 60 seconds.
- Restrict OAuth token refresh so it only happens when a credential is expired or within 5 minutes of expiry.
- Apply the OAuth refresh rules consistently to both `gemini_oauth` and `codex_oauth`.
- Keep static credential providers such as API-key providers on a simpler validation path without refresh semantics.

## Non-Goals

- No database-level uniqueness redesign in this change.
- No distributed job queue or durable task system in this change.
- No migration of existing keys into `pending`.
- No change to old key semantics; existing keys remain in their current validated states.

## Registration Semantics

`POST /api/providers/{provider}/keys` changes from "register and synchronously prove usability" to "register successfully and return immediately".

Successful registration means:

- provider exists
- provider runtime dependency is ready
- credential is not a duplicate according to current application-layer checks
- upstream root reachability probe passes
- provider-local lightweight credential preparation passes
- models are synchronized if required by the provider
- key record is persisted successfully

Successful registration does not mean:

- the credential is currently usable
- quota retrieval succeeded
- OAuth credential refresh succeeded
- runtime-only bootstrap values were discovered

## Status Model

Add a new `pending` status.

Meaning of `pending`:

- the key has been registered successfully
- immediate async validation has not yet converged the key into a final runtime state
- the key must not be allocated while pending

State transitions for newly registered keys:

- registration success -> `pending`
- async validation success with usable credential -> `available`
- async validation success with explicit zero quota -> `exhausted`
- async validation failure -> `disabled_upstream`

`pending` is intended to be short-lived. It is not a long-term operational state.

## Registration Flow

`KeyService.create_key()` should be reshaped into a registration-only path.

New flow:

1. Normalize and resolve provider.
2. Ensure provider exists and is ready.
3. Check duplicate credential using existing application-layer logic.
4. Call `verify_upstream_root_reachable()`.
5. Call lightweight `prepare_credential()`.
6. Build the new `ApiKey` entity with `status = pending`.
7. Call `fetch_models()`.
8. Persist the key.
9. Sync the allocation store.
10. Trigger immediate async validation via `asyncio.create_task(...)`.
11. Return success immediately.

Removed from the synchronous registration path:

- `_refresh_single_key()`
- `is_credential_available()`
- `get_capacity_signal()`
- runtime token refresh
- runtime bootstrap discovery

## Immediate Async Validation

After registration succeeds, the process should immediately schedule a per-key validation task with `asyncio.create_task(...)`.

The async validation task should:

1. Reload the key from the repository by `key_id`.
2. Exit if the key no longer exists.
3. Exit if the key is no longer `pending`.
4. Execute provider-specific runtime validation logic.
5. Converge the key status.
6. Persist the updated key and resync allocation state.

This task is intentionally process-local for this design. It is acceptable that it is not durable across process restarts. That gap is covered by the stale-`pending` fallback in the background refresh loop.

## Background Refresh Interaction

The existing background refresh loop remains in place, but `pending` keys are treated specially.

Rules:

- `pending` keys created within the last 60 seconds are skipped by background refresh
- `pending` keys older than 60 seconds are treated as stale and can be validated by the background refresh flow

This ensures:

- immediate validation gets priority in the common case
- process restart or lost in-memory tasks do not leave a key in `pending` forever

The 60-second cutoff is a hard part of this design, not a placeholder.

## Provider Categories

The design distinguishes between two provider categories.

### Static Credential Providers

Examples:

- `openai`
- `openrouter`
- `anthropic`
- cookie-based non-refresh credentials

Characteristics:

- no token refresh semantics
- async validation only probes runtime usability and optional capacity

Validation path:

- `is_credential_available()`
- `get_capacity_signal()` when supported

### Refreshable OAuth Providers

Examples:

- `gemini_oauth`
- `codex_oauth`

Characteristics:

- credential includes expiring access material
- runtime validation may need conditional refresh
- refresh must never be the default registration behavior

Validation path:

- runtime expiry check
- conditional refresh only when allowed
- runtime usability probe
- capacity probe when supported

## Provider Interface Semantics

`ProviderPlugin.prepare_credential()` must be narrowed to a pure local preprocessing hook.

Allowed responsibilities:

- normalize local credential structure
- inject stable local metadata
- strip non-persisted local fields
- validate required local fields

Forbidden responsibilities:

- HTTP requests
- token refresh
- runtime project discovery
- quota requests
- any other remote IO

Runtime-only preparation must live outside the registration hook. It can remain provider-local in this change; a new shared base interface is optional and not required for this design.

## OAuth Refresh Rules

For refreshable OAuth providers, token refresh is forbidden by default.

Rules:

- if `expiry_date` indicates the credential expires more than 5 minutes from now, refresh is forbidden
- if `expiry_date` is within 5 minutes of expiry, refresh is allowed
- if `expiry_date` is already expired, refresh is allowed and should be attempted before concluding the credential is unusable

This rule applies to:

- `gemini_oauth`
- `codex_oauth`

This rule does not apply to:

- static API-key providers
- fixed header/token providers without refresh semantics
- cookie-based providers without refresh semantics

## Avoiding Duplicate Refresh Work

Within a single async validation pass, runtime credential preparation must happen once.

Requirements:

- at most one refresh attempt per validation pass
- availability probing and capacity probing must reuse the same prepared runtime credential
- provider implementations must not independently re-run full runtime bootstrap inside both `is_credential_available()` and `get_capacity_signal()` during the same validation pass

This is required to avoid duplicate refreshes, duplicate project discovery, and unnecessary upstream load.

## Error Handling

### Registration-Phase Errors

These continue to fail the request synchronously:

- duplicate credential
- provider not found
- provider not ready
- upstream root unreachable
- local credential preprocessing failure

### Async Validation Errors

These do not fail registration, but do affect final key status.

Rules:

- runtime preparation failure -> `disabled_upstream`
- allowed refresh failure -> `disabled_upstream`
- explicit unusable credential result -> `disabled_upstream`
- explicit zero quota result -> `exhausted`
- capacity retrieval failure with successful availability probe -> keep key `available`, but treat capacity as unknown

The design intentionally distinguishes "credential unavailable" from "capacity unknown".

## Allocation Rules

Allocation must exclude `pending`.

Practical effect:

- new keys do not enter the schedulable candidate set until immediate async validation converges them into a schedulable state
- this avoids race conditions where a newly registered but not yet validated key is allocated immediately

## Existing Data

Existing keys are unaffected.

They remain in their current validated states and do not transition into `pending`. The `pending` state only applies to newly registered keys going forward.

## Testing Strategy

At minimum, the implementation must add or adjust coverage for:

- registration returns success without synchronous provider availability probing
- newly created keys persist as `pending`
- `pending` keys are excluded from allocation
- immediate async validation transitions `pending` to `available`
- immediate async validation transitions `pending` to `disabled_upstream`
- immediate async validation transitions `pending` to `exhausted` when quota is explicitly zero
- background refresh skips `pending` keys younger than 60 seconds
- background refresh takes over `pending` keys older than 60 seconds
- `gemini_oauth` does not refresh outside the 5-minute window
- `codex_oauth` does not refresh outside the 5-minute window
- refreshable providers may refresh within the 5-minute window
- refreshable providers may refresh once expired
- static providers never enter refresh logic

## Risks and Tradeoffs

- `asyncio.create_task(...)` is process-local and non-durable
- a process crash after persistence but before validation completion can leave a key in `pending` temporarily
- the 60-second background fallback is sufficient for this design, but not equivalent to a real distributed task queue

These tradeoffs are accepted because the goal is a minimal, focused redesign that removes synchronous registration latency without introducing a larger infrastructure change.

## Recommendation

Implement this design as the new standard registration and validation flow for all providers, with special runtime-refresh handling only for refreshable OAuth providers. This keeps the registration API fast and predictable while still converging keys into correct runtime states shortly after creation.

## Implementation Tasks

### Phase 1: Status and Registration Flow

- Add `pending` to `KeyStatus`.
- Ensure `pending` is exposed correctly in API responses and admin views.
- Refactor `KeyService.create_key()` into a registration-only path.
- Remove synchronous `_refresh_single_key()` execution from key creation.
- Keep duplicate checking, `verify_upstream_root_reachable()`, lightweight `prepare_credential()`, and `fetch_models()` in the registration path.
- Persist newly registered keys with `status = pending`.
- Return success immediately after persistence and allocation-store sync.

Completion criteria:

- newly registered keys return immediately
- newly registered keys persist as `pending`

### Phase 2: Allocation and Background Handling

- Exclude `pending` keys from allocation candidate collection.
- Make background refresh skip `pending` keys newer than 60 seconds.
- Make background refresh take over `pending` keys older than 60 seconds.

Completion criteria:

- `pending` is never allocatable
- stale `pending` keys are eventually revalidated

### Phase 3: Immediate Async Validation

- Add a single-key async validation entry point in `KeyService`.
- Trigger it with `asyncio.create_task(...)` after successful registration.
- Reload the key from storage by `key_id`.
- Only validate keys still in `pending`.
- Persist state convergence results back to the repository and allocation store.

Completion criteria:

- registration triggers immediate validation
- `pending` converges to a terminal runtime status

### Phase 4: Provider Boundary Tightening

- Narrow `ProviderPlugin.prepare_credential()` to a local preprocessing hook only.
- Explicitly forbid remote IO, refresh, project discovery, and quota calls inside `prepare_credential()`.

Completion criteria:

- registration path contains no hidden runtime-side remote actions

### Phase 5: OAuth Provider Refactor

- Refactor `gemini_oauth` runtime preparation.
- Refactor `codex_oauth` runtime preparation.
- Separate local preprocessing from runtime bootstrap.
- Ensure one runtime preparation per validation pass.
- Reuse the prepared runtime credential for both availability and capacity probing.

Completion criteria:

- no duplicate refresh in one validation pass
- no duplicate runtime bootstrap in one validation pass

### Phase 6: Refresh Window Enforcement

- For refreshable OAuth providers, forbid refresh when expiry is more than 5 minutes away.
- Allow refresh when expiry is within 5 minutes.
- Allow and attempt refresh when already expired.
- Ensure static credential providers never enter refresh logic.

Completion criteria:

- `gemini_oauth` and `codex_oauth` obey the 5-minute refresh window
- static providers remain refresh-free

### Phase 7: State Convergence Rules

- Converge to `disabled_upstream` when availability fails.
- Converge to `exhausted` when quota is explicitly zero.
- Converge to `available` when availability succeeds but capacity is unknown.
- Converge to `disabled_upstream` when runtime preparation fails.

Completion criteria:

- `pending` does not linger indefinitely
- capacity-unknown does not incorrectly disable usable keys

### Phase 8: Test Coverage

- Cover registration returning quickly with `pending`.
- Cover `pending` exclusion from allocation.
- Cover immediate async validation transitions to `available`.
- Cover immediate async validation transitions to `disabled_upstream`.
- Cover immediate async validation transitions to `exhausted`.
- Cover background skip for `pending` younger than 60 seconds.
- Cover background takeover for `pending` older than 60 seconds.
- Cover the 5-minute refresh window for `gemini_oauth`.
- Cover the 5-minute refresh window for `codex_oauth`.
- Cover that static providers do not enter refresh logic.

Completion criteria:

- the redesign is protected by focused regression coverage across registration, validation, scheduling, and OAuth refresh behavior
