"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-03-20
@Description: 健康检查路由：依赖就绪状态（应用、数据库、Redis）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["ops"])
CHECK_ORDER = ("app", "database", "redis")


async def _run_health_checks(request: Request) -> tuple[int, dict[str, Any]]:
    checkers = getattr(request.app.state, "health_checkers", None) or {}
    checks: dict[str, dict[str, Any]] = {}
    all_ok = True
    for name in CHECK_ORDER:
        fn = checkers.get(name)
        if fn is None:
            checks[name] = {"status": "error", "detail": "not_configured"}
            all_ok = False
            continue
        try:
            ok, detail = await fn()
        except Exception as exc:
            checks[name] = {"status": "error", "detail": str(exc)}
            all_ok = False
            continue
        if ok:
            checks[name] = {"status": "ok", "detail": detail}
        else:
            checks[name] = {"status": "error", "detail": detail}
            all_ok = False
    overall = "ok" if all_ok else "degraded"
    status_code = 200 if all_ok else 503
    return status_code, {"status": overall, "checks": checks}


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Readiness probe: runs app / database / Redis checks from app.state.health_checkers."""
    status_code, body = await _run_health_checks(request)
    return JSONResponse(status_code=status_code, content=body)
