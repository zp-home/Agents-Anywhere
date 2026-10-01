"""Minimal SenseVoice-Small speech recognition service for Agents Anywhere.

Implements the ASR service protocol in docs/api/speech.md:
  GET  /health      -> {"status": "ok", "engine": ..., "languages": [...]}
  POST /transcribe  -> body is a 16-bit mono PCM WAV, returns {"text", "language"}
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, HTTPException, Request

MODEL_DIR = Path(os.environ.get("ASR_MODEL_DIR", "/models/sense-voice"))
NUM_THREADS = int(os.environ.get("ASR_NUM_THREADS", "2"))
MAX_BODY_BYTES = 4 * 1024 * 1024
ENGINE = "sensevoice-small"
LANGUAGES = ["auto", "zh", "en", "yue", "ja", "ko"]
LANGUAGE_TAG = re.compile(r"<\|([a-z]+)\|>")

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=str(MODEL_DIR / "model.int8.onnx"),
    tokens=str(MODEL_DIR / "tokens.txt"),
    num_threads=NUM_THREADS,
    language="auto",
    use_itn=True,
    debug=False,
)
# One decode at a time: the model is CPU bound and sharing threads between
# requests only makes each one slower.
decode_lock = asyncio.Lock()

app = FastAPI(title="Agents Anywhere ASR", docs_url=None, redoc_url=None)


def read_wav(data: bytes) -> tuple[int, np.ndarray]:
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                raise HTTPException(status_code=415, detail="audio must be 16-bit mono PCM WAV")
            sample_rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as exc:
        raise HTTPException(status_code=415, detail=f"invalid WAV: {exc}") from exc
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return sample_rate, samples


def decode(sample_rate: int, samples: np.ndarray) -> tuple[str, str | None]:
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)
    result = stream.result
    match = LANGUAGE_TAG.search(getattr(result, "lang", "") or "")
    return result.text.strip(), match.group(1) if match else None


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "engine": ENGINE, "languages": LANGUAGES}


@app.post("/transcribe")
async def transcribe(request: Request) -> dict[str, object]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    sample_rate, samples = read_wav(body)
    if samples.size == 0:
        return {"text": "", "language": None}
    async with decode_lock:
        text, language = await asyncio.to_thread(decode, sample_rate, samples)
    return {"text": text, "language": language}
