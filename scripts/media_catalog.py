#!/usr/bin/env python3
"""Load live public model/group membership and join it to verified capabilities."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CATALOG_FILE = Path(__file__).resolve().parents[1] / "references" / "model-catalog.json"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
CATALOG_TIMEOUT_SECONDS = 10
CATALOG_USER_AGENT = "CoderMediaGenerationSkill/1.0"


def load_capabilities() -> dict[str, Any]:
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"model capability catalog is missing or invalid: {error}") from error
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("model capability catalog has an unsupported version")
    return data


def _safe_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        return ""
    return value


def _fallback_catalog(capabilities: dict[str, Any], image_model_names: set[str], warning: str) -> dict[str, Any]:
    video_profiles = capabilities["video_models"]
    video_groups = []
    for group, names in capabilities["fallback_video_groups"].items():
        safe_group = _safe_name(group)
        if not safe_group or not isinstance(names, list):
            continue
        supported = [name for name in names if name in video_profiles]
        unsupported = [name for name in names if name not in video_profiles]
        video_groups.append({"group": safe_group, "models": supported, "unconfigured_models": unsupported})
    return {
        "source": "bundled_snapshot",
        "warning": warning,
        "image_models": sorted(image_model_names.intersection(capabilities.get("image_models", []))),
        "unconfigured_image_models": [],
        "video_groups": video_groups,
    }


def catalog_from_payload(payload: Any, image_model_names: set[str]) -> dict[str, Any]:
    capabilities = load_capabilities()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("public model catalog response has an invalid shape")

    video_profiles = capabilities["video_models"]
    video_groups: dict[str, dict[str, list[str]]] = {}
    public_images: set[str] = set()
    unconfigured_images: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model = _safe_name(item.get("model_name"))
        endpoints = item.get("supported_endpoint_types")
        if not model or not isinstance(endpoints, list):
            continue
        if "image-generation" in endpoints:
            if model in image_model_names:
                public_images.add(model)
            else:
                unconfigured_images.add(model)
        if "openai-video" not in endpoints:
            continue
        groups = item.get("enable_groups")
        if not isinstance(groups, list):
            continue
        target = video_profiles if model in video_profiles else None
        for raw_group in groups:
            group = _safe_name(raw_group)
            if not group:
                continue
            entry = video_groups.setdefault(group, {"models": [], "unconfigured_models": []})
            key = "models" if target is not None else "unconfigured_models"
            if model not in entry[key]:
                entry[key].append(model)

    if not video_groups:
        raise ValueError("public model catalog contains no video groups")
    return {
        "source": "live",
        "warning": "",
        "image_models": sorted(public_images),
        "unconfigured_image_models": sorted(unconfigured_images),
        "video_groups": [
            {"group": group, **values}
            for group, values in sorted(video_groups.items(), key=lambda item: item[0].casefold())
        ],
    }


def load_public_catalog(image_model_names: set[str] | None = None) -> dict[str, Any]:
    capabilities = load_capabilities()
    if image_model_names is None:
        image_model_names = set(capabilities.get("image_models", []))
    offline = os.environ.get("CODER_MEDIA_OFFLINE_CATALOG", "").strip().lower() in {"1", "true", "yes"}
    if offline:
        return _fallback_catalog(capabilities, image_model_names, "Offline mode is active; using the bundled group snapshot.")

    request = urllib.request.Request(
        capabilities["pricing_url"],
        headers={"Accept": "application/json", "User-Agent": CATALOG_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=CATALOG_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_CATALOG_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fallback_catalog(capabilities, image_model_names, "Public catalog unavailable; using the bundled group snapshot, which may be stale.")
    if len(body) > MAX_CATALOG_BYTES:
        return _fallback_catalog(capabilities, image_model_names, "Public catalog exceeded the response limit; using the bundled group snapshot, which may be stale.")
    try:
        payload = json.loads(body)
        return catalog_from_payload(payload, image_model_names)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _fallback_catalog(capabilities, image_model_names, "Public catalog response was invalid; using the bundled group snapshot, which may be stale.")


def video_groups_by_name(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        entry["group"]: entry
        for entry in catalog.get("video_groups", [])
        if isinstance(entry, dict) and isinstance(entry.get("group"), str)
    }
