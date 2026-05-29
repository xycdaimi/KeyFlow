"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 子节点地址校验
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_node_base_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("base_url host is required")
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must be an origin without path")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not include query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
