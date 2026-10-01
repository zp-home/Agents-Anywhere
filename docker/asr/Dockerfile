# Speech recognition sidecar: SenseVoice-Small (int8) served through sherpa-onnx.
# Build from the repository root:
#   docker build -f docker/asr/Dockerfile -t agents-anywhere-asr docker/asr
FROM python:3.12-slim

ARG MODEL_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
ARG MODEL_NAME=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASR_MODEL_DIR=/models/sense-voice \
    ASR_NUM_THREADS=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Keep only the int8 model and tokens; the fp32 model in the archive is ~900 MB.
RUN python - "${MODEL_URL}" "${MODEL_NAME}" "${ASR_MODEL_DIR}" <<'PY'
import os, sys, tarfile, tempfile, urllib.request
url, name, target = sys.argv[1:4]
os.makedirs(target, exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    archive = os.path.join(tmp, "model.tar.bz2")
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:bz2") as tar:
        for member in ("model.int8.onnx", "tokens.txt"):
            source = tar.extractfile(f"{name}/{member}")
            with open(os.path.join(target, member), "wb") as out:
                out.write(source.read())
PY

COPY server.py .

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
