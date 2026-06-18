"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: OpenAI Codex OAuth 提供商插件
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from infrastructure.plugins.base import (
    CapacitySignal,
    CredentialPreparationResult,
    EgressMode,
    ProviderPlugin,
)

_AUTH_ORIGIN = "https://auth.openai.com"
_TOKEN_URL = f"{_AUTH_ORIGIN}/oauth/token"
_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_VERSION = "0.124.0"

_BASE_MODELS: list[str] = [
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-image-2",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CodexOauthPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "codex_oauth"

    @property
    def description(self) -> str:
        return (
            "OpenAI Codex OAuth provider (chatgpt.com backend-api). "
            "Credential is OAuth token JSON, not single API key."
        )

    @property
    def auth_type(self) -> str:
        return "oauth_json"

    @property
    def credential_hint(self) -> str:
        return (
            '{"access_token":"...",'
            '"id_token":"...",'
            '"refresh_token":"...",'
            '"account_id":"...",'
            '"email":"...",'
            '"expired":"2026-04-09T13:00:00Z"} '
            '(OpenAI Codex OAuth credential JSON)'
        )

    @property
    def model_source(self) -> str:
        return "remote"

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_AUTH_ORIGIN, httpx.AsyncClient)

    @staticmethod
    def _decode_jwt_payload(id_token: str) -> dict[str, Any]:
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError("invalid id_token format")
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload_raw = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(payload_raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid id_token payload")
        return payload

    @staticmethod
    def _parse_expired_at(expired_value: str | None) -> datetime | None:
        if not expired_value:
            return None
        try:
            return datetime.fromisoformat(expired_value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _safe_iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _mask_token(token: str | None) -> str:
        if not token:
            return "missing"
        return token[:8] + "***"

    @staticmethod
    def _is_near_expiry(credential: dict[str, Any], near_minutes: int = 20) -> bool:
        expired_at = CodexOauthPlugin._parse_expired_at(credential.get("expired"))
        if expired_at is None:
            return False
        return expired_at <= (_utc_now() + timedelta(minutes=near_minutes))

    def _normalize_credential(self, credential: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(credential)
        access_token = str(normalized.get("access_token") or "")
        refresh_token = str(normalized.get("refresh_token") or "")
        if not access_token and not refresh_token:
            raise ValueError("credential.access_token or credential.refresh_token is required")
        account_id = str(normalized.get("account_id") or "")
        email = str(normalized.get("email") or "")

        id_token = str(normalized.get("id_token") or "")
        if id_token:
            claims = self._decode_jwt_payload(id_token)
            auth_claims = claims.get("https://api.openai.com/auth")
            auth_claims_dict = auth_claims if isinstance(auth_claims, dict) else {}
            account_id = (
                account_id
                or str(auth_claims_dict.get("chatgpt_account_id") or "")
                or str(claims.get("sub") or "")
            )
            email = email or str(claims.get("email") or "")

        if not account_id:
            raise ValueError("credential.account_id or credential.id_token is required")

        normalized["account_id"] = account_id
        normalized["email"] = email
        normalized["type"] = "codex"
        return normalized

    async def _refresh_credential(self, credential: dict[str, Any]) -> dict[str, Any]:
        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            raise ValueError("credential.refresh_token is required for token refresh")

        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": _CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()

        access_token = str(payload.get("access_token") or "")
        id_token = str(payload.get("id_token") or credential.get("id_token") or "")
        new_refresh_token = str(payload.get("refresh_token") or refresh_token)
        expires_in = int(payload.get("expires_in") or 3600)
        expired_at = _utc_now() + timedelta(seconds=max(expires_in, 1))

        refreshed_input = {
            **credential,
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": new_refresh_token,
            "expired": self._safe_iso(expired_at) or "",
            "last_refresh": self._safe_iso(_utc_now()) or "",
        }
        if id_token:
            refreshed_input.pop("account_id", None)
            refreshed_input.pop("email", None)

        refreshed = self._normalize_credential(refreshed_input)
        return refreshed

    def _is_oauth_credential_fresh(self, credential: dict[str, Any]) -> bool:
        normalized = self._normalize_credential(credential)
        if normalized.get("refresh_token"):
            if not normalized.get("access_token"):
                return False
            return not self._is_near_expiry(normalized)
        return bool(normalized.get("access_token"))

    async def _refresh_oauth_credential(self, credential: dict[str, Any]) -> dict[str, Any] | None:
        try:
            normalized = self._normalize_credential(credential)
            return await self._refresh_credential(normalized)
        except Exception:
            return None

    async def prepare_credential(self, credential: dict[str, Any]) -> CredentialPreparationResult:
        normalized = self._normalize_credential(credential)
        return CredentialPreparationResult(
            credential=normalized,
            changed=normalized != credential,
        )

    @staticmethod
    def _usage_headers(runtime_credential: dict[str, Any]) -> dict[str, Any]:
        return {
            "user-agent": (
                f"codex-tui/{_CODEX_VERSION} "
                f"(Windows 10.0.26100; x86_64) WindowsTerminal "
                f"(codex-tui; {_CODEX_VERSION})"
            ),
            "authorization": f"Bearer {runtime_credential['access_token']}",
            "chatgpt-account-id": str(runtime_credential["account_id"]),
            "accept": "*/*",
            "host": "chatgpt.com",
            "Connection": "close",
        }

    async def _retrieve_usage(self, runtime_credential: dict[str, Any]) -> httpx.Response | None:
        try:
            async with self._build_http_client(httpx.AsyncClient) as client:
                return await client.get(
                    _USAGE_URL,
                    headers=self._usage_headers(runtime_credential),
                )
        except Exception:
            return None

    @staticmethod
    def _usage_models_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
        rate_limit = payload.get("rate_limit")
        if not isinstance(rate_limit, dict):
            return None
        primary_window = rate_limit.get("primary_window")
        if not isinstance(primary_window, dict):
            return None

        try:
            used_percent = float(primary_window.get("used_percent"))
        except (TypeError, ValueError):
            return None

        remaining = 1.0 - (used_percent / 100.0)
        remaining = max(0.0, min(1.0, remaining))
        reset_time_raw = primary_window.get("reset_at")
        reset_time = None
        if isinstance(reset_time_raw, (int, float)):
            reset_time = datetime.fromtimestamp(float(reset_time_raw), tz=timezone.utc).isoformat()

        return {
            "default": {
                "remaining": remaining,
                "resetTime": reset_time,
                "resetTimeRaw": reset_time_raw,
            }
        }

    @staticmethod
    def _capacity_signal_from_usage_payload(payload: dict[str, Any]) -> CapacitySignal | None:
        models = CodexOauthPlugin._usage_models_from_payload(payload)
        if not models:
            return None
        remaining = float(models["default"]["remaining"])
        return CapacitySignal(
            has_capacity_signal=True,
            capacity_score=remaining,
            quota_available=remaining > 0.0,
            capacity_kind="remaining_ratio",
            reason=f"plan_type={payload.get('plan_type', 'unknown')}",
        )

    async def fetch_models(self, credential: dict[str, Any]) -> list[str]:
        fast_models = [f"{m}-fast" for m in _BASE_MODELS]
        return list(dict.fromkeys([*_BASE_MODELS, *fast_models]))

    async def is_credential_available(self, credential: dict[str, Any]) -> bool:
        response = await self._retrieve_usage(credential)
        if response is None or not response.is_success:
            return False
        return True

    async def get_capacity_signal(self, credential: dict[str, Any]) -> CapacitySignal | None:
        response = await self._retrieve_usage(credential)
        if response is None:
            return None
        if not response.is_success:
            return None

        try:
            payload = response.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return self._capacity_signal_from_usage_payload(payload)

    async def explain_credential(self, credential: dict[str, Any]) -> dict:
        try:
            normalized = self._normalize_credential(credential)
            status = "ok"
        except Exception:
            normalized = dict(credential)
            status = "invalid_credential"

        return {
            "provider": self.name,
            "status": status,
            "model_source": self.model_source,
            "auth_type": self.auth_type,
            "credential_hint": self._mask_token(normalized.get("access_token")),
            "email": normalized.get("email", ""),
            "account_id": normalized.get("account_id", ""),
            "type": normalized.get("type", ""),
            "expired": normalized.get("expired"),
            "has_refresh_token": bool(normalized.get("refresh_token")),
        }
