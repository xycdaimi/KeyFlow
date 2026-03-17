from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["ops"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness and readiness probe for K8s / Docker health checks."""
    return HealthResponse(status="ok")
