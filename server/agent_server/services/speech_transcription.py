from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_server.core.speech import (
    SPEECH_MAX_BYTES,
    SPEECH_MAX_DURATION_SECONDS,
    SPEECH_SAMPLE_RATE,
    InvalidWavError,
    SpeechStatus,
    SpeechTranscription,
    parse_wav_header,
)


@dataclass(frozen=True)
class SpeechEngineHealth:
    engine: str
    languages: list[str]


@dataclass(frozen=True)
class RecognizedSpeech:
    text: str
    language: str | None


class SpeechRecognizerPort(Protocol):
    async def health(self) -> SpeechEngineHealth | None:
        """Return engine details, or None when the ASR service is unreachable."""

    async def transcribe(self, wav: bytes) -> RecognizedSpeech:
        """Recognize one WAV utterance; raise SpeechRecognizerError on failure."""


class SpeechRecognizerError(Exception):
    """Raised by recognizer adapters when the ASR service fails."""


class SpeechError(Exception):
    code = "speech_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SpeechUnavailableError(SpeechError):
    code = "speech_unavailable"


class SpeechAudioTooLongError(SpeechError):
    code = "audio_too_long"


class SpeechUnsupportedAudioError(SpeechError):
    code = "unsupported_audio"


class SpeechTranscriptionService:
    def __init__(self, recognizer: SpeechRecognizerPort | None) -> None:
        self._recognizer = recognizer

    async def status(self) -> SpeechStatus:
        health = await self._recognizer.health() if self._recognizer is not None else None
        if health is None:
            return SpeechStatus(available=False)
        return SpeechStatus(available=True, engine=health.engine, languages=health.languages)

    async def transcribe(self, audio: bytes) -> SpeechTranscription:
        if len(audio) > SPEECH_MAX_BYTES:
            raise SpeechAudioTooLongError(f"audio is larger than {SPEECH_MAX_BYTES} bytes")
        try:
            info = parse_wav_header(audio)
        except InvalidWavError as exc:
            raise SpeechUnsupportedAudioError(str(exc)) from exc
        if (
            info.channels != 1
            or info.bits_per_sample != 16
            or info.sample_rate != SPEECH_SAMPLE_RATE
        ):
            raise SpeechUnsupportedAudioError(
                "audio must be 16-bit mono PCM at "
                f"{SPEECH_SAMPLE_RATE} Hz (got {info.bits_per_sample}-bit, "
                f"{info.channels} channels, {info.sample_rate} Hz)"
            )
        if info.duration_ms > SPEECH_MAX_DURATION_SECONDS * 1000:
            raise SpeechAudioTooLongError(
                f"audio is longer than {SPEECH_MAX_DURATION_SECONDS} seconds"
            )
        if self._recognizer is None:
            raise SpeechUnavailableError("speech recognition is not configured")
        try:
            recognized = await self._recognizer.transcribe(audio)
        except SpeechRecognizerError as exc:
            raise SpeechUnavailableError(f"speech recognition failed: {exc}") from exc
        return SpeechTranscription(
            text=recognized.text.strip(),
            language=recognized.language,
            durationMs=info.duration_ms,
        )
