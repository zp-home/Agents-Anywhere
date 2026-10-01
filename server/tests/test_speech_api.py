from __future__ import annotations

import asyncio
import io
import struct
import wave

import httpx
import pytest
from test_backend_mvp import auth_headers, make_client

from agent_server.core.speech import parse_wav_header
from agent_server.infra.asr_client import AsrHttpClient
from agent_server.services.speech_transcription import (
    RecognizedSpeech,
    SpeechEngineHealth,
    SpeechRecognizerError,
)


def wav_bytes(seconds: float, *, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buffer.getvalue()


class FakeRecognizer:
    def __init__(self, *, healthy: bool = True, fail: bool = False) -> None:
        self.healthy = healthy
        self.fail = fail
        self.received: list[bytes] = []

    async def health(self) -> SpeechEngineHealth | None:
        return SpeechEngineHealth("sensevoice-small", ["auto", "zh", "en"]) if self.healthy else None

    async def transcribe(self, wav: bytes) -> RecognizedSpeech:
        self.received.append(wav)
        if self.fail:
            raise SpeechRecognizerError("boom")
        return RecognizedSpeech(text="  帮我跑一下测试 ", language="zh")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_SERVER_ASR_URL", raising=False)
    return make_client(tmp_path)


def upload(client, headers, data: bytes, *, field: str = "audio"):
    return client.post(
        "/api/v2/speech/transcriptions",
        headers=headers,
        files={field: ("utterance.wav", data, "audio/wav")},
    )


def test_speech_endpoints_require_login(client):
    assert client.get("/api/v2/speech/status").status_code == 401
    assert upload(client, {}, wav_bytes(1)).status_code == 401


def test_status_is_unavailable_without_asr_service(client):
    response = client.get("/api/v2/speech/status", headers=auth_headers(client))

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "engine": None,
        "languages": [],
        "sampleRate": 16000,
        "maxDurationSeconds": 60,
        "maxBytes": 2 * 1024 * 1024,
    }


def test_transcription_without_asr_service_is_unavailable(client):
    response = upload(client, auth_headers(client), wav_bytes(1))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "speech_unavailable"


def test_status_reports_engine_when_asr_is_healthy(client):
    client.app.state.speech_recognizer = FakeRecognizer()

    body = client.get("/api/v2/speech/status", headers=auth_headers(client)).json()

    assert body["available"] is True
    assert body["engine"] == "sensevoice-small"
    assert body["languages"] == ["auto", "zh", "en"]


def test_status_is_unavailable_when_asr_health_fails(client):
    client.app.state.speech_recognizer = FakeRecognizer(healthy=False)

    body = client.get("/api/v2/speech/status", headers=auth_headers(client)).json()

    assert body["available"] is False


def test_transcribes_valid_wav(client):
    recognizer = FakeRecognizer()
    client.app.state.speech_recognizer = recognizer
    audio = wav_bytes(2.5)

    response = upload(client, auth_headers(client), audio)

    assert response.status_code == 200
    assert response.json() == {"text": "帮我跑一下测试", "language": "zh", "durationMs": 2500}
    assert recognizer.received == [audio]


INVALID_AUDIO = {
    "longer-than-60s": (lambda: wav_bytes(61), 413, "audio_too_long"),
    "larger-than-2mb": (lambda: wav_bytes(1) + b"\x00" * (2 * 1024 * 1024), 413, "audio_too_long"),
    "not-wav": (lambda: b"ID3 definitely not a wav", 415, "unsupported_audio"),
    "44khz": (lambda: wav_bytes(1, rate=44_100), 415, "unsupported_audio"),
    "stereo": (lambda: wav_bytes(1, channels=2), 415, "unsupported_audio"),
    "8-bit": (lambda: wav_bytes(1, width=1), 415, "unsupported_audio"),
}


@pytest.mark.parametrize("case", sorted(INVALID_AUDIO))
def test_rejects_invalid_audio(client, case):
    build, expected_status, expected_code = INVALID_AUDIO[case]
    recognizer = FakeRecognizer()
    client.app.state.speech_recognizer = recognizer

    response = upload(client, auth_headers(client), build())

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    assert recognizer.received == []


def test_missing_audio_field_is_unsupported(client):
    client.app.state.speech_recognizer = FakeRecognizer()

    response = upload(client, auth_headers(client), wav_bytes(1), field="file")

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_audio"


def test_asr_failure_is_reported_as_unavailable(client):
    client.app.state.speech_recognizer = FakeRecognizer(fail=True)

    response = upload(client, auth_headers(client), wav_bytes(1))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "speech_unavailable"


def test_asr_http_client_parses_health_and_caches_it():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "engine": "sensevoice-small", "languages": ["zh"]})

    async def scenario() -> None:
        asr = AsrHttpClient("http://asr:8000/", transport=httpx.MockTransport(handler))
        try:
            first = await asr.health()
            second = await asr.health()
        finally:
            await asr.aclose()
        assert first == SpeechEngineHealth("sensevoice-small", ["zh"])
        assert second == first

    asyncio.run(scenario())
    assert calls == ["/health"]


def test_asr_http_client_treats_unhealthy_service_as_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "loading"})

    async def scenario() -> None:
        asr = AsrHttpClient("http://asr:8000", transport=httpx.MockTransport(handler))
        try:
            assert await asr.health() is None
        finally:
            await asr.aclose()

    asyncio.run(scenario())


def test_asr_http_client_posts_wav_and_maps_errors():
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["content-type"], request.content))
        if request.content == b"bad":
            return httpx.Response(500, json={"error": "decode failed"})
        return httpx.Response(200, json={"text": "你好", "language": "zh"})

    async def scenario() -> None:
        asr = AsrHttpClient("http://asr:8000", transport=httpx.MockTransport(handler))
        try:
            assert await asr.transcribe(b"RIFF") == RecognizedSpeech("你好", "zh")
            with pytest.raises(SpeechRecognizerError, match="500"):
                await asr.transcribe(b"bad")
        finally:
            await asr.aclose()

    asyncio.run(scenario())
    assert seen[0] == ("/transcribe", "audio/wav", b"RIFF")


def test_asr_client_is_not_created_without_url(monkeypatch):
    monkeypatch.delenv("AGENT_SERVER_ASR_URL", raising=False)
    assert AsrHttpClient.from_environment() is None
    monkeypatch.setenv("AGENT_SERVER_ASR_URL", "  ")
    assert AsrHttpClient.from_environment() is None


def test_wav_header_parser_skips_extra_chunks():
    audio = wav_bytes(1)
    # Some encoders put a LIST chunk between fmt and data.
    list_chunk = b"LIST" + struct.pack("<I", 4) + b"INFO"
    data_index = audio.index(b"data")
    patched = audio[:data_index] + list_chunk + audio[data_index:]

    info = parse_wav_header(patched)

    assert (info.sample_rate, info.channels, info.bits_per_sample, info.duration_ms) == (16000, 1, 16, 1000)
