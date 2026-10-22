from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_video.py"
sys.path.insert(0, str(ROOT / "scripts"))
import generate_image as image_skill
import generate_video as video


PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-image"
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


class MockVideoAPI(BaseHTTPRequestHandler):
    submitted: list[dict] = []
    auth_headers: list[str] = []
    queries = 0
    create_status = 200
    query_status = 200
    content_status = 200
    task_status = "completed"

    def log_message(self, format: str, *args: object) -> None:
        return

    def respond(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/v1/videos":
            self.send_error(404)
            return
        self.__class__.auth_headers.append(self.headers.get("Authorization", ""))
        body = self.rfile.read(int(self.headers["Content-Length"]))
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            multipart = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
            )
            payload = {}
            for part in multipart.iter_parts():
                name = part.get_param("name", header="content-disposition")
                payload[name] = part.get_payload(decode=True) if name == "image" else part.get_payload(decode=True).decode()
        else:
            payload = json.loads(body)
        self.__class__.submitted.append(payload)
        if self.__class__.create_status != 200:
            self.respond(self.__class__.create_status, b'{"error":{"message":"upstream busy"}}')
            return
        self.respond(200, b'{"id":"video-task-1","status":"queued"}')

    def do_GET(self) -> None:
        self.__class__.auth_headers.append(self.headers.get("Authorization", ""))
        if self.path == "/v1/videos/video-task-1":
            self.__class__.queries += 1
            if self.__class__.query_status != 200:
                self.respond(self.__class__.query_status, b'{"error":{"message":"try again"}}')
                return
            self.respond(200, json.dumps({"id": "video-task-1", "status": self.__class__.task_status}).encode())
        elif self.path == "/v1/videos/video-task-1/content":
            if self.__class__.content_status == 302:
                self.send_response(302)
                self.send_header("Location", "/should-not-visit")
                self.end_headers()
            elif self.__class__.content_status != 200:
                self.respond(self.__class__.content_status, b'{"error":{"message":"media unavailable"}}')
            else:
                self.respond(200, MP4_BYTES, "video/mp4")
        elif self.path == "/should-not-visit":
            self.respond(200, MP4_BYTES, "video/mp4")
        else:
            self.send_error(404)


class GenerateVideoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockVideoAPI)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self) -> None:
        MockVideoAPI.submitted = []
        MockVideoAPI.auth_headers = []
        MockVideoAPI.queries = 0
        MockVideoAPI.create_status = 200
        MockVideoAPI.query_status = 200
        MockVideoAPI.content_status = 200
        MockVideoAPI.task_status = "completed"
        self.credentials_temp = tempfile.TemporaryDirectory()
        self.default_config_path = Path(self.credentials_temp.name) / "credentials.json"

    def tearDown(self) -> None:
        self.credentials_temp.cleanup()

    def run_skill(
        self,
        *args: str,
        with_key: bool = True,
        config_path: Path | None = None,
        group_keys: dict[str, str] | None = None,
        stdin: str | None = None,
    ):
        environment = os.environ.copy()
        environment["CODER_API_BASE_URL"] = f"http://127.0.0.1:{self.server.server_port}/v1"
        environment["CODER_API_CONFIG_PATH"] = str(config_path or self.default_config_path)
        environment["CODER_MEDIA_OFFLINE_CATALOG"] = "1"
        environment.pop("CODER_API_KEY", None)
        if with_key:
            keys = group_keys or {
                "视频模型": "test-video-key",
                "seedance官渠 720&1080p 国内": "test-cn-720-key",
                "视频-seedance官渠-全分辨率-国内": "test-cn-full-key",
                "seedance官渠 全分辨率 海外版": "test-overseas-full-key",
                "seedance官渠 720p 海外版": "test-overseas-720-key",
            }
            environment["CODER_VIDEO_GROUP_API_KEYS_JSON"] = json.dumps(keys, ensure_ascii=False)
        else:
            environment.pop("CODER_VIDEO_GROUP_API_KEYS_JSON", None)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=environment, cwd=ROOT, text=True, input=stdin, capture_output=True, check=False)

    def ready(self, model: str = "grok-imagine-video-1.5", image: str | None = None, prompt: str = "waves on a pier", group: str = "视频模型", **kwargs: str) -> str:
        args = ["--begin", "--prompt", prompt]
        if image:
            args += ["--image", image]
        begun = self.run_skill(*args)
        self.assertEqual(begun.returncode, 0, begun.stderr)
        path = json.loads(begun.stdout)["state"]
        configured = self.run_skill(
            "--select-configuration", "--state", path, "--group", group, "--model", model, "--seconds", kwargs.get("seconds", "5"),
            "--resolution", kwargs.get("resolution", "480p"), *( ["--aspect-ratio", kwargs["aspect_ratio"]] if "aspect_ratio" in kwargs else []),
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertEqual(json.loads(configured.stdout)["status"], "ready")
        return path

    def test_catalog_and_begin_require_explicit_settings(self) -> None:
        catalog = self.run_skill("--list-models")
        self.assertEqual(catalog.returncode, 0)
        catalog_data = json.loads(catalog.stdout)
        video_group = next(item for item in catalog_data["groups"] if item["group"] == "视频模型")
        self.assertEqual(len(video_group["models"]), 12)
        self.assertIn("doubao-seedance-2-0-260128", video_group["models"])
        self.assertEqual(catalog_data["model_profiles"]["grok-imagine-video"]["resolutions"], ["480p", "720p"])
        self.assertEqual(catalog_data["model_profiles"]["seedance-2.0"]["duration"], {"min": 5, "max": 15})
        self.assertEqual(catalog_data["model_profiles"]["seedance-2.5"]["local_image_input"], "upload_to_public_url")
        begun = self.run_skill("--begin", "--prompt", "light in a bottle")
        state = json.loads(begun.stdout)
        self.assertEqual(state["status"], "model_selection")
        self.assertEqual(stat.S_IMODE(Path(state["state"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(state["state"]).parent.stat().st_mode), 0o700)
        rejected = self.run_skill("--select-configuration", "--state", state["state"], "--group", "视频模型", "--model", "grok-imagine-video", "--seconds", "5", "--resolution", "1080p")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unsupported resolution", rejected.stderr)
        video.remove_state(Path(state["state"]))

    def test_group_key_does_not_fall_back_to_another_video_group(self) -> None:
        begun = self.run_skill("--begin", "--prompt", "sunrise", group_keys={"视频模型": "only-general-key"})
        state_path = json.loads(begun.stdout)["state"]
        selected = self.run_skill(
            "--select-configuration", "--state", state_path, "--group", "seedance官渠 720p 海外版",
            "--model", "doubao-seedance-2-0-260128", "--seconds", "5", "--resolution", "720p",
            group_keys={"视频模型": "only-general-key"},
        )
        self.assertEqual(json.loads(selected.stdout)["status"], "key_storage_decision")
        submitted = self.run_skill("--submit", "--state", state_path, group_keys={"视频模型": "only-general-key"})
        self.assertNotEqual(submitted.returncode, 0)
        self.assertFalse(MockVideoAPI.submitted)
        video.remove_state(Path(state_path))

    def test_rollldek_model_uses_selected_groups_key(self) -> None:
        group = "seedance官渠 全分辨率 海外版"
        begun = self.run_skill("--begin", "--prompt", "A paper boat on a moonlit river")
        state_path = json.loads(begun.stdout)["state"]
        selected = self.run_skill(
            "--select-configuration", "--state", state_path, "--group", group,
            "--model", "doubao-seedance-2-5-260628", "--seconds", "8", "--resolution", "1080p",
            "--aspect-ratio", "9:16", group_keys={group: "rollldek-group-key"},
        )
        self.assertEqual(json.loads(selected.stdout)["status"], "ready")
        submitted = self.run_skill("--submit", "--state", state_path, group_keys={group: "rollldek-group-key"})
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(MockVideoAPI.auth_headers[0], "Bearer rollldek-group-key")
        self.assertEqual(MockVideoAPI.submitted[0], {
            "model": "doubao-seedance-2-5-260628",
            "resolution": "1080p",
            "duration": 8,
            "prompt": "A paper boat on a moonlit river",
            "aspect_ratio": "9:16",
        })
        video.remove_state(Path(state_path))

    def test_rollldek_payload_carries_video_audio_and_image_references(self) -> None:
        group = "视频-seedance官渠-全分辨率-国内"
        image_url = "https://media.example/start.png"
        video_url = "https://media.example/motion.mp4"
        audio_url = "https://media.example/music.mp3"
        begun = self.run_skill(
            "--begin", "--prompt", "A glass bottle on a table",
            "--image-url", image_url,
            "--reference-video-url", video_url,
            "--reference-audio-url", audio_url,
        )
        state_path = json.loads(begun.stdout)["state"]
        selected = self.run_skill(
            "--select-configuration", "--state", state_path, "--group", group,
            "--model", "doubao-seedance-2-5-260628", "--seconds", "8", "--resolution", "1080p",
            "--aspect-ratio", "9:16", "--generate-audio", "--watermark", "--seed", "42",
        )
        self.assertEqual(selected.returncode, 0, selected.stderr)
        submitted = self.run_skill("--submit", "--state", state_path)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(MockVideoAPI.submitted[0], {
            "model": "doubao-seedance-2-5-260628",
            "resolution": "1080p",
            "duration": 8,
            "prompt": "A glass bottle on a table",
            "input_reference": image_url,
            "reference_video_urls": [video_url],
            "reference_audio_urls": [audio_url],
            "aspect_ratio": "9:16",
            "generate_audio": True,
            "watermark": True,
            "seed": 42,
        })
        video.remove_state(Path(state_path))

    def test_zhiqi_models_use_duration_and_public_image_url(self) -> None:
        image_url = "https://images.example/reference.png"
        begun = self.run_skill("--begin", "--prompt", "Animate the reference image", "--image-url", image_url)
        self.assertEqual(begun.returncode, 0, begun.stderr)
        state_path = json.loads(begun.stdout)["state"]
        configured = self.run_skill(
            "--select-configuration", "--state", state_path, "--group", "视频模型", "--model", "seedance-2.5",
            "--seconds", "8", "--resolution", "1080p", "--aspect-ratio", "16:9", "--generate-audio",
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        submitted = self.run_skill("--submit", "--state", state_path)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(MockVideoAPI.submitted[0], {
            "model": "seedance-2.5",
            "duration": 8,
            "resolution": "1080p",
            "input_reference": image_url,
            "prompt": "Animate the reference image",
            "aspect_ratio": "16:9",
            "generate_audio": True,
        })
        video.remove_state(Path(state_path))

    def test_zhiqi_resolution_and_local_image_rules_are_model_specific(self) -> None:
        begun = self.run_skill("--begin", "--prompt", "sunrise")
        state_path = json.loads(begun.stdout)["state"]
        rejected = self.run_skill(
            "--select-configuration", "--state", state_path, "--group", "视频模型", "--model", "seedance-2.0",
            "--seconds", "5", "--resolution", "1080p",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unsupported resolution", rejected.stderr)
        video.remove_state(Path(state_path))

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.png"
            image.write_bytes(PNG_BYTES)
            begun = self.run_skill("--begin", "--prompt", "animate this", "--image", str(image))
            state_path = json.loads(begun.stdout)["state"]
            configured = self.run_skill(
                "--select-configuration", "--state", state_path, "--group", "视频模型", "--model", "seedance-2.5",
                "--seconds", "5", "--resolution", "720p",
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            state = video.read_state(Path(state_path))
            with self.assertRaisesRegex(video.SkillError, "uploading the local reference image"):
                video.request_payload(state)
            self.assertFalse(MockVideoAPI.submitted)
            video.remove_state(Path(state_path))

    def test_local_image_upload_reuses_selected_video_group_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.png"
            image.write_bytes(PNG_BYTES)
            config_path = Path(directory) / "credentials.json"
            image_skill.write_local_api_key(config_path, "coder-api-key")
            image_skill.write_local_api_key(config_path, "test-video-group-key", scope="video", group="视频模型")
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["api_key"], "coder-api-key")
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            image_skill.configure_api_key(config_path, "updated-coder-key", emit_reminder=False)
            self.assertEqual(video.read_video_group_api_key("视频模型", config_path), "test-video-group-key")
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["api_key"], "updated-coder-key")

            media_id = "m_0" + "1" * 25
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = json.dumps({
                "success": True,
                "data": {"url": f"https://coderapi.vip/image-upload/{media_id}"},
                "meta": {},
            }).encode()
            opener = Mock()
            opener.open.return_value = response
            with patch.dict(os.environ, {"CODER_API_CONFIG_PATH": str(config_path)}), \
                 patch.object(video.urllib.request, "build_opener", return_value=opener):
                uploaded_url = video.upload_reference_image(image, "test-video-group-key", timeout=30)

            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, video.IMAGE_UPLOAD_ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer test-video-group-key")
            self.assertEqual(request.get_header("User-agent"), "CoderMediaGenerationSkill/1.0")
            self.assertEqual(uploaded_url, f"https://coderapi.vip/image-upload/{media_id}")
            self.assertNotIn(b"test-video-group-key", request.data)
            self.assertIn(b'name="file"; filename="reference.png"', request.data)

    def test_local_image_upload_rejects_foreign_or_malformed_public_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.png"
            image.write_bytes(PNG_BYTES)
            for url in (
                "https://images.example/reference.png",
                "http://coderapi.vip/image-upload/m_" + "1" * 26,
                "https://coderapi.vip/image-upload/m_" + "8" + "1" * 25,
                "https://coderapi.vip/image-upload/m_" + "1" * 26 + "?download=1",
            ):
                response = MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = json.dumps({"data": {"url": url}}).encode()
                opener = Mock()
                opener.open.return_value = response
                with self.subTest(url=url), patch.object(video.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(video.SkillError):
                        video.upload_reference_image(image, "test-video-group-key", timeout=30)

    def test_failed_local_upload_never_submits_video_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.png"
            image.write_bytes(PNG_BYTES)
            state_path = self.ready(model="seedance-2.5", image=str(image))
            state = video.read_state(Path(state_path))
            with patch.object(video, "upload_reference_image", side_effect=video.SkillError("upload rejected")), \
                 patch.object(video, "http_json") as submit_request, \
                 patch.object(video, "api_base_url", return_value="https://api.example/v1"), \
                 patch.object(video, "read_video_group_api_key", return_value="coder-api-key"):
                with self.assertRaisesRegex(video.SkillError, "upload rejected"):
                    video.submit(Path(state_path), state, timeout=30)
            submit_request.assert_not_called()
            video.remove_state(Path(state_path))

    def test_local_image_upload_url_is_only_sent_as_zee_qi_input_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.png"
            image.write_bytes(PNG_BYTES)
            state_path = self.ready(model="seedance-2.5", image=str(image))
            state = video.read_state(Path(state_path))
            media_id = "m_0" + "1" * 25
            uploaded_url = f"https://coderapi.vip/image-upload/{media_id}"
            with patch.object(video, "upload_reference_image", return_value=uploaded_url) as upload, \
                 patch.object(video, "http_json", return_value={"id": "video-task-1", "status": "queued"}) as submit_request, \
                 patch.object(video, "api_base_url", return_value="https://api.example/v1"), \
                 patch.object(video, "read_video_group_api_key", return_value="coder-api-key"):
                result = video.submit(Path(state_path), state, timeout=30)
            upload.assert_called_once_with(image, "coder-api-key", 30)
            submitted_url, submitted_key, _, body, content_type = submit_request.call_args.args
            self.assertEqual(submitted_url, "https://api.example/v1/videos")
            self.assertEqual(submitted_key, "coder-api-key")
            self.assertEqual(content_type, "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["input_reference"], uploaded_url)
            self.assertNotIn("image", payload)
            self.assertNotIn("image_path", payload)
            self.assertEqual(result["task_id"], "video-task-1")
            self.assertEqual(state["uploaded_image_url"], uploaded_url)
            video.remove_state(Path(state_path))

    def test_media_upload_streams_file_with_selected_group_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "reference.mp4"
            media.write_bytes(MP4_BYTES)
            media_id = "v_0" + "1" * 25
            response = MagicMock()
            response.status = 201
            response.read.return_value = json.dumps({
                "success": True,
                "data": {"url": f"https://api.qlhazycoder.tech/media-upload/{media_id}"},
            }).encode()
            connection = MagicMock()
            connection.getresponse.return_value = response
            with patch.object(video.http.client, "HTTPSConnection", return_value=connection) as connect:
                uploaded_url, uploaded_id = video.upload_media_file(media, "video", "test-video-group-key", timeout=30)
            self.assertEqual(uploaded_url, f"https://api.qlhazycoder.tech/media-upload/{media_id}")
            self.assertEqual(uploaded_id, media_id)
            connect.assert_called_once_with("api.qlhazycoder.tech", timeout=30)
            connection.putrequest.assert_called_once_with("POST", "/media-upload/upload", skip_host=True)
            headers = {call.args[0]: call.args[1] for call in connection.putheader.call_args_list}
            self.assertEqual(headers["Authorization"], "Bearer test-video-group-key")
            self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
            sent = b"".join(call.args[0] for call in connection.send.call_args_list)
            self.assertIn(b'name="file"; filename="reference.mp4"', sent)
            self.assertIn(MP4_BYTES, sent)

    def test_media_upload_rejects_foreign_or_wrong_kind_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "reference.mp4"
            media.write_bytes(MP4_BYTES)
            for kind, url in (
                ("video", "https://api.qlhazycoder.tech/media-upload/a_" + "1" * 26),
                ("video", "https://coderapi.vip/image-upload/m_" + "1" * 26),
                ("video", "https://api.qlhazycoder.tech/media-upload/v_" + "1" * 26 + "?x=1"),
            ):
                response = MagicMock()
                response.status = 201
                response.read.return_value = json.dumps({"data": {"url": url}}).encode()
                connection = MagicMock()
                connection.getresponse.return_value = response
                with self.subTest(url=url), patch.object(video.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaises(video.SkillError):
                        video.upload_media_file(media, kind, "test-video-group-key", timeout=30)

    def test_local_reference_media_is_uploaded_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference_video = Path(directory) / "motion.mp4"
            reference_audio = Path(directory) / "music.mp3"
            reference_video.write_bytes(MP4_BYTES)
            reference_audio.write_bytes(b"ID3" + b"\x00" * 32)
            begun = self.run_skill(
                "--begin", "--prompt", "A glass bottle on a table",
                "--reference-video", str(reference_video),
                "--reference-audio", str(reference_audio),
            )
            self.assertEqual(begun.returncode, 0, begun.stderr)
            state_path = json.loads(begun.stdout)["state"]
            configured = self.run_skill(
                "--select-configuration", "--state", state_path, "--group", "视频-seedance官渠-全分辨率-国内",
                "--model", "doubao-seedance-2-5-260628", "--seconds", "8", "--resolution", "1080p",
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            state = video.read_state(Path(state_path))
            video_url = "https://api.qlhazycoder.tech/media-upload/v_" + "1" * 26
            audio_url = "https://api.qlhazycoder.tech/media-upload/a_" + "2" * 26
            with patch.object(video, "upload_media_file", side_effect=[(video_url, "v_" + "1" * 26), (audio_url, "a_" + "2" * 26)]) as upload, \
                 patch.object(video, "http_json", return_value={"id": "video-task-1", "status": "queued"}) as submit_request, \
                 patch.object(video, "api_base_url", return_value="https://api.example/v1"), \
                 patch.object(video, "read_video_group_api_key", return_value="coder-api-key"):
                result = video.submit(Path(state_path), state, timeout=30)
            self.assertEqual(upload.call_args_list[0].args[1], "video")
            self.assertEqual(upload.call_args_list[1].args[1], "audio")
            self.assertEqual(upload.call_args_list[0].args[2], "coder-api-key")
            payload = json.loads(submit_request.call_args.args[3])
            self.assertEqual(payload["reference_video_urls"], [video_url])
            self.assertEqual(payload["reference_audio_urls"], [audio_url])
            self.assertEqual(result["task_id"], "video-task-1")
            video.remove_state(Path(state_path))

    def test_failed_local_media_upload_never_submits_video_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference_video = Path(directory) / "motion.mp4"
            reference_video.write_bytes(MP4_BYTES)
            begun = self.run_skill(
                "--begin", "--prompt", "A glass bottle on a table",
                "--reference-video", str(reference_video),
            )
            state_path = json.loads(begun.stdout)["state"]
            self.run_skill(
                "--select-configuration", "--state", state_path, "--group", "视频-seedance官渠-全分辨率-国内",
                "--model", "doubao-seedance-2-5-260628", "--seconds", "8", "--resolution", "1080p",
            )
            state = video.read_state(Path(state_path))
            with patch.object(video, "upload_media_file", side_effect=video.SkillError("media upload rejected")), \
                 patch.object(video, "http_json") as submit_request, \
                 patch.object(video, "api_base_url", return_value="https://api.example/v1"), \
                 patch.object(video, "read_video_group_api_key", return_value="coder-api-key"):
                with self.assertRaisesRegex(video.SkillError, "media upload rejected"):
                    video.submit(Path(state_path), state, timeout=30)
            submit_request.assert_not_called()
            video.remove_state(Path(state_path))

    def test_publish_requires_file_and_group(self) -> None:
        result = self.run_skill("--publish")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--publish requires both --file and --group", result.stderr)

    def test_public_image_url_is_validated_before_workflow_creation(self) -> None:
        result = self.run_skill("--begin", "--prompt", "animate", "--image-url", "file:///tmp/image.png")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("public HTTP(S) URL", result.stderr)

    def test_create_poll_and_download_without_repeat_post(self) -> None:
        path = self.ready(resolution="1080p", aspect_ratio="9:16")
        submitted = self.run_skill("--submit", "--state", path)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(json.loads(submitted.stdout)["task_id"], "video-task-1")
        self.assertEqual(MockVideoAPI.submitted, [{"model": "grok-imagine-video-1.5", "seconds": 5, "resolution": "1080p", "prompt": "waves on a pier", "aspect_ratio": "9:16"}])
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_skill("--poll", "--state", path, "--output-dir", directory, "--output", "test.mp4", "--max-wait", "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(Path(data["file"]).read_bytes(), MP4_BYTES)
            self.assertEqual(data["model"], "grok-imagine-video-1.5")
        self.assertEqual(len(MockVideoAPI.submitted), 1)
        self.assertFalse(Path(path).exists())
        self.assertEqual(MockVideoAPI.auth_headers, ["Bearer test-video-key"] * 3)

    def test_pending_poll_can_resume_without_posting_again(self) -> None:
        path = self.ready(model="grok-imagine-video")
        self.assertEqual(self.run_skill("--submit", "--state", path).returncode, 0)
        MockVideoAPI.task_status = "queued"
        pending = self.run_skill("--poll", "--state", path, "--max-wait", "0")
        self.assertEqual(json.loads(pending.stdout)["status"], "in_progress")
        MockVideoAPI.query_status = 503
        failed_query = self.run_skill("--poll", "--state", path, "--max-wait", "0")
        self.assertEqual(failed_query.returncode, 1)
        self.assertEqual(json.loads(Path(path).read_text())["task_id"], "video-task-1")
        MockVideoAPI.query_status = 200
        MockVideoAPI.task_status = "completed"
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_skill("--poll", "--state", path, "--max-wait", "0", "--output-dir", directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(Path(json.loads(result.stdout)["file"]).exists())
        self.assertEqual(len(MockVideoAPI.submitted), 1)

    def test_transient_submit_failure_does_not_retry_a_billable_post(self) -> None:
        path = self.ready()
        MockVideoAPI.create_status = 503
        result = self.run_skill("--submit", "--state", path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(Path(path).read_text())["status"], "submission_uncertain")
        self.assertEqual(self.run_skill("--submit", "--state", path).returncode, 1)
        self.assertEqual(len(MockVideoAPI.submitted), 1)
        attached = self.run_skill("--attach-task-id", "--state", path, "--task-id", "video-task-1")
        self.assertEqual(json.loads(attached.stdout)["status"], "submitted")
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_skill("--poll", "--state", path, "--output-dir", directory)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(MockVideoAPI.submitted), 1)

    def test_definite_client_error_keeps_configuration_without_automatic_retry(self) -> None:
        path = self.ready()
        MockVideoAPI.create_status = 400
        result = self.run_skill("--submit", "--state", path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(Path(path).read_text())["status"], "ready")
        self.assertEqual(len(MockVideoAPI.submitted), 1)
        video.remove_state(Path(path))

    def test_image_to_video_uses_multipart_and_preserves_source_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.png"
            image.write_bytes(PNG_BYTES)
            path = self.ready(image=str(image), prompt="", resolution="720p")
            result = self.run_skill("--submit", "--state", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = MockVideoAPI.submitted[0]
            self.assertEqual(payload["image"], PNG_BYTES)
            self.assertEqual(payload["model"], "grok-imagine-video-1.5")
            self.assertEqual(payload["seconds"], "5")
            self.assertNotIn("aspect_ratio", payload)
            video.remove_state(Path(path))

    def test_invalid_or_oversized_reference_is_rejected_before_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.png"
            bad.write_bytes(b"not an image")
            result = self.run_skill("--begin", "--image", str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(MockVideoAPI.submitted)
            big = Path(directory) / "big.png"
            with big.open("wb") as file:
                file.write(PNG_BYTES)
                file.truncate(video.MAX_REFERENCE_BYTES + 1)
            result = self.run_skill("--begin", "--image", str(big))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("at most 20 MiB", result.stderr)

    def test_download_failure_keeps_task_and_never_reposts(self) -> None:
        path = self.ready()
        self.run_skill("--submit", "--state", path)
        MockVideoAPI.content_status = 503
        with tempfile.TemporaryDirectory() as directory:
            failed = self.run_skill("--poll", "--state", path, "--output-dir", directory)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(json.loads(Path(path).read_text())["status"], "completed")
            self.assertEqual(list(Path(directory).iterdir()), [])
            MockVideoAPI.content_status = 200
            retried = self.run_skill("--poll", "--state", path, "--output-dir", directory)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(len(MockVideoAPI.submitted), 1)

    def test_redirected_video_content_is_not_followed_with_bearer_token(self) -> None:
        path = self.ready()
        self.run_skill("--submit", "--state", path)
        MockVideoAPI.content_status = 302
        with tempfile.TemporaryDirectory() as directory:
            failed = self.run_skill("--poll", "--state", path, "--output-dir", directory)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("HTTP 302", failed.stderr)
        self.assertEqual(len(MockVideoAPI.auth_headers), 3)
        video.remove_state(Path(path))

    def test_missing_key_does_not_submit(self) -> None:
        begun = self.run_skill("--begin", "--prompt", "sunrise", with_key=False)
        state = json.loads(begun.stdout)
        self.assertEqual(state["status"], "model_selection")
        selected = self.run_skill(
            "--select-configuration", "--state", state["state"], "--group", "视频模型",
            "--model", "grok-imagine-video", "--seconds", "5", "--resolution", "480p", with_key=False,
        )
        self.assertEqual(json.loads(selected.stdout)["status"], "key_storage_decision")
        self.assertIn("security_reminder", json.loads(selected.stdout))
        result = self.run_skill("--submit", "--state", state["state"], with_key=False)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(MockVideoAPI.submitted)
        video.remove_state(Path(state["state"]))

    def test_video_key_storage_is_scoped_to_the_selected_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "config" / "credentials.json"
            begun = self.run_skill("--begin", "--prompt", "sunrise", with_key=False, config_path=credentials)
            state_path = json.loads(begun.stdout)["state"]
            selected = self.run_skill(
                "--select-configuration", "--state", state_path, "--group", "seedance官渠 720p 海外版",
                "--model", "doubao-seedance-2-0-260128", "--seconds", "5", "--resolution", "720p",
                with_key=False, config_path=credentials,
            )
            self.assertEqual(json.loads(selected.stdout)["status"], "key_storage_decision")
            saved = self.run_skill("--save-local-key", "--api-key-stdin", "--state", state_path, with_key=False,
                                   config_path=credentials, stdin="chat-video-key\n")
            self.assertEqual(saved.returncode, 0, saved.stderr)
            self.assertEqual(json.loads(saved.stdout)["status"], "ready")
            self.assertIn("security_reminder", json.loads(saved.stdout))
            config = json.loads(credentials.read_text())
            self.assertEqual(config["video_group_api_keys"]["seedance官渠 720p 海外版"], "chat-video-key")
            self.assertNotIn("api_key", config)
            self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)
            self.assertNotIn("chat-video-key", Path(state_path).read_text())
            video.remove_state(Path(state_path))

    def test_video_submit_rejects_free_form_model_overrides(self) -> None:
        path = self.ready()
        attempt = self.run_skill("--submit", "--state", path, "--model", "grok-imagine-video")
        self.assertEqual(attempt.returncode, 1)
        self.assertIn("only with --select-configuration", attempt.stderr)
        self.assertFalse(MockVideoAPI.submitted)
        video.remove_state(Path(path))

    def test_existing_output_is_not_removed_on_exclusive_create_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            occupied = Path(directory) / "existing.mp4"
            occupied.write_bytes(b"user's existing video")
            base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
            with patch.object(video, "unique_output_path", return_value=occupied):
                with self.assertRaises(video.SkillError):
                    video.download_video(base_url + "/videos/video-task-1/content", "test-video-key", 10, Path(directory), "existing.mp4")
            self.assertEqual(occupied.read_bytes(), b"user's existing video")

    def test_failed_video_stays_failed_and_cannot_resubmit(self) -> None:
        path = self.ready()
        self.run_skill("--submit", "--state", path)
        MockVideoAPI.task_status = "failed"
        result = self.run_skill("--poll", "--state", path, "--max-wait", "0")
        self.assertEqual(json.loads(result.stdout)["status"], "failed")
        self.assertEqual(self.run_skill("--submit", "--state", path).returncode, 1)
        self.assertEqual(len(MockVideoAPI.submitted), 1)
        video.remove_state(Path(path))


if __name__ == "__main__":
    unittest.main()
