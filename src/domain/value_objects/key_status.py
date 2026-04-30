"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-21
@Description: Key 状态枚举
"""
from enum import StrEnum


class KeyStatus(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    DISABLED_UPSTREAM = "disabled_upstream"
    DISABLED_ADMIN = "disabled_admin"
    DISABLED_REPORT = "disabled_report"
    EXHAUSTED = "exhausted"
