"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-28
@Description: 模型别名解析服务
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ModelAliasResolver:
    aliases_by_model: dict[str, dict[str, list[str]]]

    @classmethod
    def empty(cls) -> "ModelAliasResolver":
        return cls(aliases_by_model={})

    @classmethod
    def from_yaml_file(cls, path: str | None) -> "ModelAliasResolver":
        if not path:
            return cls.empty()

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if data.get("version") != 1:
            raise ValueError("model alias config version must be 1")

        models = data.get("models")
        if not isinstance(models, dict):
            raise ValueError("model alias config models must be a mapping")

        normalized: dict[str, dict[str, list[str]]] = {}
        for requested_model, config in models.items():
            providers = (config or {}).get("providers")
            if not isinstance(providers, dict) or not providers:
                raise ValueError(f"model {requested_model} must define providers")

            provider_aliases: dict[str, list[str]] = {}
            for provider, aliases in providers.items():
                if not isinstance(aliases, list) or not aliases:
                    raise ValueError(f"provider {provider} must define at least one alias")
                alias_list = [str(alias).strip() for alias in aliases if str(alias).strip()]
                if not alias_list:
                    raise ValueError(f"provider {provider} must define at least one alias")
                provider_aliases[str(provider).strip().lower()] = alias_list

            normalized[str(requested_model).strip().lower()] = provider_aliases

        return cls(aliases_by_model=normalized)

    def resolve_provider_model(
        self,
        requested_model: str,
        provider: str,
        supported_models: list[str],
    ) -> str | None:
        requested_model = requested_model.strip()
        normalized_model = requested_model.lower()
        normalized_provider = provider.strip().lower()
        configured_aliases = self.aliases_by_model.get(normalized_model, {}).get(normalized_provider)

        supported_lookup = {model.lower(): model for model in supported_models}
        if configured_aliases is None:
            if supported_models and normalized_model not in supported_lookup:
                return None
            if not supported_models:
                return requested_model
            return supported_lookup[normalized_model]

        if not supported_models:
            return configured_aliases[0]

        aliases = [requested_model, *configured_aliases]
        deduped_aliases: list[str] = []
        seen_aliases: set[str] = set()
        for alias in aliases:
            normalized_alias = alias.lower()
            if normalized_alias in seen_aliases:
                continue
            seen_aliases.add(normalized_alias)
            deduped_aliases.append(alias)

        for alias in deduped_aliases:
            matched = supported_lookup.get(alias.lower())
            if matched is not None:
                return matched
        return None
