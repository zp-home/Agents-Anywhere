"""Codex desktop IPC wire format.

The Codex App and IDE extension share a local router (``\\\\.\\pipe\\codex-ipc`` on
Windows, ``$CODEX_HOME/ipc/ipc.sock`` elsewhere). Frames are a little-endian
uint32 byte length followed by one UTF-8 JSON envelope. This is not the
app-server JSON-RPC protocol.

Method versions mirror the installed Codex App (26.930). The router and every
peer drop broadcasts whose version differs from their own table and reject
requests with ``request-version-mismatch``, so an unknown version is never
"best effort" compatible.
"""

from __future__ import annotations

import json
import struct
import uuid
from collections.abc import Mapping
from typing import Any

MAX_FRAME_BYTES = 268_435_456
INITIALIZING_CLIENT_ID = "initializing-client"
LOCAL_HOST_ID = "local"

METHOD_VERSIONS: Mapping[str, int] = {
    "thread-stream-state-changed": 11,
    "thread-stream-following-changed": 1,
    "thread-stream-following-status-requested": 1,
    "ipc-connection-reset": 1,
    "thread-read-state-changed": 3,
    "thread-archived": 2,
    "thread-unarchived": 1,
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-load-complete-history": 1,
    "thread-follower-compact-thread": 1,
    "thread-follower-steer-turn": 1,
    "thread-follower-interrupt-turn": 4,
    "thread-follower-update-thread-settings": 2,
    "thread-follower-update-daybreak": 1,
    "thread-follower-edit-last-user-turn": 2,
    "thread-follower-command-approval-decision": 1,
    "thread-follower-file-approval-decision": 1,
    "thread-follower-permissions-request-approval-response": 1,
    "thread-follower-submit-user-input": 1,
    "thread-follower-submit-mcp-server-elicitation-response": 1,
    "thread-follower-set-queued-follow-ups-state": 1,
    "thread-follower-remove-queued-message": 1,
    "thread-follower-clear-queued-messages": 1,
    "thread-queued-followups-changed": 2,
}


class CodexIpcError(Exception):
    """A coordination failure identified by the router or peer error string."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def method_version(method: str) -> int:
    """Version for a method; unlisted methods (initialize, client-status-changed) are 0."""

    return METHOD_VERSIONS.get(method, 0)


def request_version(method: str, params: Any) -> int:
    """Version of a local request (no outer ``hostId``), as the App computes it.

    An interrupt without ``expectedTurnId`` is the older v3 form; owners accept
    both v3 and v4 for local requests.
    """

    if method == "thread-follower-interrupt-turn" and (
        not isinstance(params, Mapping) or params.get("expectedTurnId") is None
    ):
        return 3
    return method_version(method)


def encode_frame(envelope: Mapping[str, Any]) -> bytes:
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_FRAME_BYTES:
        raise CodexIpcError("frame-too-large")
    return struct.pack("<I", len(payload)) + payload


class FrameDecoder:
    """Incremental decoder for length-prefixed JSON frames.

    Bytes may arrive fragmented or coalesced. A zero or oversized length is a
    protocol violation; Codex closes the connection in that case and so do we.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        frames: list[dict[str, Any]] = []
        while len(self._buffer) >= 4:
            (length,) = struct.unpack_from("<I", self._buffer, 0)
            if length == 0 or length > MAX_FRAME_BYTES:
                raise CodexIpcError("invalid-frame-length")
            if len(self._buffer) < 4 + length:
                break
            payload = bytes(self._buffer[4 : 4 + length])
            del self._buffer[: 4 + length]
            envelope = json.loads(payload.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise CodexIpcError("invalid-envelope")
            frames.append(envelope)
        return frames


def new_request_id() -> str:
    return str(uuid.uuid4())


def request_envelope(
    *,
    method: str,
    params: Any,
    source_client_id: str,
    target_client_id: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Build a request envelope for the local host (no outer ``hostId``)."""

    envelope: dict[str, Any] = {
        "type": "request",
        "requestId": new_request_id(),
        "sourceClientId": source_client_id,
        "version": request_version(method, params),
        "method": method,
        "params": params,
    }
    if target_client_id is not None:
        envelope["targetClientId"] = target_client_id
    if timeout_ms is not None:
        envelope["timeoutMs"] = timeout_ms
    return envelope


def broadcast_envelope(
    *,
    method: str,
    params: Any,
    source_client_id: str,
    target_client_ids: list[str] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "type": "broadcast",
        "method": method,
        "sourceClientId": source_client_id,
        "version": method_version(method),
        "params": params,
    }
    if target_client_ids is not None:
        envelope["targetClientIds"] = list(target_client_ids)
    return envelope
