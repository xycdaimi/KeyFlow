# Error Handling Governance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a complete, uniform API error-handling system with standardized error responses, global exception handlers, clear exception boundaries, and regression tests for known and unknown failures.

**Architecture:** Keep business semantics in `domain` and `application`, move HTTP rendering of errors into centralized FastAPI exception handlers, and reduce route-level `try/except` to the minimum. Use a shared error response schema plus an exception-to-status/code mapping table so all routes, validation failures, auth failures, startup failures, and unexpected errors behave consistently.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, pytest, existing DDD + CQRS layering

---

### Task 1: Define The Error Contract First

**Files:**
- Create: `src/interfaces/schemas/error.py`
- Modify: `tests/test_api.py`
- Modify: `src/interfaces/schemas/response.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Add API tests that assert the new uniform error body shape:

```python
def test_create_key_returns_standard_error_body_for_duplicate_credential() -> None:
    client = build_client()

    response = client.post(
        "/api/providers/openai/keys",
        json={"credential": {"api_key": "sk-test"}},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "duplicate_credential",
        "message": "credential already exists for this provider",
        "details": {"provider": "openai"},
    }
```

```python
def test_allocate_key_returns_standard_error_body_for_no_available_key() -> None:
    client = build_client(plugin_available=False)

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "no_available_key"
```

```python
def test_request_validation_error_uses_standard_error_body() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/report-success",
        json={"key_id": "key-1", "tokens_used": -1},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_error"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "standard_error_body or validation_error" -v`

Expected: FAIL because responses still use `detail` and there is no shared error schema.

**Step 3: Add the minimal schema implementation**

Create `src/interfaces/schemas/error.py`:

```python
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict | None = None
```

Optionally re-export from `src/interfaces/schemas/response.py` if you want one schema import surface.

**Step 4: Run tests to verify import/schema wiring works**

Run: `pytest tests/test_api.py -k "standard_error_body or validation_error" -v`

Expected: Tests still FAIL on behavior, but no import errors remain.

**Step 5: Commit**

```bash
git add src/interfaces/schemas/error.py src/interfaces/schemas/response.py tests/test_api.py
git commit -m "test: define standard API error contract"
```

---

### Task 2: Introduce Global Exception Mapping

**Files:**
- Create: `src/interfaces/api/error_handlers.py`
- Modify: `src/interfaces/api/app.py`
- Modify: `src/domain/exceptions/domain_exceptions.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Add tests for known domain exceptions and unknown exceptions being converted centrally:

```python
def test_key_not_found_uses_global_handler() -> None:
    client = build_client()

    response = client.get("/api/keys/not-exists")

    assert response.status_code == 404
    assert response.json() == {
        "code": "key_not_found",
        "message": "key was not found",
        "details": {"key_id": "not-exists"},
    }
```

```python
def test_unknown_exception_returns_standard_500(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client()
    service: KeyService = client.app.state.container.resolve(KeyService)

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "get_key", _boom, raising=False)

    response = client.get("/api/keys/key-1")

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_server_error",
        "message": "internal server error",
        "details": None,
    }
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "global_handler or internal_server_error" -v`

Expected: FAIL because app has no exception handlers registered.

**Step 3: Implement the handler module**

Create `src/interfaces/api/error_handlers.py` with:

```python
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from domain.exceptions.domain_exceptions import (
    DomainError,
    DuplicateCredentialError,
    InvalidStateTransitionError,
    KeyNotFoundError,
    NoAvailableKeyError,
)
from interfaces.schemas.error import ErrorResponse


DOMAIN_ERROR_MAP = {
    KeyNotFoundError: (404, "key_not_found", "key was not found"),
    NoAvailableKeyError: (404, "no_available_key", "no key is currently available"),
    DuplicateCredentialError: (409, "duplicate_credential", "credential already exists for this provider"),
    InvalidStateTransitionError: (409, "invalid_state_transition", "requested state transition is not allowed"),
}
```

And register handlers for:
- `DomainError`
- `RequestValidationError`
- `HTTPException`
- generic `Exception`

**Step 4: Register handlers in app startup**

Modify `src/interfaces/api/app.py`:

```python
from interfaces.api.error_handlers import register_exception_handlers

