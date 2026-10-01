from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from starlette.requests import HTTPConnection

from agent_server.core.models import UserView
from agent_server.core.speech import SPEECH_MAX_BYTES, SpeechStatus, SpeechTranscription
from agent_server.deps import current_user
from agent_server.services.speech_transcription import (
    SpeechAudioTooLongError,
    SpeechError,
    SpeechTranscriptionService,
    SpeechUnavailableError,
    SpeechUnsupportedAudioError,
)

router = APIRouter(prefix="/speech", tags=["speech"])


def get_speech_service(conn: HTTPConnection) -> SpeechTranscriptionService:
    return SpeechTranscriptionService(conn.app.state.speech_recognizer)


SpeechServiceDep = Annotated[SpeechTranscriptionService, Depends(get_speech_service)]
CurrentUserDep = Annotated[UserView, Depends(current_user)]


def _http_error(exc: SpeechError) -> HTTPException:
    if isinstance(exc, SpeechAudioTooLongError):
        status_code = 413
    elif isinstance(exc, SpeechUnsupportedAudioError):
        status_code = 415
    elif isinstance(exc, SpeechUnavailableError):
        status_code = 503
    else:
        status_code = 500
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


@router.get("/status", response_model=SpeechStatus)
async def speech_status(_user: CurrentUserDep, service: SpeechServiceDep) -> SpeechStatus:
    return await service.status()


@router.post("/transcriptions", response_model=SpeechTranscription)
async def create_transcription(
    _user: CurrentUserDep,
    service: SpeechServiceDep,
    audio: Annotated[UploadFile | None, File()] = None,
) -> SpeechTranscription:
    if audio is None:
        raise _http_error(SpeechUnsupportedAudioError("missing audio file field"))
    data = await audio.read(SPEECH_MAX_BYTES + 1)
    try:
        return await service.transcribe(data)
    except SpeechError as exc:
        raise _http_error(exc) from exc
