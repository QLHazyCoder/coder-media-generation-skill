#!/usr/bin/env python3
"""Generate text-to-speech or music through the Coder API."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import mimetypes
import re
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
    local_config_path,
    read_audio_group_api_key,
    read_key_input,
    security_reminder,
    unique_output_path,
    write_private_json,
)
from media_catalog import audio_groups_by_name, load_capabilities, load_public_catalog


AUDIO_MODEL_CONFIG = load_capabilities()["audio_models"]
AUDIO_STATE_VERSION = 1
MAX_TIMEOUT = 1000
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 512 * 1024 * 1024
TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}\Z")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
DEFAULT_AUDIO_GROUP = "\u97f3\u9891\u6a21\u578b"
FORMAT_EXTENSIONS = {
    "mp3": ".mp3",
    "wav": ".wav",
    "opus": ".opus",
    "aac": ".aac",
    "flac": ".flac",
    "pcm": ".pcm",
    "m4a": ".m4a",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class AudioHTTPError(SkillError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"audio API returned HTTP {status}: {message}")
        self.status = status


def parse_args() -> Any:
    import argparse

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
    parser.add_argument("--state", help="audio workflow file returned by --begin")
    parser.add_argument("--prompt", help="text to speak or music description")
    parser.add_argument("--group", help="exact public audio group used for the API key")
    parser.add_argument("--model", choices=sorted(AUDIO_MODEL_CONFIG))
    parser.add_argument("--voice", help="TTS voice name")
    parser.add_argument("--format", dest="response_format", help="audio output format")
    parser.add_argument("--speed", type=float, help="TTS speed, normally between 0.25 and 4")
    parser.add_argument("--instructions", help="optional TTS style instructions")
    parser.add_argument("--mv", help="optional music version parameter")
    parser.add_argument("--title", help="optional music title")
    parser.add_argument("--tags", help="optional music tags")
    instrumental = parser.add_mutually_exclusive_group()
    instrumental.add_argument("--make-instrumental", dest="make_instrumental", action="store_true")
    instrumental.add_argument("--no-make-instrumental", dest="make_instrumental", action="store_false")
    parser.set_defaults(make_instrumental=None)
    parser.add_argument("--task-id", help="existing task ID for manual reconciliation")
    parser.add_argument("--api-key-stdin", action="store_true", help="read a supplied API key from non-terminal stdin")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--output", help="optional output filename")
    parser.add_argument("--timeout", type=int, default=60, help="per-request timeout in seconds (1-1000)")
    parser.add_argument("--max-wait", type=int, default=300, help="maximum seconds to wait per --poll call")
    parser.add_argument("--poll-interval", type=int, default=5, help="seconds between task status queries")
    return parser.parse_args()


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SkillError(f"audio workflow state is missing or invalid: {error}") from error
    if not isinstance(state, dict) or state.get("version") != AUDIO_STATE_VERSION or state.get("media") != "audio":
        raise SkillError("audio workflow state is invalid or from an unsupported version")
    if not isinstance(state.get("status"), str) or not isinstance(state.get("prompt"), str):
        raise SkillError("audio workflow state is invalid")
    return state


def begin(args: Any) -> tuple[Path, dict[str, Any]]:
    prompt = (args.prompt or "").strip()
    if not prompt:
        raise SkillError("--prompt is required with --begin")
    catalog = load_public_catalog(set(AUDIO_MODEL_CONFIG))
    directory = Path(tempfile.mkdtemp(prefix="coder-api-audio-"))
    directory.chmod(0o700)
    path = directory / "workflow.json"
    state = {
        "version": AUDIO_STATE_VERSION,
        "media": "audio",
        "prompt": prompt,
        "status": "model_selection",
        "local_key_saved": False,
        "catalog": catalog,
    }
    write_private_json(path, state)
    return path, state


def state_result(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"state": str(path), "status": state["status"], "media": "audio"}
    status = state["status"]
    if status == "model_selection":
        catalog = state.get("catalog", {})
        result.update({
            "action": "ask_user_to_choose_audio_group_model_and_settings",
            "catalog_source": catalog.get("source", "bundled_snapshot"),
            "catalog_warning": catalog.get("warning", ""),
            "groups": catalog.get("audio_groups", []),
            "model_profiles": AUDIO_MODEL_CONFIG,
        })
    elif status == "key_storage_decision":
        result.update({
            "action": "configure_audio_group_key",
            "group": state.get("group"),
            "model": state.get("model"),
            "security_reminder": security_reminder(),
        })
    elif status == "ready":
        result.update({"action": "ready_to_submit", "group": state["group"], "model": state["model"], "settings": state["settings"]})
    elif status in {"submitting", "submission_uncertain"}:
        result.update({
            "action": "manual_reconcile_submission",
            "warning": "The audio request may have been accepted. Do not submit it again automatically.",
        })
    elif status in {"submitted", "in_progress"}:
        result.update({"action": "poll_existing_task", "task_id": state.get("task_id")})
    elif status in {"failed", "canceled"}:
        result.update({"action": "inspect_failed_task", "task_id": state.get("task_id"), "error": state.get("error", "")})
    else:
        raise SkillError("audio workflow has an unsupported status")
    if state.get("local_key_saved"):
        result["security_reminder"] = security_reminder()
    return result


def select_configuration(state: dict[str, Any], args: Any) -> None:
    if state["status"] != "model_selection":
        raise SkillError("audio model selection is not the next workflow step")
    if not args.group:
        raise SkillError("--group is required; choose the exact public audio group first")
    groups = audio_groups_by_name(state.get("catalog", {}))
    group = groups.get(args.group)
    if group is None:
        raise SkillError("unknown audio group; reload --list-models and use an exact group name")
    if args.model not in group.get("models", []):
        raise SkillError("choose an audio model listed in the selected public group")
    profile = AUDIO_MODEL_CONFIG.get(args.model)
    if not isinstance(profile, dict):
        raise SkillError("audio model has no verified capability profile")
    settings: dict[str, Any] = {}
    if profile.get("kind") == "tts":
        formats = profile.get("formats", [])
        response_format = args.response_format or profile.get("default_format", "mp3")
        if response_format not in formats:
            raise SkillError(f"unsupported audio format for {args.model}; choose {', '.join(formats)}")
        voices = profile.get("voices", [])
        voice = (args.voice or profile.get("default_voice", "")).strip()
        if voices and voice not in voices:
            raise SkillError(f"unsupported voice for {args.model}; choose {', '.join(voices)}")
        if not voice:
            raise SkillError(f"--voice is required for {args.model}")
        speed = profile.get("default_speed", 1) if args.speed is None else args.speed
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.25 <= speed <= 4:
            raise SkillError("--speed must be between 0.25 and 4")
        settings.update({"kind": "tts", "voice": voice, "format": response_format, "speed": round(float(speed), 2)})
        instructions = (args.instructions or "").strip()
        if instructions:
            settings["instructions"] = instructions
    else:
        settings.update({"kind": "music"})
        for name in ("mv", "title", "tags"):
            value = getattr(args, name)
            if value:
                settings[name] = value.strip()
        if args.make_instrumental is not None:
            settings["make_instrumental"] = bool(args.make_instrumental)
    state.update({"group": args.group, "model": args.model, "settings": settings})
    try:
        read_audio_group_api_key(args.group)
    except SkillError as error:
        if "no API key is configured" not in str(error):
            raise
        state["status"] = "key_storage_decision"
    else:
        state["status"] = "ready"


def valid_task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_PATTERN.fullmatch(value):
        raise SkillError("audio response did not contain a valid public task ID")
    return value


def request_bytes(url: str, api_key: str | None, timeout: int, payload: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Accept": "application/json, audio/*"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(MAX_AUDIO_BYTES + 1)
            return response.status, dict(response.headers.items()), body
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            return error.code, dict(error.headers.items()), error.read(MAX_JSON_BYTES)
        body = error.read(min(MAX_JSON_BYTES, MAX_AUDIO_BYTES))
        raise AudioHTTPError(error.code, api_error_message(body).replace(api_key or "", "[redacted]")) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        message = str(error).replace(api_key or "", "[redacted]")
        raise SkillError(f"audio API request failed: {message}") from error


def json_response(body: bytes) -> Any:
    if len(body) > MAX_JSON_BYTES:
        raise SkillError("audio JSON response exceeds the 4 MiB safety limit")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillError("audio API response is not valid JSON") from error


def is_audio_bytes(body: bytes, response_format: str) -> bool:
    if not body or len(body) > MAX_AUDIO_BYTES:
        return False
    if response_format == "pcm":
        return True
    return (
        body.startswith(b"ID3")
        or body.startswith(b"RIFF") and body[8:12] == b"WAVE"
        or body.startswith(b"OggS")
        or body.startswith(b"fLaC")
        or body[4:8] == b"ftyp"
        or len(body) >= 2 and body[0] == 0xFF and body[1] & 0xE0 == 0xE0
    )


def valid_public_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(")],.;\"'")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return value


def decode_base64(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None


def find_audio_source(value: Any, depth: int = 0) -> tuple[bytes | None, str | None]:
    if depth > 8:
        return None, None
    if isinstance(value, str):
        url = valid_public_url(value)
        if url:
            return None, url
        for match in URL_PATTERN.findall(value):
            url = valid_public_url(match)
            if url:
                return None, url
        return None, None
    if isinstance(value, list):
        for item in value:
            data, url = find_audio_source(item, depth + 1)
            if data is not None or url:
                return data, url
        return None, None
    if not isinstance(value, dict):
        return None, None
    for key in ("b64_json", "base64", "audio_base64"):
        encoded = value.get(key)
        if isinstance(encoded, str):
            decoded = decode_base64(encoded)
            if decoded:
                return decoded, None
    for key in ("audio_url", "audioUrl", "download_url", "content_url", "url"):
        url = valid_public_url(value.get(key))
        if url:
            return None, url
    for key in ("audio", "data", "result", "results", "choices", "message", "content", "output"):
        if key in value:
            data, url = find_audio_source(value[key], depth + 1)
            if data is not None or url:
                return data, url
    for item in value.values():
        data, url = find_audio_source(item, depth + 1)
        if data is not None or url:
            return data, url
    return None, None


def download_public_audio(url: str, timeout: int, response_format: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise SkillError("audio result URL must use HTTP(S)")
    if parsed.scheme.lower() == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SkillError("audio result URL must use HTTPS outside local testing")
    status, headers, body = request_bytes(url, None, timeout)
    if 300 <= status < 400:
        location = headers.get("Location")
        if not location:
            raise SkillError("audio result redirect did not contain a location")
        return download_public_audio(urllib.parse.urljoin(url, location), timeout, response_format)
    if status < 200 or status >= 300 or not is_audio_bytes(body, response_format):
        raise SkillError("audio result URL did not return a recognized audio file")
    return body


def extract_audio_bytes(body: bytes, headers: dict[str, str], response_format: str, timeout: int) -> bytes:
    content_type = headers.get("Content-Type", "").lower()
    if is_audio_bytes(body, response_format) or content_type.startswith("audio/"):
        if not body:
            raise SkillError("audio response was empty")
        return body
    payload = json_response(body)
    data, url = find_audio_source(payload)
    if data is not None:
        return data
    if url:
        return download_public_audio(url, timeout, response_format)
    raise SkillError("audio response did not contain audio data or a downloadable audio URL")


def save_audio(body: bytes, output_dir: Path, output: str | None, response_format: str) -> Path:
    extension = FORMAT_EXTENSIONS.get(response_format, ".bin")
    if output and Path(output).suffix.lower() not in {"", extension}:
        raise SkillError(f"--output must use the .{response_format} extension")
    name = output or f"generated-audio-{dt.datetime.now(tz=dt.UTC):%Y%m%d-%H%M%S}"
    path = unique_output_path(output_dir, name, extension)
    try:
        with path.open("xb") as file:
            file.write(body)
    except OSError as error:
        raise SkillError(f"cannot save audio output: {error}") from error
    return path.resolve()


def api_key_for_state(state: dict[str, Any]) -> str:
    group = state.get("group")
    if not isinstance(group, str) or not group:
        raise SkillError("audio workflow has no selected group; start a new workflow")
    return read_audio_group_api_key(group)


def submit(path: Path, state: dict[str, Any], args: Any) -> dict[str, Any]:
    if state["status"] != "ready":
        raise SkillError(f"audio workflow is not ready to submit; current status is {state['status']}")
    api_key = api_key_for_state(state)
    base_url = api_base_url()
    model = state["model"]
    settings = state["settings"]
    if settings["kind"] == "tts":
        payload: dict[str, Any] = {
            "model": model,
            "input": state["prompt"],
            "voice": settings["voice"],
            "response_format": settings["format"],
            "speed": settings["speed"],
        }
        if settings.get("instructions"):
            payload["instructions"] = settings["instructions"]
        endpoint = f"{base_url}/audio/speech"
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": state["prompt"]}],
            "stream": False,
        }
        for key in ("mv", "title", "tags", "make_instrumental"):
            if key in settings:
                payload[key] = settings[key]
        endpoint = f"{base_url}/chat/completions"
    state.update({"status": "submitting", "base_url": base_url})
    write_private_json(path, state)
    try:
        status, headers, body = request_bytes(endpoint, api_key, args.timeout, json.dumps(payload).encode("utf-8"))
        if status < 200 or status >= 300:
            if 300 <= status < 400 and headers.get("Location"):
                body = download_public_audio(urllib.parse.urljoin(endpoint, headers["Location"]), args.timeout, settings.get("format", "mp3"))
                headers = {"Content-Type": f"audio/{settings.get('format', 'mp3')}"}
            else:
                raise AudioHTTPError(status, "unexpected response status")
        if settings["kind"] == "tts":
            audio = extract_audio_bytes(body, headers, settings["format"], args.timeout)
            state["status"] = "completed"
            write_private_json(path, state)
            output_path = save_audio(audio, Path(args.output_dir), args.output, settings["format"])
            remove_state(path)
            return {"media": "audio", "kind": "tts", "file": str(output_path), "group": state["group"], "model": model, "mime_type": mimetypes.types_map.get(FORMAT_EXTENSIONS.get(settings["format"], ""), "audio/octet-stream")}
        response = json_response(body)
        audio = None
        url = None
        audio, url = find_audio_source(response)
        if audio is None and url:
            audio = download_public_audio(url, args.timeout, "mp3")
        if audio is None:
            task_id = response.get("task_id") if isinstance(response, dict) else None
            if isinstance(task_id, str) and task_id:
                state.update({"status": "submitted", "task_id": valid_task_id(task_id)})
                write_private_json(path, state)
                return state_result(path, state)
            raise SkillError("music response did not contain audio data or a downloadable audio URL")
        state["status"] = "completed"
        write_private_json(path, state)
        output_path = save_audio(audio, Path(args.output_dir), args.output, "mp3")
        remove_state(path)
        return {"media": "audio", "kind": "music", "file": str(output_path), "group": state["group"], "model": model, "mime_type": "audio/mpeg"}
    except AudioHTTPError as error:
        state["status"] = "ready" if error.status in {400, 401, 403, 404, 422} else "submission_uncertain"
        write_private_json(path, state)
        raise
    except SkillError:
        state["status"] = "submission_uncertain"
        write_private_json(path, state)
        raise


def attach_task(path: Path, state: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    if state["status"] not in {"submitting", "submission_uncertain"}:
        raise SkillError("task ID reconciliation is only available after an uncertain submission")
    state.update({"task_id": valid_task_id(task_id), "status": "submitted"})
    write_private_json(path, state)
    return state_result(path, state)


def poll(path: Path, state: dict[str, Any], args: Any) -> dict[str, Any]:
    if state["status"] not in {"submitted", "in_progress"}:
        raise SkillError(f"audio task cannot be polled in status {state['status']}")
    task_id = valid_task_id(state.get("task_id"))
    api_key = api_key_for_state(state)
    base_url = state.get("base_url")
    if not isinstance(base_url, str) or base_url != api_base_url():
        raise SkillError("CODER_API_BASE_URL differs from the URL used for this audio task")
    deadline = time.monotonic() + args.max_wait
    while True:
        status, headers, body = request_bytes(f"{base_url}/suno/fetch/{urllib.parse.quote(task_id, safe='')}", api_key, args.timeout)
        if status < 200 or status >= 300:
            raise AudioHTTPError(status, "unexpected task response status")
        response = json_response(body)
        if isinstance(response, dict) and isinstance(response.get("data"), dict):
            task = response["data"]
        else:
            task = response
        task_status = str(task.get("status", "")).lower() if isinstance(task, dict) else ""
        if task_status in {"success", "succeeded", "completed", "complete"}:
            data, url = find_audio_source(task)
            if data is None and url:
                data = download_public_audio(url, args.timeout, "mp3")
            if data is None:
                raise SkillError("completed music task did not contain audio data or a downloadable audio URL")
            output_path = save_audio(data, Path(args.output_dir), args.output, "mp3")
            remove_state(path)
            return {"media": "audio", "kind": "music", "file": str(output_path), "group": state["group"], "model": state["model"], "task_id": task_id, "mime_type": "audio/mpeg"}
        if task_status in {"failed", "failure", "canceled", "cancelled"}:
            state["status"] = "canceled" if "cancel" in task_status else "failed"
            state["error"] = str(task.get("fail_reason") or task.get("error") or "")[:500]
            write_private_json(path, state)
            return state_result(path, state)
        state["status"] = "in_progress"
        write_private_json(path, state)
        if time.monotonic() >= deadline:
            return state_result(path, state)
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))


def remove_state(path: Path) -> None:
    path.unlink(missing_ok=True)
    if path.parent.parent == Path(tempfile.gettempdir()).resolve() and path.parent.name.startswith("coder-api-audio-"):
        path.parent.rmdir()


def main() -> int:
    args = parse_args()
    try:
        if not 1 <= args.timeout <= MAX_TIMEOUT or args.max_wait < 0 or args.poll_interval < 1:
            raise SkillError("--timeout must be 1-1000; --max-wait >= 0; --poll-interval >= 1")
        if args.api_key_stdin and not args.save_local_key:
            raise SkillError("--api-key-stdin is accepted only with --save-local-key")
        if args.task_id and not args.attach_task_id:
            raise SkillError("--task-id is accepted only with --attach-task-id")
        if args.group and not (args.select_configuration or args.remove_group_key):
            raise SkillError("--group is accepted only with --select-configuration or --remove-group-key")
        if args.begin:
            if args.state:
                raise SkillError("--begin cannot use --state")
            path, state = begin(args)
            result = state_result(path, state)
        elif args.list_models:
            catalog = load_public_catalog(set(AUDIO_MODEL_CONFIG))
            result = {"media": "audio", "catalog_source": catalog["source"], "catalog_warning": catalog["warning"], "groups": catalog.get("audio_groups", []), "model_profiles": AUDIO_MODEL_CONFIG}
        elif args.remove_group_key:
            if not args.group:
                raise SkillError("--remove-group-key requires --group")
            from generate_image import remove_local_api_key
            remove_local_api_key(local_config_path(), scope="audio", group=args.group)
            result = {"audio_group_key_removed": args.group, "config": str(local_config_path())}
        else:
            if not args.state:
                raise SkillError("--state is required")
            path = Path(args.state).expanduser().resolve()
            state = read_state(path)
            if args.save_local_key:
                if state["status"] != "key_storage_decision" or not state.get("group"):
                    raise SkillError("--save-local-key requires a selected group in key_storage_decision state")
                key = read_key_input(args.api_key_stdin, f"API key for audio group {state['group']} (input hidden): ")
                configure_api_key(local_config_path(), key, emit_reminder=False, scope="audio", group=state["group"])
                state.update({"status": "ready", "local_key_saved": True})
                write_private_json(path, state)
                result = state_result(path, state)
            elif args.select_configuration:
                select_configuration(state, args)
                write_private_json(path, state)
                result = state_result(path, state)
            elif args.submit:
                result = submit(path, state, args)
            elif args.attach_task_id:
                result = attach_task(path, state, args.task_id)
            elif args.poll:
                if args.max_wait > 3600:
                    raise SkillError("--max-wait must not exceed 3600 seconds")
                result = poll(path, state, args)
            else:
                raise SkillError("choose --save-local-key, --select-configuration, --submit, --attach-task-id, or --poll")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SkillError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