app = FastAPI(...)
register_exception_handlers(app)
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/test_api.py -k "global_handler or internal_server_error" -v`

Expected: PASS

**Step 6: Commit**

```bash
git add src/interfaces/api/error_handlers.py src/interfaces/api/app.py src/domain/exceptions/domain_exceptions.py tests/test_api.py
git commit -m "feat: add global API exception handlers"
```

---

### Task 3: Standardize Auth And Validation Errors

**Files:**
- Modify: `src/interfaces/middleware/auth.py`
- Modify: `src/interfaces/api/error_handlers.py`
- Modify: `tests/test_api.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Add explicit auth error assertions:

```python
def test_internal_auth_uses_standard_error_body() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "wrong"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "code": "unauthorized",
        "message": "invalid internal key",
        "details": None,
    }
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_internal_auth_uses_standard_error_body -v`

Expected: FAIL because current body is `{"detail": "invalid internal key"}`.

**Step 3: Implement the minimal auth/HTTPException standardization**

Keep `verify_internal_key()` raising `HTTPException`, but make the global `HTTPException` handler normalize the body:

```python
if exc.status_code == status.HTTP_401_UNAUTHORIZED:
    payload = ErrorResponse(code="unauthorized", message=str(exc.detail), details=None)
```

For `RequestValidationError`, build:

```python
ErrorResponse(
    code="request_validation_error",
    message="request validation failed",
    details={"errors": exc.errors()},
)
```

**Step 4: Run targeted tests**

Run: `pytest tests/test_api.py -k "internal_auth or validation_error" -v`

Expected: PASS

**Step 5: Commit**

```bash
git add src/interfaces/middleware/auth.py src/interfaces/api/error_handlers.py tests/test_api.py
git commit -m "feat: standardize auth and validation errors"
```

---

### Task 4: Remove Route-Level Error Translation

**Files:**
- Modify: `src/interfaces/api/routes/allocate.py`
- Modify: `src/interfaces/api/routes/report.py`
- Modify: `src/interfaces/api/routes/admin.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Before deleting route-level `try/except`, add or update tests so they only assert final HTTP behavior, not route-local implementation details.

Example:

```python
def test_report_success_returns_standard_not_found_error() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/report-success",
        json={"key_id": "missing", "tokens_used": 1},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "key_not_found"
```

**Step 2: Run the targeted tests**

Run: `pytest tests/test_api.py -k "not_found or duplicate_credential or no_available_key" -v`

Expected: PASS before refactor.

**Step 3: Remove local exception mapping**

Refactor routes from:

```python
try:
    key = await service.report_success(...)
except KeyNotFoundError as exc:
    raise HTTPException(...)
```

to:

```python
key = await service.report_success(...)
```

Do this in:
- `src/interfaces/api/routes/allocate.py`
- `src/interfaces/api/routes/report.py`
- `src/interfaces/api/routes/admin.py`

**Step 4: Run tests to verify behavior is unchanged**

Run: `pytest tests/test_api.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add src/interfaces/api/routes/allocate.py src/interfaces/api/routes/report.py src/interfaces/api/routes/admin.py tests/test_api.py
git commit -m "refactor: centralize API error translation"
```

---

### Task 5: Add Application-Level Infrastructure Error Wrappers

**Files:**
- Create: `src/application/exceptions/application_exceptions.py`
- Modify: `src/application/services/key_service.py`
- Modify: `src/interfaces/api/error_handlers.py`
- Modify: `tests/test_api.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Add tests for provider/repository/cache failures that should not leak raw stack semantics:

```python
def test_repository_failure_returns_standard_500(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client()
    service: KeyService = client.app.state.container.resolve(KeyService)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(service, "list_keys", _boom, raising=False)

    response = client.get("/api/providers/openai/keys")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_server_error"
```

Add at least one application wrapper path too:

```python
class InfrastructureUnavailableError(Exception):
    ...
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -k "repository_failure or internal_server_error" -v`

Expected: FAIL or produce unstable behavior.

**Step 3: Implement minimal wrapper exceptions**

Create `src/application/exceptions/application_exceptions.py`:

```python
class ApplicationError(Exception):
    pass


class InfrastructureUnavailableError(ApplicationError):
    pass
```

