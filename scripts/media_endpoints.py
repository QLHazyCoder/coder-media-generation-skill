"""Validated upload and public media endpoints for the media skill."""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


DEFAULT_MEDIA_UPLOAD_ORIGIN = "https://wcf.coderapi.vip"
DEFAULT_IMAGE_PUBLIC_BASE_URL = "https://coderapi.vip/image-upload"
DEFAULT_MEDIA_PUBLIC_BASE_URL = "https://coderapi.vip/media-upload"


@dataclass(frozen=True)
class MediaEndpoints:
    upload_origin: str
    image_public_base_url: str
    media_public_base_url: str

    @property
    def image_upload_endpoint(self) -> str:
        return self.upload_origin + "/image-upload/upload"

    @property
    def media_upload_endpoint(self) -> str:
        return self.upload_origin + "/media-upload/upload"


def _configured(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip().rstrip("/")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _validate_https_url(name: str, value: str, expected_path: str | None = None, origin_only: bool = False) -> str:
    if any(char.isspace() for char in value) or "?" in value or "#" in value:
        raise ValueError(f"{name} must be a valid HTTPS URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as error:
        raise ValueError(f"{name} must be a valid HTTPS URL") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS URL without user info, query, or fragment")
    if origin_only:
        if parsed.path not in {"", "/"}:
            raise ValueError(f"{name} must be an origin without a path")
    elif parsed.path != expected_path:
        raise ValueError(f"{name} must use path {expected_path}")
    return value


def load_media_endpoints() -> MediaEndpoints:
    upload_origin = _validate_https_url(
        "CODER_MEDIA_UPLOAD_ORIGIN",
        _configured("CODER_MEDIA_UPLOAD_ORIGIN", DEFAULT_MEDIA_UPLOAD_ORIGIN),
        origin_only=True,
    )
    image_public_base_url = _validate_https_url(
        "CODER_IMAGE_PUBLIC_BASE_URL",
        _configured("CODER_IMAGE_PUBLIC_BASE_URL", DEFAULT_IMAGE_PUBLIC_BASE_URL),
        expected_path="/image-upload",
    )
    media_public_base_url = _validate_https_url(
        "CODER_MEDIA_PUBLIC_BASE_URL",
        _configured("CODER_MEDIA_PUBLIC_BASE_URL", DEFAULT_MEDIA_PUBLIC_BASE_URL),
        expected_path="/media-upload",
    )
    return MediaEndpoints(
        upload_origin=upload_origin,
        image_public_base_url=image_public_base_url,
        media_public_base_url=media_public_base_url,
    )
