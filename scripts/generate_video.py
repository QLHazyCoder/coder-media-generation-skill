#!/usr/bin/env python3
"""Create and retrieve one Grok video through Coder API's standard video endpoint."""

from __future__ import annotations

import argparse
import http.client
import datetime as dt
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from generate_image import (
    SkillError,
    api_base_url,
    api_error_message,
    configure_api_key,
    inferred_mime_type,
    local_config_path,
    read_api_key,
    read_key_input,
    read_video_group_api_key,
    security_reminder,
    unique_output_path,
    validate_edit_input,
    write_private_json,
)
from media_catalog import load_capabilities, load_public_catalog, video_groups_by_name


VIDEO_MODEL_CONFIG = load_capabilities()["video_models"]
VIDEO_MODELS = {model: profile["resolutions"] for model, profile in VIDEO_MODEL_CONFIG.items()}
ASPECT_RATIOS = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
VIDEO_STATE_VERSION = 2
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_UPLOAD_RESPONSE_BYTES = 256 * 1024
MAX_TIMEOUT = 1000
MAX_MEDIA_UPLOAD_BYTES = 512 * 1024 * 1024
MEDIA_UPLOAD_TIMEOUT = 300
TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}\Z")
IMAGE_UPLOAD_ENDPOINT = "https://coderapi.vip/image-upload/upload"
IMAGE_UPLOAD_URL_PATTERN = re.compile(r"^/image-upload/(m_[0-7][0-9A-HJKMNP-TV-Z]{25})$")
MEDIA_UPLOAD_ENDPOINT = "https://api.qlhazycoder.tech/media-upload/upload"
MEDIA_UPLOAD_HOST = "api.qlhazycoder.tech"
MEDIA_UPLOAD_URL_PATTERN = re.compile(r"^/media-upload/([va]_[0-7][0-9A-HJKMNP-TV-Z]{25})$")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group(required=True)
    operations.add_argument("--list-models", action="store_true")
    operations.add_argument("--begin", action="store_true")
    operations.add_argument("--save-local-key", action="store_true")
    operations.add_argument("--remove-group-key", action="store_true")
    operations.add_argument("--select-configuration", action="store_true")
    operations.add_argument("--submit", action="store_true")
    operations.add_argument("--attach-task-id", action="store_true")
    operations.add_argument("--poll", action="store_true")
    operations.add_argument("--publish", action="store_true")
    parser.add_argument("--state", help="video workflow file returned by --begin")
    parser.add_argument("--prompt", help="video prompt (optional when an image is provided)")
    parser.add_argument("--group", help="exact public model group used to select its dedicated API key")
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--image", help="local PNG, JPEG, or WebP reference image, at most 20 MiB")
    references.add_argument("--image-url", help="public HTTP(S) reference image URL for ZeeQi video models")
    parser.add_argument("--model", choices=sorted(VIDEO_MODELS))
    parser.add_argument("--seconds", type=int, help="video duration in seconds; range depends on the selected model")
    parser.add_argument("--resolution", help="480p, 720p, or 1080p where supported")
    parser.add_argument("--aspect-ratio", help="optional ratio; allowed values depend on the model")
    parser.add_argument("--reference-image-url", action="append", default=[], help="additional public reference image URL; repeat as needed")
    parser.add_argument("--reference-video-url", action="append", default=[], help="public reference video URL; repeat as needed")
    parser.add_argument("--reference-audio-url", action="append", default=[], help="public reference audio URL; repeat as needed")
    parser.add_argument("--reference-video", action="append", default=[], help="local MP4, WebM, or MOV reference video, at most 512 MiB; repeat as needed")
    parser.add_argument("--reference-audio", action="append", default=[], help="local MP3, WAV, or M4A reference audio, at most 512 MiB; repeat as needed")
    parser.add_argument("--first-frame-image-url", help="public first-frame image URL")
    parser.add_argument("--last-frame-image-url", help="public last-frame image URL")
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--generate-audio", dest="generate_audio", action="store_true")
    audio.add_argument("--no-generate-audio", dest="generate_audio", action="store_false")
    parser.set_defaults(generate_audio=None)
    watermark = parser.add_mutually_exclusive_group()
    watermark.add_argument("--watermark", dest="watermark", action="store_true")
    watermark.add_argument("--no-watermark", dest="watermark", action="store_false")
    parser.set_defaults(watermark=None)
    last_frame_output = parser.add_mutually_exclusive_group()
    last_frame_output.add_argument("--return-last-frame", dest="return_last_frame", action="store_true")
    last_frame_output.add_argument("--no-return-last-frame", dest="return_last_frame", action="store_false")
    parser.set_defaults(return_last_frame=None)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--task-id", help="existing public video task ID for manual reconciliation")
    parser.add_argument("--api-key-stdin", action="store_true", help="read a supplied API key from non-terminal stdin")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--file", help="local MP4 file to publish with --publish")
    parser.add_argument("--output", help="optional output filename (must end with .mp4)")
    parser.add_argument("--timeout", type=int, default=60, help="per-request timeout in seconds (1-1000)")
    parser.add_argument("--max-wait", type=int, default=120, help="maximum seconds to wait per --poll call")
    parser.add_argument("--poll-interval", type=int, default=5, help="seconds between task status queries")
    return parser.parse_args()


