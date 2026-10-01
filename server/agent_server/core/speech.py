from __future__ import annotations

import struct
from dataclasses import dataclass

from pydantic import BaseModel

SPEECH_SAMPLE_RATE = 16_000
SPEECH_MAX_DURATION_SECONDS = 60
SPEECH_MAX_BYTES = 2 * 1024 * 1024


class SpeechStatus(BaseModel):
    available: bool
    engine: str | None = None
    languages: list[str] = []
    sampleRate: int = SPEECH_SAMPLE_RATE
    maxDurationSeconds: int = SPEECH_MAX_DURATION_SECONDS
    maxBytes: int = SPEECH_MAX_BYTES


class SpeechTranscription(BaseModel):
    text: str
    language: str | None = None
    durationMs: int


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    data_bytes: int

    @property
    def duration_ms(self) -> int:
        frame_bytes = self.channels * self.bits_per_sample // 8
        if frame_bytes <= 0 or self.sample_rate <= 0:
            return 0
        return self.data_bytes * 1000 // (frame_bytes * self.sample_rate)


class InvalidWavError(ValueError):
    pass


def parse_wav_header(data: bytes) -> WavInfo:
    """Read the RIFF/WAVE `fmt ` and `data` chunks of a PCM WAV file."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise InvalidWavError("not a RIFF/WAVE file")
    offset = 12
    fmt: tuple[int, int, int, int] | None = None
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (chunk_size,) = struct.unpack_from("<I", data, offset + 4)
        body = offset + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16 or body + 16 > len(data):
                raise InvalidWavError("truncated fmt chunk")
            audio_format, channels, sample_rate = struct.unpack_from("<HHI", data, body)
            (bits_per_sample,) = struct.unpack_from("<H", data, body + 14)
            fmt = (audio_format, channels, sample_rate, bits_per_sample)
        elif chunk_id == b"data":
            if fmt is None:
                raise InvalidWavError("data chunk before fmt chunk")
            audio_format, channels, sample_rate, bits_per_sample = fmt
            if audio_format != 1:
                raise InvalidWavError("only PCM WAV is supported")
            data_bytes = min(chunk_size, len(data) - body)
            return WavInfo(sample_rate, channels, bits_per_sample, data_bytes)
        offset = body + chunk_size + (chunk_size & 1)
    raise InvalidWavError("missing data chunk")
