"""Fake Miragea voice engine on httpx.MockTransport: no network, no API key, real ffmpeg."""

from __future__ import annotations

import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from youtube_multi_language_audio import cli, media

API_KEY = "sk-test-SECRET-0000"
CAPS = {
    "api_version": "1",
    "audio": {"models": [{"id": "expressive", "uses_reference_text": True}], "default_model": "expressive"},
    "subtitles": {"points_per_second": 3},
}


def tone_wav(seconds: float, freq: float = 220.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * n / rate))) for n in range(int(seconds * rate))
        ))
    return buf.getvalue()


class FakeEngine:
    """Clip length = characters / 15 per second / speed, like a real speaker."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.keys: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.recognitions = 0
        self.fail_text: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "cdn.test":
            tid = path.rsplit("/", 1)[1].split(".")[0]
            body = self.tasks[tid]["_body"]
            seconds = max(0.4, len(body["text"]) / 15 / float(body.get("speed", 1.0)))
            return httpx.Response(200, content=tone_wav(seconds))
        assert request.headers.get("authorization") == f"Bearer {API_KEY}"
        if path.endswith("/capabilities"):
            return httpx.Response(200, json=CAPS)
        if path.endswith("/audio/generations"):
            body = json.loads(request.content)
            self.created.append(body)
            self.keys.append(request.headers["idempotency-key"])
            tid = f"t{len(self.created)}"
            failed = bool(self.fail_text and self.fail_text in body["text"])
            self.tasks[tid] = {
                "task_id": tid, "_body": body,
                "status": "failed" if failed else "succeeded",
                "result": None if failed else {"audio_url": f"https://cdn.test/{tid}.wav"},
                "error": {"code": "synthesis_failed", "message": "boom", "retryable": True} if failed else None,
            }
            return httpx.Response(202, json={"task_id": tid, "status": "queued"})
        if path.endswith("/subtitles/recognitions"):
            self.recognitions += 1
            self.tasks["asr1"] = {"task_id": "asr1", "status": "succeeded",
                                  "result": {"text": "This is my reference clip.", "segments": []}}
            return httpx.Response(202, json={"task_id": "asr1", "status": "queued"})
        if "/tasks/" in path:
            task = {k: v for k, v in self.tasks[path.rsplit("/", 1)[1]].items() if not k.startswith("_")}
            return httpx.Response(200, json=task)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setenv("MIRAGEA_API_KEY", API_KEY)
    monkeypatch.delenv("MIRAGEA_BASE_URL", raising=False)
    monkeypatch.setattr(cli, "_TRANSPORT", httpx.MockTransport(fake.handler))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(cli, "_SLEEP", no_sleep)
    return fake


@pytest.fixture
def uploads(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, json={"success": True, "files": [{"url": f"https://d.host.test/{len(seen)}.wav"}]})

    monkeypatch.setattr(cli, "_UPLOAD_HTTP", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return seen


@pytest.fixture
def video(tmp_path) -> Path:
    """A 10-second test video (color + tone) made with ffmpeg."""
    path = tmp_path / "talk.mp4"
    media.run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=160x90:d=10", "-f", "lavfi", "-i", "sine=f=300:d=10",
                      "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    return path


ES = """1
00:00:00,500 --> 00:00:03,000
[Anna] Hola a todos.

2
00:00:03,200 --> 00:00:05,000
[Ben] Hoy probamos

3
00:00:05,000 --> 00:00:06,500
las pistas de audio.

4
00:00:07,000 --> 00:00:09,000
[Anna] Hasta pronto.
"""

DE = """1
00:00:00,500 --> 00:00:03,000
Hallo zusammen.

2
00:00:03,200 --> 00:00:06,500
Heute testen wir die Audiospuren.
"""


@pytest.fixture
def subs(tmp_path) -> dict[str, Path]:
    es = tmp_path / "talk.es.srt"
    es.write_text(ES, "utf-8")
    de = tmp_path / "talk.de.srt"
    de.write_text(DE, "utf-8")
    return {"es": es, "de": de}
