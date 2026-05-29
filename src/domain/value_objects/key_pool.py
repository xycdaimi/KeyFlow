"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: Key 池级别枚举
"""
from __future__ import annotations

from enum import Enum


class KeyPool(str, Enum):
    DEFAULT = "default"
    VIP = "vip"