Wrap only the narrowest necessary operations in `KeyService`:
- repository writes
- Redis allocation calls
- plugin calls that are critical to serving the request

Do **not** wrap every `Exception` blindly; only wrap infrastructure boundaries where you want stable API semantics.

**Step 4: Map application exceptions in the global handler**

Example:

```python
InfrastructureUnavailableError: (503, "infrastructure_unavailable", "dependent service is temporarily unavailable")
```

**Step 5: Run targeted tests**

Run: `pytest tests/test_api.py -k "infrastructure_unavailable or internal_server_error" -v`

Expected: PASS

**Step 6: Commit**

```bash
git add src/application/exceptions/application_exceptions.py src/application/services/key_service.py src/interfaces/api/error_handlers.py tests/test_api.py
git commit -m "feat: add application-level infrastructure error mapping"
```

---

### Task 6: Define Startup And Background Error Strategy

**Files:**
- Modify: `src/interfaces/api/app.py`
- Modify: `tests/test_api.py`
- Modify: `README.md`
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Add tests that codify the intended behavior:

```python
@pytest.mark.anyio
async def test_startup_schema_failure_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    ...
```

```python
@pytest.mark.anyio
async def test_background_task_logs_and_continues_after_single_iteration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    ...
```

**Step 2: Run targeted tests**

Run: `pytest tests/test_api.py -k "startup_schema_failure or background_task" -v`

Expected: FAIL because behavior is not fully specified in tests yet.

**Step 3: Implement the strategy**

Recommended rules:
- Startup failures in `ensure_schema_ready()` remain fail-fast
- `ensure_refresh_columns()` failure remains fail-fast
- Background task exceptions are logged with structured fields and loop continues
- Unknown shutdown exceptions are swallowed only after logging

Improve logging shape in `src/interfaces/api/app.py`:

```python
logger.exception(
    "event=background_task_error source=keyflow phase=refresh_keys"
)
```

**Step 4: Run targeted tests**

Run: `pytest tests/test_api.py -k "startup_schema_failure or background_task" -v`

Expected: PASS

**Step 5: Commit**

```bash
git add src/interfaces/api/app.py tests/test_api.py README.md
git commit -m "docs: codify startup and background error policy"
```

---

### Task 7: Publish The Error Code Catalog

**Files:**
- Create: `docs/error-codes.md`
- Modify: `README.md`
- Test: `tests/test_api.py`

**Step 1: Write the failing test**

Add a single contract test that enumerates the main API codes so future changes must be explicit:

```python
def test_error_codes_remain_stable() -> None:
    assert {
        "duplicate_credential",
        "key_not_found",
        "no_available_key",
        "invalid_state_transition",
        "request_validation_error",
        "unauthorized",
        "internal_server_error",
    }
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_error_codes_remain_stable -v`

Expected: FAIL because the catalog/helper does not exist yet or the set is not documented in one place.

**Step 3: Write the docs**

Create `docs/error-codes.md` with:
- error code
- HTTP status
- meaning
- typical trigger
- whether retry is sensible

Update `README.md` to link the document.

**Step 4: Run tests**

Run: `pytest tests/test_api.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add docs/error-codes.md README.md tests/test_api.py
git commit -m "docs: publish API error code catalog"
```

---

### Task 8: Full Verification Sweep

**Files:**
- Modify: `tests/test_api.py`
- Modify: `tests/test_domain.py`
- Test: `tests/test_api.py`
- Test: `tests/test_domain.py`

**Step 1: Add any missing regression tests**

Add final matrix coverage for:
- duplicate credential
- key not found
- no available key
- unauthorized
- request validation error
- unknown exception
- invalid state transition (if introduced in API path)
- infrastructure unavailable

**Step 2: Run API test suite**

Run: `pytest tests/test_api.py -v`

Expected: PASS

**Step 3: Run domain test suite**

Run: `pytest tests/test_domain.py -v`

Expected: PASS

**Step 4: Run broader suite excluding known external-network tests**

Run: `pytest tests -v --ignore=tests/test_gemini_webapi.py`

Expected: PASS except pre-existing unrelated failures, if any.

**Step 5: Commit**

```bash
git add tests/test_api.py tests/test_domain.py
git commit -m "test: cover complete API error handling matrix"
```