def validate_reference(path: str) -> Path:
    image = validate_edit_input(Path(path))
    if image.stat().st_size > MAX_REFERENCE_BYTES:
        raise SkillError("video reference image must be at most 20 MiB")
    return image


def validate_public_media_url(value: str, label: str = "reference URL") -> str:
    url = (value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in url)
    ):
        raise SkillError(f"{label} must be a public HTTP(S) URL")
    return url


def upload_multipart_payload(image_path: Path, field: str) -> tuple[bytes, str]:
    image = validate_reference(str(image_path))
    image_bytes = image.read_bytes()
    if len(image_bytes) > MAX_REFERENCE_BYTES:
        raise SkillError("video reference image must be at most 20 MiB")
    mime = inferred_mime_type(image_bytes[:12], "")
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime)
    if not extension:
        raise SkillError("video reference image must be PNG, JPEG, or WebP")
    boundary = f"----CoderAPIImageUpload{secrets.token_hex(16)}"
    body = bytearray()
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="reference.{extension}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode("ascii")
    )
    body.extend(image_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def extract_uploaded_image_url(value: Any, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
        return None
    if isinstance(value, list):
        for item in value:
            found = extract_uploaded_image_url(item, depth + 1)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    for key in ("url", "public_url", "image_url", "download_url"):
        found = extract_uploaded_image_url(value.get(key), depth + 1)
        if found:
            return found
    for key in ("data", "result", "image", "images", "links"):
        found = extract_uploaded_image_url(value.get(key), depth + 1)
        if found:
            return found
    return None


def validate_media_reference(path: str, kind: str) -> Path:
    media = Path(path).expanduser().resolve()
    if not media.is_file() or media.stat().st_size <= 0:
        raise SkillError(f"{kind} reference file does not exist or is empty")
    if media.stat().st_size > MAX_MEDIA_UPLOAD_BYTES:
        raise SkillError(f"{kind} reference file must be at most 512 MiB")
    with media.open("rb") as file:
        header = file.read(16)
    if len(header) < 8:
        raise SkillError(f"{kind} reference file is not a supported media container")
    if kind == "video":
        if header[4:8] != b"ftyp" and header[:4] != b"\x1a\x45\xdf\xa3":
            raise SkillError("video reference file must be MP4, WebM, or MOV")
    elif kind == "audio":
        if not (
            header[:3] == b"ID3"
            or (header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
            or (header[:4] == b"RIFF" and header[8:12] == b"WAVE")
            or header[4:8] == b"ftyp"
        ):
            raise SkillError("audio reference file must be MP3, WAV, or M4A")
    else:
        raise SkillError("unsupported media reference kind")
    return media


def media_upload_multipart_prefix(boundary: str, filename: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")


def upload_media_file(media_path: Path, expected_kind: str, api_key: str, timeout: int) -> tuple[str, str]:
    media = validate_media_reference(str(media_path), expected_kind)
    parsed = urllib.parse.urlsplit(MEDIA_UPLOAD_ENDPOINT)
    if parsed.scheme != "https" or parsed.netloc != MEDIA_UPLOAD_HOST or parsed.query or parsed.fragment:
        raise SkillError("media upload endpoint is invalid")
    size = media.stat().st_size
    boundary = f"----CoderAPIMediaUpload{secrets.token_hex(16)}"
    filename = "reference.mp4" if expected_kind == "video" else "reference.m4a"
    prefix = media_upload_multipart_prefix(boundary, filename)
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    connection = http.client.HTTPSConnection(MEDIA_UPLOAD_HOST, timeout=timeout)
    try:
        connection.putrequest("POST", parsed.path, skip_host=True)
        connection.putheader("Host", MEDIA_UPLOAD_HOST)
        connection.putheader("Accept", "application/json, text/plain")
        connection.putheader("User-Agent", "CoderMediaGenerationSkill/1.0")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(prefix) + size + len(suffix)))
        connection.putheader("Authorization", f"Bearer {api_key}")
        connection.endheaders()
        connection.send(prefix)
        with media.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        response_body = response.read(MAX_UPLOAD_RESPONSE_BYTES + 1)
        status = response.status
    except (OSError, TimeoutError) as error:
        raise SkillError(f"media upload request failed: {str(error).replace(api_key, '[redacted]')}") from error
    finally:
        connection.close()
    if len(response_body) > MAX_UPLOAD_RESPONSE_BYTES:
        raise SkillError("media upload response exceeds the 256 KiB safety limit")
    if status < 200 or status >= 300:
        details = api_error_message(response_body)
        raise SkillError(f"media upload returned HTTP {status}: {details.replace(api_key, '[redacted]')}")
    try:
        decoded: Any = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillError("media upload response is not valid JSON") from error
    media_url = extract_uploaded_image_url(decoded)
    if not media_url:
        raise SkillError("media upload response did not contain a public media URL")
    media_parsed = urllib.parse.urlsplit(media_url)
    match = MEDIA_UPLOAD_URL_PATTERN.fullmatch(media_parsed.path)
    if (
        media_parsed.scheme != "https"
        or media_parsed.netloc != MEDIA_UPLOAD_HOST
        or not match
        or media_parsed.query
        or media_parsed.fragment
    ):
        raise SkillError("media upload response must contain an api.qlhazycoder.tech media URL with a valid media ID")
    media_id = match.group(1)
    expected_prefix = "v_" if expected_kind == "video" else "a_"
    if not media_id.startswith(expected_prefix):
        raise SkillError("media upload response returned the wrong media kind")
    return f"https://{MEDIA_UPLOAD_HOST}/media-upload/{media_id}", media_id


def media_fingerprint(media: Path) -> tuple[int, int]:
    information = media.stat()
    return information.st_size, information.st_mtime_ns


def upload_state_reference_media(path: Path, state: dict[str, Any], api_key: str, timeout: int) -> None:
    profile = VIDEO_MODEL_CONFIG.get(state.get("model"))
    if not profile:
        raise SkillError("video workflow configuration is invalid")
    local_videos = [Path(value) for value in state.get("reference_video_paths", [])]
    local_audios = [Path(value) for value in state.get("reference_audio_paths", [])]
    if profile["adapter"] == "grok" and (local_videos or local_audios):
        raise SkillError(f"{state['model']} does not accept local reference video or audio uploads")
    limits = profile.get("reference_limits")
    if isinstance(limits, dict):
        video_count = len(state.get("reference_video_urls", [])) + len(local_videos)
        audio_count = len(state.get("reference_audio_urls", [])) + len(local_audios)
        if video_count > limits.get("videos", 0) or audio_count > limits.get("audios", 0):
            raise SkillError(f"{state['model']} does not accept this many local reference media files")
    uploads = state.setdefault("uploaded_reference_media", {})
    for kind, paths, url_field in (
        ("video", local_videos, "reference_video_urls"),
        ("audio", local_audios, "reference_audio_urls"),
    ):
        if not paths:
            continue
        uploaded_urls: list[str] = []
        for media in paths:
            fingerprint = media_fingerprint(media)
            cached = uploads.get(str(media))
            if (
                isinstance(cached, dict)
                and cached.get("kind") == kind
                and (cached.get("size"), cached.get("mtime_ns")) == fingerprint
                and isinstance(cached.get("url"), str)
            ):
                url = cached["url"]
            else:
                url, _ = upload_media_file(media, kind, api_key, timeout)
                uploads[str(media)] = {
                    "kind": kind,
                    "url": url,
                    "size": fingerprint[0],
                    "mtime_ns": fingerprint[1],
                }
                write_private_json(path, state)
            uploaded_urls.append(url)
        state[url_field] = list(state.get(url_field, [])) + uploaded_urls
    write_private_json(path, state)


def upload_reference_image(image_path: Path, api_key: str, timeout: int) -> str:
    body, content_type = upload_multipart_payload(image_path, "file")
    headers = {
        "Accept": "application/json, text/plain",
        "User-Agent": "CoderMediaGenerationSkill/1.0",
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(IMAGE_UPLOAD_ENDPOINT, data=body, method="POST", headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            response_body = response.read(MAX_UPLOAD_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        details = api_error_message(error.read(8192))
        details = details.replace(api_key, "[redacted]")
        raise SkillError(f"image upload returned HTTP {error.code}: {details}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        message = str(error).replace(api_key, "[redacted]")
        raise SkillError(f"image upload request failed: {message}") from error
    if len(response_body) > MAX_UPLOAD_RESPONSE_BYTES:
        raise SkillError("image upload response exceeds the 256 KiB safety limit")
    try:
        decoded: Any = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = response_body.decode("utf-8", errors="replace").strip()
    image_url = extract_uploaded_image_url(decoded)
    if not image_url:
        raise SkillError("image upload response did not contain a public image URL")
    parsed = urllib.parse.urlsplit(image_url)
    match = IMAGE_UPLOAD_URL_PATTERN.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "coderapi.vip"
        or not match
        or parsed.query
        or parsed.fragment
    ):
        raise SkillError("image upload response must contain a coderapi.vip image URL with a valid media ID")
    return f"https://coderapi.vip/image-upload/{match.group(1)}"


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"video workflow state is missing or invalid: {error}") from error
    if not isinstance(state, dict) or state.get("version") not in {1, VIDEO_STATE_VERSION} or state.get("media") != "video":
        raise SkillError("video workflow state is invalid or from an unsupported version")
    if not isinstance(state.get("status"), str) or not isinstance(state.get("prompt"), str):
        raise SkillError("video workflow state is invalid")
    if state.get("version") == 1:
        if state["status"] not in {"submitting", "submission_uncertain", "submitted", "in_progress", "completed", "failed", "canceled"}:
            raise SkillError("old video workflow has no group selection; start a new workflow")
        state["legacy_image_key"] = True
    return state


def begin(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    if not (args.prompt or "").strip() and not args.image and not args.image_url and not args.reference_image_url and not args.reference_video_url and not args.reference_audio_url and not args.reference_video and not args.reference_audio:
        raise SkillError("a prompt or at least one reference is required for a video")
    image = validate_reference(args.image) if args.image else None
    image_url = validate_public_media_url(args.image_url, "--image-url") if args.image_url else None
    reference_images = [validate_public_media_url(url, "--reference-image-url") for url in args.reference_image_url]
    reference_videos = [validate_public_media_url(url, "--reference-video-url") for url in args.reference_video_url]
    reference_audios = [validate_public_media_url(url, "--reference-audio-url") for url in args.reference_audio_url]
    reference_video_paths = [validate_media_reference(path, "video") for path in args.reference_video]
    reference_audio_paths = [validate_media_reference(path, "audio") for path in args.reference_audio]
    first_frame = validate_public_media_url(args.first_frame_image_url, "--first-frame-image-url") if args.first_frame_image_url else None
    last_frame = validate_public_media_url(args.last_frame_image_url, "--last-frame-image-url") if args.last_frame_image_url else None
    if last_frame and not first_frame:
        raise SkillError("--last-frame-image-url requires --first-frame-image-url")
    catalog = load_public_catalog()
    directory = Path(tempfile.mkdtemp(prefix="coder-api-video-"))
    os.chmod(directory, 0o700)
    path = directory / "workflow.json"
    state: dict[str, Any] = {
        "version": VIDEO_STATE_VERSION,
        "media": "video",
        "prompt": (args.prompt or "").strip(),
        "status": "model_selection",
        "local_key_saved": False,
        "catalog": catalog,
    }
    if image:
        state["image_path"] = str(image)
    if image_url:
        state["image_url"] = image_url
    if reference_images:
        state["reference_image_urls"] = reference_images
    if reference_videos:
        state["reference_video_urls"] = reference_videos
    if reference_audios:
        state["reference_audio_urls"] = reference_audios
    if reference_video_paths:
        state["reference_video_paths"] = [str(value) for value in reference_video_paths]
    if reference_audio_paths:
        state["reference_audio_paths"] = [str(value) for value in reference_audio_paths]
    if first_frame:
        state["first_frame_image_url"] = first_frame
    if last_frame:
        state["last_frame_image_url"] = last_frame
    write_private_json(path, state)
    return path, state


def state_result(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"state": str(path), "status": state["status"], "media": "video"}
    status = state["status"]
    if status == "key_storage_decision":
        result.update({
            "action": "configure_video_group_key",
            "group": state.get("group"),
            "model": state.get("model"),
            "security_reminder": security_reminder(),
        })
    elif status == "model_selection":
        catalog = state.get("catalog", {})
        result.update({
            "action": "ask_user_to_choose_group_model_and_video_settings",
            "catalog_source": catalog.get("source", "bundled_snapshot"),
            "catalog_warning": catalog.get("warning", ""),
            "groups": catalog.get("video_groups", []),
            "model_profiles": VIDEO_MODEL_CONFIG,
        })
    elif status == "ready":
        result.update({"action": "ready_to_submit", "group": state["group"], "model": state["model"], "settings": state["settings"]})
    elif status in {"submitting", "submission_uncertain"}:
        result.update({"action": "manual_reconcile_submission", "warning": "The upstream may have accepted this paid request. Do not submit it again; find its public task ID and use --attach-task-id."})
    elif status in {"submitted", "in_progress"}:
        result.update({"action": "poll_existing_task", "task_id": state["task_id"]})
    elif status == "completed":
        result.update({"action": "download_existing_task", "task_id": state["task_id"]})
    elif status in {"failed", "canceled"}:
        result.update({"action": "inspect_failed_task", "task_id": state["task_id"], "error": state.get("error", "")})
    else:
        raise SkillError("video workflow has an unsupported status")
    if state.get("local_key_saved"):
        result["security_reminder"] = security_reminder()
    return result


def select_configuration(state: dict[str, Any], args: argparse.Namespace) -> None:
    if state["status"] != "model_selection":
        raise SkillError("video model selection is not the next workflow step")
    if not args.group:
        raise SkillError("--group is required; choose the exact public group before selecting a model")
    catalog = state.get("catalog")
    if not isinstance(catalog, dict):
        raise SkillError("video workflow is missing its public group catalog")
    group = video_groups_by_name(catalog).get(args.group)
    if group is None:
        raise SkillError("unknown video group; reload --list-models and use an exact group name")
    if args.model not in group.get("models", []):
        raise SkillError("choose an explicit video model from --list-models")
    profile = VIDEO_MODEL_CONFIG[args.model]
    duration = profile["duration"]
    if args.seconds is None or not duration["min"] <= args.seconds <= duration["max"]:
        raise SkillError(f"--seconds must be an integer between {duration['min']} and {duration['max']} for {args.model}")
    if args.resolution not in VIDEO_MODELS[args.model]:
        raise SkillError(f"unsupported resolution for {args.model}; choose {', '.join(VIDEO_MODELS[args.model])}")
    ratios = profile.get("aspect_ratios")
    if args.aspect_ratio:
        if not isinstance(args.aspect_ratio, str) or not args.aspect_ratio.strip() or len(args.aspect_ratio) > 32:
            raise SkillError("--aspect-ratio must be a short non-empty string")
        if isinstance(ratios, list) and args.aspect_ratio not in ratios:
            raise SkillError(f"unsupported aspect ratio for {args.model}; choose {', '.join(ratios)}")
    main_image = bool(state.get("image_path") or state.get("image_url"))
    reference_images = state.get("reference_image_urls", [])
    reference_videos = list(state.get("reference_video_urls", [])) + list(state.get("reference_video_paths", []))
    reference_audios = list(state.get("reference_audio_urls", [])) + list(state.get("reference_audio_paths", []))
    first_frame = state.get("first_frame_image_url")
    last_frame = state.get("last_frame_image_url")
    if (first_frame or last_frame) and not profile.get("supports_first_last_frames"):
        raise SkillError(f"{args.model} does not support first/last-frame input")
    if first_frame and (main_image or reference_images or reference_videos or reference_audios):
        raise SkillError("first/last-frame input cannot be combined with other reference media")
    if profile["adapter"] == "grok" and (reference_images or reference_videos or reference_audios or first_frame or last_frame):
        raise SkillError(f"{args.model} accepts one image input, not reference arrays, video/audio, or first/last frames")
    limits = profile.get("reference_limits")
    if isinstance(limits, dict):
        counts = {
            "images": int(main_image) + len(reference_images) + int(bool(first_frame)) + int(bool(last_frame)),
            "videos": len(reference_videos),
            "audios": len(reference_audios),
        }
        for kind, count in counts.items():
            maximum = limits.get(kind)
            if isinstance(maximum, int) and count > maximum:
                raise SkillError(f"{args.model} accepts at most {maximum} reference {kind}")
    if reference_videos and isinstance(limits, dict) and limits.get("videos", 0) == 0:
        raise SkillError(f"{args.model} does not accept reference videos")
    if reference_audios and isinstance(limits, dict) and limits.get("audios", 0) == 0:
        raise SkillError(f"{args.model} does not accept reference audio")
    has_image_input = main_image or bool(reference_images) or bool(first_frame)
    if profile.get("prompt_required_without_image") and not has_image_input and not state.get("prompt", "").strip():
        raise SkillError(f"{args.model} requires a non-empty prompt")
    if profile.get("prompt_required_with_image") and has_image_input and not state.get("prompt", "").strip():
        raise SkillError(f"{args.model} requires a non-empty prompt, including image-to-video")
    settings: dict[str, Any] = {"seconds": args.seconds, "resolution": args.resolution}
    if args.aspect_ratio:
        settings["aspect_ratio"] = args.aspect_ratio
    if args.generate_audio is not None:
        if "generate_audio" not in profile.get("optional_parameters", []):
            raise SkillError(f"{args.model} does not declare generate_audio as a supported option")
        settings["generate_audio"] = args.generate_audio
    if args.watermark is not None:
        if "watermark" not in profile.get("optional_parameters", []):
            raise SkillError(f"{args.model} does not declare watermark as a supported option")
        settings["watermark"] = args.watermark
    if args.return_last_frame is not None:
        if "return_last_frame" not in profile.get("optional_parameters", []):
            raise SkillError(f"{args.model} does not declare return_last_frame as a supported option")
        settings["return_last_frame"] = args.return_last_frame
    if args.seed is not None:
        if "seed" not in profile.get("optional_parameters", []):
            raise SkillError(f"{args.model} does not declare seed as a supported option")
        settings["seed"] = args.seed
    state.update({"group": args.group, "model": args.model, "settings": settings})
    try:
        read_video_group_api_key(args.group)
    except SkillError as error:
        if "no API key is configured" not in str(error):
            raise
        state["status"] = "key_storage_decision"
    else:
        state["status"] = "ready"


def request_payload(state: dict[str, Any], resolved_image_url: str | None = None) -> dict[str, Any]:
    model, settings = state.get("model"), state.get("settings")
    if model not in VIDEO_MODELS or not isinstance(settings, dict) or model not in VIDEO_MODEL_CONFIG:
        raise SkillError("video workflow configuration is invalid")
    seconds, resolution = settings.get("seconds"), settings.get("resolution")
    profile = VIDEO_MODEL_CONFIG[model]
    duration = profile["duration"]
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not duration["min"] <= seconds <= duration["max"] or resolution not in VIDEO_MODELS[model]:
        raise SkillError("video workflow configuration is invalid")
    adapter = profile["adapter"]
    if adapter != "grok" and state.get("image_path") and not resolved_image_url:
        raise SkillError(f"{model} requires uploading the local reference image before submission")
    payload: dict[str, Any] = {"model": model, "resolution": resolution}
    payload["seconds" if adapter == "grok" else "duration"] = seconds
    if adapter != "grok" and not state.get("prompt", "").strip():
        raise SkillError(f"{model} requires a non-empty prompt")
    if state.get("prompt", "").strip():
        payload["prompt"] = state["prompt"]
    image_url = state.get("image_url") or resolved_image_url
    if image_url:
        payload["image" if adapter == "grok" else "input_reference"] = validate_public_media_url(image_url, "image reference")
    for state_field, payload_field in (
        ("reference_image_urls", "reference_image_urls"),
        ("reference_video_urls", "reference_video_urls"),
        ("reference_audio_urls", "reference_audio_urls"),
    ):
        values = state.get(state_field, [])
        if values:
            payload[payload_field] = [validate_public_media_url(url, payload_field) for url in values]
    for field in ("first_frame_image_url", "last_frame_image_url"):
        if state.get(field):
            payload[field] = validate_public_media_url(state[field], field)
    ratio = settings.get("aspect_ratio")
    if ratio:
        ratios = profile.get("aspect_ratios")
        if isinstance(ratios, list) and ratio not in ratios:
            raise SkillError("video workflow aspect ratio is invalid")
        payload["aspect_ratio"] = ratio
    for field in ("generate_audio", "watermark", "return_last_frame", "seed"):
        if field in settings:
            payload[field] = settings[field]
    return payload


def multipart_payload(payload: dict[str, Any], image_path: Path) -> tuple[bytes, str]:
    image = validate_reference(str(image_path))
    image_bytes = image.read_bytes()
    if len(image_bytes) > MAX_REFERENCE_BYTES:
        raise SkillError("video reference image must be at most 20 MiB")
    boundary = f"----CoderAPIVideo{secrets.token_hex(16)}"
    body = bytearray()
    for key, value in payload.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode("ascii"))
        body.extend((str(value).lower() if isinstance(value, bool) else str(value)).encode("utf-8"))
        body.extend(b"\r\n")
    mime = inferred_mime_type(image_bytes[:12], "")
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference.{extension}"\r\nContent-Type: {mime}\r\n\r\n'.encode("ascii"))
    body.extend(image_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def http_json(url: str, api_key: str, timeout: int, payload: bytes | None = None, content_type: str = "application/json") -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST" if payload is not None else "GET",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": content_type} if payload is not None else {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as error:
        message = api_error_message(error.read(8192)).replace(api_key, "[redacted]")
        raise SkillError(f"video API returned HTTP {error.code}: {message}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SkillError(f"video API request failed: {str(error).replace(api_key, '[redacted]')}") from error
    if len(body) > MAX_JSON_BYTES:
        raise SkillError("video task response exceeds the 1 MiB safety limit")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillError("video task response is not valid JSON") from error
    if not isinstance(data, dict):
        raise SkillError("video task response must be a JSON object")
    return data


def valid_task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_PATTERN.fullmatch(value):
        raise SkillError("video response did not contain a valid public task ID")
    return value


def state_url(state: dict[str, Any]) -> str:
    base_url = state.get("base_url")
    if not isinstance(base_url, str) or base_url != api_base_url():
        raise SkillError("CODER_API_BASE_URL differs from the URL used for this video task")
    return base_url


def api_key_for_state(state: dict[str, Any]) -> str:
    if state.get("legacy_image_key"):
        return read_api_key()
    group = state.get("group")
    if not isinstance(group, str) or not group:
        raise SkillError("video workflow has no selected group; start a new workflow")
    return read_video_group_api_key(group)


def submit(path: Path, state: dict[str, Any], timeout: int) -> dict[str, Any]:
    if state["status"] != "ready":
        raise SkillError(f"video workflow is not ready to submit; current status is {state['status']}")
    profile = VIDEO_MODEL_CONFIG.get(state.get("model"))
    base_url = api_base_url()
    api_key = api_key_for_state(state)
    upload_state_reference_media(path, state, api_key, max(timeout, 300))
    resolved_image_url = state.get("uploaded_image_url")
    if profile and profile["adapter"] != "grok" and state.get("image_path"):
        if resolved_image_url:
            resolved_image_url = validate_public_media_url(resolved_image_url, "uploaded image URL")
        else:
            resolved_image_url = upload_reference_image(Path(state["image_path"]), api_key, timeout)
            state["uploaded_image_url"] = resolved_image_url
            write_private_json(path, state)
    payload = request_payload(state, resolved_image_url)
    if state.get("image_path") and profile and profile["adapter"] == "grok":
        body, content_type = multipart_payload(payload, Path(state["image_path"]))
    else:
        body, content_type = json.dumps(payload).encode("utf-8"), "application/json"
    state.update({"status": "submitting", "base_url": base_url})
    write_private_json(path, state)
    try:
        response = http_json(f"{base_url}/videos", api_key, timeout, body, content_type)
    except SkillError as error:
        # Only a definite client-side HTTP rejection permits a fresh submission.
        cause = error.__cause__
        if isinstance(cause, urllib.error.HTTPError) and cause.code in {400, 401, 403, 404, 422}:
            state["status"] = "ready"
        else:
            state["status"] = "submission_uncertain"
        write_private_json(path, state)
        raise
    try:
        state["task_id"] = valid_task_id(response.get("id"))
    except SkillError:
        state["status"] = "submission_uncertain"
        write_private_json(path, state)
        raise
    state["status"] = "submitted"
    write_private_json(path, state)
    return state_result(path, state)


def attach_task(path: Path, state: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    if state["status"] not in {"submitting", "submission_uncertain"}:
        raise SkillError("manual task ID reconciliation is only available after an uncertain submission")
    state["task_id"] = valid_task_id(task_id)
    state["status"] = "submitted"
    write_private_json(path, state)
    return state_result(path, state)


def download_video(url: str, api_key: str, timeout: int, output_dir: Path, output: str | None) -> Path:
    if output and Path(output).suffix.lower() not in {"", ".mp4"}:
        raise SkillError("--output must be an .mp4 filename")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "video/mp4"})
    output_path: Path | None = None
    created = False
    complete = False
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            size = response.headers.get("Content-Length")
            if size and size.isdecimal() and int(size) > MAX_VIDEO_BYTES:
                raise SkillError("video exceeds the 512 MiB download limit")
            first = response.read(64 * 1024)
            if len(first) < 12 or first[4:8] != b"ftyp":
                raise SkillError("video content was not an MP4 file")
            name = output or f"generated-video-{dt.datetime.now(tz=dt.UTC):%Y%m%d-%H%M%S}"
            output_path = unique_output_path(output_dir, name, ".mp4")
            with output_path.open("xb") as file:
                created = True
                file.write(first)
                total = len(first)
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise SkillError("video exceeds the 512 MiB download limit")
                    file.write(chunk)
            complete = True
    except urllib.error.HTTPError as error:
        message = api_error_message(error.read(8192)).replace(api_key, "[redacted]")
        raise SkillError(f"video download returned HTTP {error.code}: {message}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SkillError(f"video download failed: {str(error).replace(api_key, '[redacted]')}") from error
    finally:
        if created and not complete and output_path is not None:
            output_path.unlink(missing_ok=True)
    if output_path is None:
        raise SkillError("video download returned no content")
    return output_path.resolve()


def remove_state(path: Path) -> None:
    path.unlink(missing_ok=True)
    if path.parent.parent == Path(tempfile.gettempdir()).resolve() and path.parent.name.startswith("coder-api-video-"):
        path.parent.rmdir()


def poll(path: Path, state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if state["status"] not in {"submitted", "in_progress", "completed"}:
        raise SkillError(f"video task cannot be polled in status {state['status']}")
    task_id = valid_task_id(state.get("task_id"))
    base_url = state_url(state)
    api_key = api_key_for_state(state)
    deadline = time.monotonic() + args.max_wait
    while state["status"] != "completed":
        response = http_json(f"{base_url}/videos/{urllib.parse.quote(task_id, safe='')}", api_key, args.timeout)
        if response.get("id") != task_id:
            raise SkillError("video status response returned a different task ID")
        status = response.get("status")
        if status == "completed":
            state["status"] = "completed"
        elif status in {"failed", "canceled"}:
            state["status"] = status
            error = response.get("error")
            message = error.get("message", "") if isinstance(error, dict) else ""
            state["error"] = message[:500].replace(api_key, "[redacted]") if isinstance(message, str) else ""
        elif status in {"queued", "in_progress"}:
            state["status"] = "in_progress"
        else:
            raise SkillError("video task returned an unsupported status")
        write_private_json(path, state)
        if state["status"] in {"failed", "canceled"}:
            return state_result(path, state)
        if state["status"] == "completed" or time.monotonic() >= deadline:
            break
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
    if state["status"] != "completed":
        return state_result(path, state)
    output_path = download_video(f"{base_url}/videos/{urllib.parse.quote(task_id, safe='')}/content", api_key, args.timeout, Path(args.output_dir), args.output)
    result: dict[str, Any] = {"file": str(output_path), "group": state.get("group"), "model": state["model"], "mime_type": "video/mp4", "media": "video", "task_id": task_id}
    if state.get("local_key_saved"):
        result["security_reminder"] = security_reminder()
    remove_state(path)
    return result


def main() -> int:
    args = parse_args()
    try:
        if not 1 <= args.timeout <= MAX_TIMEOUT or args.max_wait < 0 or args.poll_interval < 1:
            raise SkillError("--timeout must be 1-1000; --max-wait >= 0; --poll-interval >= 1")
        begin_args = (args.prompt, args.image, args.image_url, args.reference_image_url, args.reference_video_url, args.reference_audio_url, args.reference_video, args.reference_audio, args.first_frame_image_url, args.last_frame_image_url)
        if not args.begin and any(value for value in begin_args):
            raise SkillError("prompt and reference inputs are accepted only with --begin")
        selection_args = (args.model, args.seconds, args.resolution, args.aspect_ratio, args.generate_audio, args.watermark, args.return_last_frame, args.seed)
        if not args.select_configuration and any(value is not None for value in selection_args):
            raise SkillError("video model and settings are accepted only with --select-configuration")
        if args.group and not (args.select_configuration or args.remove_group_key or args.publish):
            raise SkillError("--group is accepted only with --select-configuration, --remove-group-key, or --publish")
        if args.file and not args.publish:
            raise SkillError("--file is accepted only with --publish")
        if not args.attach_task_id and args.task_id is not None:
            raise SkillError("--task-id is accepted only with --attach-task-id")
        if args.api_key_stdin and not args.save_local_key:
            raise SkillError("--api-key-stdin is accepted only with --save-local-key")
        if args.publish:
            if not args.file or not args.group:
                raise SkillError("--publish requires both --file and --group")
            media = validate_media_reference(args.file, "video")
            api_key = read_video_group_api_key(args.group)
            public_url, media_id = upload_media_file(media, "video", api_key, MEDIA_UPLOAD_TIMEOUT)
            result = {
                "media": "video",
                "file": str(media),
                "group": args.group,
                "public_url": public_url,
                "media_id": media_id,
            }
        elif args.list_models:
            catalog = load_public_catalog()
            result = {
                "media": "video",
                "catalog_source": catalog["source"],
                "catalog_warning": catalog["warning"],
                "groups": catalog["video_groups"],
                "model_profiles": VIDEO_MODEL_CONFIG,
            }
        elif args.remove_group_key:
            if not args.group:
                raise SkillError("--remove-group-key requires --group")
            from generate_image import remove_local_api_key
            remove_local_api_key(local_config_path(), scope="video", group=args.group)
            result = {"group_key_removed": args.group, "config": str(local_config_path())}
        elif args.begin:
            if args.state:
                raise SkillError("--begin cannot use --state")
            path, state = begin(args)
            result = state_result(path, state)
        else:
            if not args.state:
                raise SkillError("--state is required")
            path = Path(args.state).expanduser().resolve()
            state = read_state(path)
            if args.save_local_key:
                if state["status"] != "key_storage_decision" or not state.get("group"):
                    raise SkillError("--save-local-key requires a selected group in key_storage_decision state")
                key_prompt = f"API key for video group {state['group']} (input hidden): "
                api_key = read_key_input(args.api_key_stdin, key_prompt)
                configure_api_key(local_config_path(), api_key, emit_reminder=False, scope="video", group=state["group"])
                state.update({"status": "ready", "local_key_saved": True})
                write_private_json(path, state)
                result = state_result(path, state)
            elif args.select_configuration:
                select_configuration(state, args)
                write_private_json(path, state)
                result = state_result(path, state)
            elif args.submit:
                result = submit(path, state, args.timeout)
            elif args.attach_task_id:
                result = attach_task(path, state, args.task_id)
            else:
                if args.max_wait > 3600:
                    raise SkillError("--max-wait must not exceed 3600 seconds")
                result = poll(path, state, args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SkillError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
