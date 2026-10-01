# Speech API

Server-side speech recognition used by the mobile voice call mode. The server
does not run a model itself; it forwards audio to a self-hosted ASR service
(`docker/asr`, SenseVoice-Small via sherpa-onnx) configured with
`AGENT_SERVER_ASR_URL`. Clients that cannot or do not want to use the phone's
own recognition service upload one utterance at a time and get text back.

Speech is a server-level feature, not a runtime or session capability, so it is
not part of [effective capabilities](./capabilities.md). Clients probe it with
the status endpoint.

Both endpoints require a user access token (`Authorization: Bearer ...`).

## `GET /api/v2/speech/status`

```json
{
  "available": true,
  "engine": "sensevoice-small",
  "languages": ["auto", "zh", "en", "yue", "ja", "ko"],
  "sampleRate": 16000,
  "maxDurationSeconds": 60,
  "maxBytes": 2097152
}
```

- `available` is `false` when `AGENT_SERVER_ASR_URL` is not set or the ASR
  service does not answer its health check. `engine` is then `null` and
  `languages` is empty; the limits are still returned.
- The health result is cached for 15 seconds per server process.

## `POST /api/v2/speech/transcriptions`

`multipart/form-data` with one file field `audio`: a WAV file containing
16-bit PCM, mono, 16000 Hz, at most `maxDurationSeconds` long and `maxBytes`
large. The language is detected automatically.

```json
{
  "text": "帮我看一下登录接口为什么报 500",
  "language": "zh",
  "durationMs": 2380
}
```

`text` may be empty when the audio contains no speech.

### Errors

Errors use the standard structured detail shape:
`{"detail": {"code": "...", "message": "..."}}`.

| Status | `code` | Meaning |
| --- | --- | --- |
| 413 | `audio_too_long` | Longer than `maxDurationSeconds` or larger than `maxBytes`. |
| 415 | `unsupported_audio` | Missing `audio` field, not a WAV file, or not 16-bit mono 16 kHz PCM. |
| 503 | `speech_unavailable` | ASR is not configured, or the ASR service failed or timed out. |

## ASR service protocol

`AGENT_SERVER_ASR_URL` points at a service with two endpoints. Any service that
implements them can replace the bundled container.

- `GET /health` → `{"status": "ok", "engine": "sensevoice-small", "languages": [...]}`
- `POST /transcribe` with the WAV bytes as the request body
  (`Content-Type: audio/wav`) → `{"text": "...", "language": "zh"}`

## Client support

Android ≥ 2.1.0 offers a "server" recognition mode during voice calls when the
status endpoint reports `available: true`. Other clients ignore this API.
