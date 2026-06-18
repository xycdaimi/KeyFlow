#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-04
@Description: 通用 Qwen 图像编辑网关插件
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from typing import Any, Dict

from core.plugin.registry import register
from core.shared.credential_mode import CredentialMode
from core.shared.credential_utils import normalize_credential
from core.tools import proxy_context
from services.model_forwarder.infrastructure.plugin_accounting import (
    PluginAccountingMetadata,
    PluginExecutionResult,
    execute_with_account_pool,
    get_effective_accounting_provider,
)


TASK_TYPE = "qwen-image-edit"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"


def _require_string(payload: Dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"payload.{key} is required")
    return value.strip()


def _collect_images(payload: Dict[str, Any]) -> list[str]:
    value = payload.get("images")
    if not isinstance(value, list):
        raise ValueError("payload.images must be an array with 1 to 3 images")
    values = value

    images: list[str] = []
    for idx, value in enumerate(values):
        if isinstance(value, dict):
            image_url = value.get("url") or value.get("image") or value.get("image_url") or value.get("b64_json")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            value = image_url
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"payload image at index {idx} must be a non-empty string or image object")
        images.append(value.strip())

    if not images:
        raise ValueError("payload.images must contain at least 1 image")
    if len(images) > 3:
        raise ValueError("Qwen image edit supports at most 3 input images")
    return images


def _response_to_dict(response: Any) -> dict:
    if isinstance(response, dict):
        return response
    if hasattr(response, "to_dict"):
        value = response.to_dict()
        return value if isinstance(value, dict) else {"raw": value}
    try:
        return json.loads(json.dumps(response, ensure_ascii=False))
    except Exception:
        pass
    return {
        "status_code": getattr(response, "status_code", None),
        "request_id": getattr(response, "request_id", None),
        "code": getattr(response, "code", None),
        "message": getattr(response, "message", None),
        "output": getattr(response, "output", None),
        "usage": getattr(response, "usage", None),
    }


def _extract_usage_tokens(response: Any) -> tuple[int, int]:
    payload = _response_to_dict(response)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    input_token = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_token = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return max(0, input_token), max(0, output_token)


def _optional_int(params: Dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"inference_params.{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"inference_params.{key} must be an integer") from exc


def _run_business_logic(
    *,
    model_spec: Dict[str, Any],
    payload: Dict[str, Any],
    inference_params: Dict[str, Any],
) -> PluginExecutionResult:
    with proxy_context():
        images = _collect_images(payload)
        prompt = _require_string(payload, "prompt")
        negative_prompt = str(payload.get("negative_prompt") or "").strip()

        credential = normalize_credential(model_spec.get("credential"))
        api_key = credential.get("api_key") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_IMAGE_EDIT_API_KEY") or ""
        base_url = str(model_spec.get("endpoint") or os.getenv("QWEN_IMAGE_EDIT_BASE_URL") or DEFAULT_BASE_URL).strip()
        model_name = str(model_spec.get("name") or os.getenv("QWEN_IMAGE_EDIT_MODEL") or "qwen-image-edit-plus").strip()
        if not api_key:
            raise ValueError("model_spec.credential.api_key, DASHSCOPE_API_KEY or QWEN_IMAGE_EDIT_API_KEY is required")
        if not model_name:
            raise ValueError("model_spec.name or QWEN_IMAGE_EDIT_MODEL is required")
        import dashscope
        from dashscope import MultiModalConversation

        dashscope.base_http_api_url = base_url
        call_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "model": model_name,
            "messages": [{"role": "user", "content": [{"image": image} for image in images] + [{"text": prompt}]}],
            "stream": False,
            "n": _optional_int(inference_params, "n") or 1,
            "watermark": bool(inference_params.get("watermark", False)),
            "negative_prompt": negative_prompt or " ",
            "prompt_extend": bool(inference_params.get("prompt_extend", True)),
        }
        seed = _optional_int(inference_params, "seed")
        if seed is not None:
            call_kwargs["seed"] = seed
        size = inference_params.get("size")
        if isinstance(size, str) and size.strip():
            call_kwargs["size"] = size.strip()

        response = MultiModalConversation.call(**call_kwargs)
        status_code = getattr(response, "status_code", None)
        if status_code not in (None, HTTPStatus.OK, 200):
            raise RuntimeError(
                f"qwen image edit failed: status_code={status_code}, "
                f"code={getattr(response, 'code', None)}, message={getattr(response, 'message', None)}"
            )
        response_payload = _response_to_dict(response)
        input_token, output_token = _extract_usage_tokens(response)
        return PluginExecutionResult(
            output=response_payload,
            accounting=PluginAccountingMetadata(get_effective_accounting_provider(), model_name, input_token, output_token),
        )


@register(
    TASK_TYPE,
    worker_threads=2,
    credential_mode=CredentialMode.CALLER_OR_POOL,
    metadata_version=1,
    timeout_seconds=600,
    description="Generic Qwen image edit plugin. The caller provides image, prompt, optional negative_prompt, and model_spec.name.",
    request_example={
        "task_type": TASK_TYPE,
        "model_spec": {
            "name": "qwen-image-edit-max",
            "endpoint": "https://dashscope.aliyuncs.com/api/v1",
            "credential": {"api_key": "dashscope-key"},
        },
        "payload": {
            "images": [
                "https://example.com/reference-product.png",
                "https://example.com/reference-style.png",
            ],
            "prompt": "Use the reference product image and edit it into a clean studio product advertisement on a white background.",
            "negative_prompt": "blurry, distorted, low quality, extra text",
        },
        "inference_params": {
            "n": 1,
            "size": "1024*1024",
            "seed": 12345,
            "watermark": False,
            "prompt_extend": True,
        },
    },
    payload_schema={
        "type": "object",
        "required": ["images", "prompt"],
        "properties": {
            "images": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
            "prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
        },
        "additionalProperties": True,
    },
    inference_params_schema={
        "type": "object",
        "properties": {
            "watermark": {"type": "boolean"},
            "prompt_extend": {"type": "boolean"},
            "n": {"type": "integer", "minimum": 1},
            "seed": {"type": "integer"},
            "size": {"type": "string"},
        },
        "additionalProperties": True,
    },
)
def task_inference(model_spec: Dict[str, Any], payload: Dict[str, Any], inference_params: Dict[str, Any]) -> PluginExecutionResult:
    return execute_with_account_pool(
        model_spec=model_spec or {},
        provider="qwen-image-edit",
        call=lambda effective_model_spec: _run_business_logic(
            model_spec=effective_model_spec or {},
            payload=payload or {},
            inference_params=inference_params or {},
        ),
    )
