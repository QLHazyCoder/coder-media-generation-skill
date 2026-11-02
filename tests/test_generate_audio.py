from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_audio.py"
GROUP = "音频模型"
MP3_BYTES = b"ID3" + b"mock-audio"


class MockAudioAPIHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    base_url = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.requests.append({"path": self.path, "payload": payload, "authorization": self.headers.get("Authorization")})
        if self.path == "/v1/audio/speech":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(MP3_BYTES)))
            self.end_headers()
            self.wfile.write(MP3_BYTES)
            return
        if self.path == "/v1/chat/completions":
            self._json({"choices": [{"message": {"content": f"{self.__class__.base_url}/music.mp3"}}]})
            return
        self.send_error(404)

    def do_GET(self) -> None:
        if self.path != "/music.mp3":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(MP3_BYTES)))
        self.end_headers()
        self.wfile.write(MP3_BYTES)


class GenerateAudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockAudioAPIHandler)
        MockAudioAPIHandler.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self) -> None:
        MockAudioAPIHandler.requests = []
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "credentials.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_skill(self, *args: str, audio_key: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODER_API_BASE_URL"] = MockAudioAPIHandler.base_url + "/v1"
        env["CODER_MEDIA_OFFLINE_CATALOG"] = "1"
        env["CODER_MEDIA_CONFIG_PATH"] = str(self.config)
        if audio_key:
            env["CODER_AUDIO_GROUP_API_KEYS_JSON"] = json.dumps({GROUP: "audio-test-key"}, ensure_ascii=False)
        else:
            env.pop("CODER_AUDIO_GROUP_API_KEYS_JSON", None)
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=env, text=True, capture_output=True, check=False)

    def test_tts_workflow_saves_binary_audio(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            begun = self.run_skill("--begin", "--prompt", "hello")
            self.assertEqual(begun.returncode, 0, begun.stderr)
            state = json.loads(begun.stdout)["state"]
            selected = self.run_skill("--select-configuration", "--state", state, "--group", GROUP, "--model", "gemini-3.1-flash-tts", "--voice", "alloy")
            self.assertEqual(selected.returncode, 0, selected.stderr)
            result = self.run_skill("--submit", "--state", state, "--output-dir", output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(Path(output["file"]).read_bytes(), MP3_BYTES)
            self.assertEqual(MockAudioAPIHandler.requests[0]["payload"]["model"], "gemini-3.1-flash-tts")
            self.assertEqual(MockAudioAPIHandler.requests[0]["payload"]["response_format"], "mp3")

    def test_music_workflow_uses_openai_compatible_chat_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            begun = self.run_skill("--begin", "--prompt", "summer pop")
            state = json.loads(begun.stdout)["state"]
            selected = self.run_skill("--select-configuration", "--state", state, "--group", GROUP, "--model", "suno_music")
            self.assertEqual(selected.returncode, 0, selected.stderr)
            result = self.run_skill("--submit", "--state", state, "--output-dir", output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(Path(output["file"]).read_bytes(), MP3_BYTES)
            request = MockAudioAPIHandler.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["payload"]["model"], "suno_music")
            self.assertEqual(request["payload"]["messages"][0]["content"], "summer pop")

    def test_missing_audio_key_stops_before_submit(self) -> None:
        begun = self.run_skill("--begin", "--prompt", "hello", audio_key=False)
        state = json.loads(begun.stdout)["state"]
        selected = self.run_skill("--select-configuration", "--state", state, "--group", GROUP, "--model", "speech-2.8", "--voice", "voice-id", audio_key=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(json.loads(selected.stdout)["status"], "key_storage_decision")
        self.assertEqual(MockAudioAPIHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
