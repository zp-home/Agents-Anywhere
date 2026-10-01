from __future__ import annotations

import os
import time

import httpx
from loguru import logger

from agent_server.services.speech_transcription import (
    RecognizedSpeech,
    SpeechEngineHealth,
    SpeechRecognizerError,
)

HEALTH_CACHE_SECONDS = 15.0
HEALTH_TIMEOUT_SECONDS = 3.0
TRANSCRIBE_TIMEOUT_SECONDS = 30.0


class AsrHttpClient:
    """Talks to the self-hosted ASR service described in docs/api/speech.md."""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=TRANSCRIBE_TIMEOUT_SECONDS,
            transport=transport,
        )
        self._health: SpeechEngineHealth | None = None
        self._health_checked_at: float | None = None

    @classmethod
    def from_environment(cls) -> AsrHttpClient | None:
        base_url = os.environ.get("AGENT_SERVER_ASR_URL", "").strip()
        return cls(base_url) if base_url else None

    async def health(self) -> SpeechEngineHealth | None:
        now = time.monotonic()
        if self._health_checked_at is not None and now - self._health_checked_at < HEALTH_CACHE_SECONDS:
            return self._health
        self._health = await self._fetch_health()
        self._health_checked_at = now
        return self._health

    async def _fetch_health(self) -> SpeechEngineHealth | None:
        try:
            response = await self._client.get("/health", timeout=HEALTH_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("asr health check failed: {}", exc)
            return None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            logger.warning("asr health check returned {}", payload)
            return None
        languages = payload.get("languages")
        return SpeechEngineHealth(
            engine=str(payload.get("engine") or "unknown"),
            languages=[str(item) for item in languages] if isinstance(languages, list) else [],
        )

    async def transcribe(self, wav: bytes) -> RecognizedSpeech:
        try:
            response = await self._client.post(
                "/transcribe",
                content=wav,
                headers={"Content-Type": "audio/wav"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise SpeechRecognizerError("asr service timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise SpeechRecognizerError(f"asr service returned {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SpeechRecognizerError(f"asr service unreachable: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise SpeechRecognizerError("asr service returned an invalid response")
        language = payload.get("language")
        return RecognizedSpeech(
            text=payload["text"],
            language=language if isinstance(language, str) and language else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
