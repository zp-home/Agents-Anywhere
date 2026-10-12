"""Route Codex runtime operations to the Codex App owner when one exists.

Conversations owned by a running Codex App/IDE are executed by that owner:
AA sends ``thread-follower-*`` requests to it over desktop IPC, exactly like a
second App window does. Every other conversation keeps using the official SDK
client. Reads and inventory always use the SDK client.

A follower request whose outcome is unknown (timeout, disconnect) is reported
as such and never resent, because the owner may already have accepted it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from connector.logging import logger
from connector.runtime_protocol import RuntimeUpstreamError
from connector.runtimes.codex.ipc.mirror import CodexIpcMirror
from connector.runtimes.codex.ipc.projection import active_turn_id
from connector.runtimes.codex.ipc.session_sync import (
    CodexIpcSessionSync,
    ipc_request_follower_call,
)
from connector.runtimes.codex.ipc.wire import CodexIpcError
from connector.runtimes.codex.notifications.projector import notification_event
from connector.runtimes.codex.sdk.runtime_client import (
    CodexCompactResult,
    CodexInterruptTurnRequest,
    CodexModelListResult,
    CodexRuntimeClient,
    CodexStartThreadRequest,
    CodexStartTurnRequest,
    CodexSteerTurnRequest,
    CodexThreadListResult,
    CodexThreadReadResult,
    CodexThreadResult,
    CodexThreadTurnsResult,
    CodexTurnResult,
)

START_TURN_TIMEOUT = 15.0
CONTROL_TIMEOUT = 10.0
UNKNOWN_OUTCOME_MESSAGE = (
    "Codex App 没有在时限内确认这次操作（{code}）。"
    "操作可能已经生效，AA 不会自动重发；请在会话里确认结果后再决定是否重试。"
)
NOT_ACCEPTED_MESSAGE = (
    "Codex App 没有接受这次操作（{code}），操作未生效。"
    "可能是 Codex App 已不再持有该会话，或与 AA 的协议版本不兼容。"
)
REJECTED_MESSAGE = "Codex App 执行这次操作时报错：{code}"

# The router or owner answered before the owner's handler ran: nothing executed.
# ``no-client-found`` also covers a version mismatch, because the router's
# discovery asks the target and an incompatible owner answers canHandle=false.
NOT_EXECUTED_CODES = frozenset(
    {
        "no-owner",
        "not-connected",
        "no-client-found",
        "client-not-found",
        "client-cannot-handle-request",
        "request-version-mismatch",
        "no-handler-for-request",
    }
)
# The request may have reached the owner; its effect cannot be known.
UNKNOWN_OUTCOME_CODES = frozenset(
    {
        "timeout",
        "request-timeout",
        "connection-closed",
        "client-disconnected",
        "server-closed",
    }
)


class CodexIpcRoutingClient:
    def __init__(
        self,
        inner: CodexRuntimeClient,
        mirror: CodexIpcMirror,
        sync: CodexIpcSessionSync,
    ) -> None:
        self.inner = inner
        self.mirror = mirror
        self.sync = sync
        self._handler: Any = None

    async def start(self, handler: Any) -> None:
        self._handler = handler
        await self.inner.start(self._handle_sdk_notification)
        await self.mirror.start(self.sync)

    async def stop(self) -> None:
        await self.mirror.stop()
        await self.sync.stop()
        await self.inner.stop()

    async def list_models(self) -> CodexModelListResult:
        return await self.inner.list_models()

    async def list_threads(
        self,
        limit: int = 100,
        cursor: str | None = None,
        archived: bool | None = None,
    ) -> CodexThreadListResult:
        result = await self.inner.list_threads(limit=limit, cursor=cursor, archived=archived)
        if cursor is None and archived is not True:
            await self.mirror.set_recent(
                thread["id"]
                for thread in result.threads
                if isinstance(thread, Mapping) and isinstance(thread.get("id"), str)
            )
        return result

    async def read_thread(
        self,
        thread_id: str,
        include_turns: bool = True,
    ) -> CodexThreadReadResult:
        await self.mirror.pin(thread_id)
        return await self.inner.read_thread(thread_id, include_turns=include_turns)

    async def list_thread_turns(self, thread_id: str) -> CodexThreadTurnsResult:
        return await self.inner.list_thread_turns(thread_id)

    async def start_thread(self, request: CodexStartThreadRequest) -> CodexThreadResult:
        return await self.inner.start_thread(request)

    async def start_turn(self, request: CodexStartTurnRequest) -> CodexTurnResult:
        if not self.sync.owns(request.thread_id):
            return await self.inner.start_turn(request)
        from connector.runtimes.codex.sdk.client import codex_turn_start_params

        native = codex_turn_start_params(request).model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        result = await self._owner_call(
            request.thread_id,
            "thread-follower-start-turn",
            {"turnStart": {"request": native, "context": {}}},
            timeout=START_TURN_TIMEOUT,
            fallback=lambda: self.inner.start_turn(request),
        )
        if isinstance(result, CodexTurnResult):
            return result
        payload = _result_payload(result)
        return CodexTurnResult(turn_id=_turn_id(payload), payload=payload)

    async def steer_turn(self, request: CodexSteerTurnRequest) -> CodexTurnResult:
        if not self.sync.owns(request.thread_id):
            return await self.inner.steer_turn(request)
        conversation = self.mirror.conversation(request.thread_id)
        cwd = conversation.state.get("cwd") if conversation is not None else None
        message_id = request.client_message_id or str(uuid.uuid4())
        params: dict[str, Any] = {
            # The owner processes steer input itself and reads text_elements.
            "input": [{"type": "text", "text": request.content, "text_elements": []}],
            "clientUserMessageId": message_id,
            "restoreMessage": steer_restore_message(
                message_id, request.content, cwd if isinstance(cwd, str) else None
            ),
        }
        result = await self._owner_call(
            request.thread_id,
            "thread-follower-steer-turn",
            params,
            timeout=CONTROL_TIMEOUT,
            fallback=lambda: self.inner.steer_turn(request),
        )
        if isinstance(result, CodexTurnResult):
            return result
        payload = _result_payload(result)
        return CodexTurnResult(
            turn_id=_turn_id(payload) or request.turn_id, payload=payload
        )

    async def interrupt_turn(
        self,
        request: CodexInterruptTurnRequest,
    ) -> CodexTurnResult:
        conversation = self.mirror.conversation(request.thread_id)
        if conversation is None:
            return await self.inner.interrupt_turn(request)
        expected = request.turn_id or active_turn_id(conversation.state)
        params: dict[str, Any] = {"mode": "user-stop"}
        if expected is not None:
            params["expectedTurnId"] = expected
        result = await self._owner_call(
            request.thread_id,
            "thread-follower-interrupt-turn",
            params,
            timeout=CONTROL_TIMEOUT,
            fallback=lambda: self.inner.interrupt_turn(request),
        )
        if isinstance(result, CodexTurnResult):
            return result
        payload = dict(result) if isinstance(result, Mapping) else {}
        interrupted = payload.get("interruptedTurnId")
        return CodexTurnResult(
            turn_id=interrupted if isinstance(interrupted, str) else expected,
            payload=payload,
        )

    async def compact_thread(self, thread_id: str) -> CodexCompactResult:
        if not self.sync.owns(thread_id):
            return await self.inner.compact_thread(thread_id)
        result = await self._owner_call(
            thread_id,
            "thread-follower-compact-thread",
            {},
            timeout=CONTROL_TIMEOUT,
            fallback=lambda: self.inner.compact_thread(thread_id),
        )
        if isinstance(result, CodexCompactResult):
            return result
        return CodexCompactResult(
            payload=dict(result) if isinstance(result, Mapping) else {}
        )

    async def respond(
        self,
        request_id: str | int,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        route = self.sync.route_for_request(request_id)
        if route is None or not self.sync.owns(route.thread_id):
            await self.inner.respond(request_id, result)
            return
        method, params = ipc_request_follower_call(route, dict(result or {}))
        await self._owner_call(
            route.thread_id,
            method,
            params,
            timeout=CONTROL_TIMEOUT,
            fallback=None,
        )

    def __getattr__(self, name: str) -> Any:
        # Optional SDK-specific helpers (command execution, catalogs) stay on the
        # SDK client; only the CodexRuntimeClient surface is owner-aware.
        return getattr(self.inner, name)

    async def _owner_call(
        self,
        thread_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        fallback: Any,
    ) -> Any:
        try:
            return await self.mirror.request_owner(
                thread_id, method, params, timeout=timeout
            )
        except CodexIpcError as exc:
            not_executed = exc.code in NOT_EXECUTED_CODES
            logger.warning(
                "codex ipc owner call failed thread_id={} method={} error={} outcome={}",
                thread_id,
                method,
                exc.code,
                "not_executed"
                if not_executed
                else "unknown"
                if exc.code in UNKNOWN_OUTCOME_CODES
                else "owner_error",
            )
            if not_executed and fallback is not None:
                # The owner never ran the request, so the SDK path may take it.
                return await fallback()
            if not_executed:
                message = NOT_ACCEPTED_MESSAGE
            elif exc.code in UNKNOWN_OUTCOME_CODES:
                message = UNKNOWN_OUTCOME_MESSAGE
            else:
                message = REJECTED_MESSAGE
            raise RuntimeUpstreamError(message.format(code=exc.code)) from exc

    async def _handle_sdk_notification(self, message: Any) -> None:
        handler = self._handler
        if handler is None:
            return
        try:
            thread_id = notification_event(message).thread_id
        except Exception:  # noqa: BLE001
            thread_id = None
        if thread_id is not None and self.sync.owns(thread_id):
            # The App owner is the single source for this thread.
            return
        await handler(message)


def steer_restore_message(
    message_id: str, text: str, cwd: str | None
) -> dict[str, Any]:
    """Composer message the owner keeps with a steer, as the App builds by default.

    The owner reads ``cwd`` and ``context.workspaceRoots`` from it and stores it on
    the pending ``steeringUserMessage`` so the App can restore the draft if the
    steer is not accepted.
    """

    return {
        "id": message_id,
        "text": text,
        "context": {
            "prompt": text,
            "workspaceRoots": [cwd] if cwd else [],
            "commentAttachments": [],
            "imageAttachments": [],
            "fileAttachments": [],
        },
        "cwd": cwd,
        "createdAt": int(time.time() * 1000),
        "responsesapiClientMetadata": {"source": "codex"},
    }


def _result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        inner = result.get("result")
        if isinstance(inner, Mapping):
            return dict(inner)
        return dict(result)
    return {}


def _turn_id(payload: Mapping[str, Any]) -> str | None:
    turn = payload.get("turn")
    if isinstance(turn, Mapping):
        for key in ("id", "turnId"):
            value = turn.get(key)
            if isinstance(value, str):
                return value
    for key in ("turnId", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None
