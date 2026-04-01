from enum import StrEnum


class KeyStatus(StrEnum):
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    DISABLED_UPSTREAM = "disabled_upstream"
    DISABLED_ADMIN = "disabled_admin"
    DISABLED_REPORT = "disabled_report"
    EXHAUSTED = "exhausted"
