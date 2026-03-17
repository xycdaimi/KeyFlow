from enum import StrEnum


class KeyStatus(StrEnum):
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"
    EXHAUSTED = "exhausted"
