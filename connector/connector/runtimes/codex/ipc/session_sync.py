"""Publish mirrored Codex App conversations to the AA host.

The App owner is authoritative for conversations it owns. While a mirror
exists, this publisher is the only timeline/state source for that thread:
state snapshots are coalesced, re-projected with the regular Codex timeline
mapping and only changed items are sent to the host.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from connector.logging import logger
from connector.runtime_protocol import (
    RuntimeSessionStateCache,
    RuntimeTimelineItem,
)
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtimes.codex.domain import sessions as codex_sessions
from connector.runtimes.codex.domain.approvals import (
    CODEX_COMMAND_APPROVAL_REQUEST,
    CODEX_FILE_CHANGE_APPROVAL_REQUEST,
    CODEX_PERMISSIONS_APPROVAL_REQUEST,
    is_approval_request,
)
from connector.runtimes.codex.domain.notices import CodexNoticeRegistry
from connector.runtimes.codex.domain.pending_messages import (
    PendingClientMessageRegistry,
)
from connector.runtimes.codex.ipc.mirror import (
    CodexIpcMirror,
    MirroredConversation,
)
from connector.runtimes.codex.ipc.projection import (
    TERMINAL_TURN_OUTCOMES,
    active_turn_id,
    history_complete,
    pending_requests,
    session_status,
    thread_from_conversation,
    turn_statuses,
)
from connector.runtimes.codex.notifications.notices import CodexNoticeHandler
from connector.runtimes.codex.timeline.snapshot import timeline_items_from_thread

COALESCE_SECONDS = 0.05
SOURCE = "codex.ipc"


@dataclass(frozen=True, slots=True)
class IpcRequestRoute:
    thread_id: str
    method: str
    request_id: str | int


@dataclass(slots=True)
class CodexIpcSessionSync:
    host: RuntimeHostClient
    mirror: CodexIpcMirror
    session_states: RuntimeSessionStateCache
    active_turn_ids: dict[str, str]
    notices: CodexNoticeRegistry
    notice_handler: CodexNoticeHandler
    pending_messages: PendingClientMessageRegistry
    coalesce_seconds: float = COALESCE_SECONDS
    request_routes: dict[str, IpcRequestRoute] = field(default_factory=dict)
    _published: dict[str, dict[str, RuntimeTimelineItem]] = field(default_factory=dict)
    _turns: dict[str, dict[str, str]] = field(default_factory=dict)
    _requests: dict[str, set[str]] = field(default_factory=dict)
    _flushes: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def owns(self, thread_id: str) -> bool:
        """Whether a Codex App owner currently drives this thread's AA projection."""

        return self.mirror.conversation(thread_id) is not None

    def route_for_request(self, request_id: str | int) -> IpcRequestRoute | None:
        return self.request_routes.get(str(request_id))

    async def on_conversation_changed(
        self,
        thread_id: str,
        conversation: MirroredConversation | None,
    ) -> None:
        if conversation is None:
            await self._owner_lost(thread_id)
            return
        if thread_id in self._flushes:
            return
        self._flushes[thread_id] = asyncio.create_task(self._delayed_flush(thread_id))

    async def stop(self) -> None:
        tasks = list(self._flushes.values())
        self._flushes.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _delayed_flush(self, thread_id: str) -> None:
        try:
            await asyncio.sleep(self.coalesce_seconds)
        finally:
            self._flushes.pop(thread_id, None)
        try:
            await self.flush(thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "codex ipc publish failed thread_id={} error_type={} error={}",
                thread_id,
                exc.__class__.__name__,
                str(exc) or exc.__class__.__name__,
            )

    async def flush(self, thread_id: str) -> None:
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            conversation = self.mirror.conversation(thread_id)
            if conversation is None:
                return
            session_id = self._session_id(thread_id)
            await self._publish_timeline(session_id, thread_id, conversation)
            await self._publish_turns(session_id, thread_id, conversation)
            await self._publish_requests(session_id, thread_id, conversation)
            await self.session_states.update(
                session_id=session_id,
                external_session_id=thread_id,
                status=session_status(conversation.state),
                metadata={
                    "source": SOURCE,
                    "codexIpcOwner": conversation.owner_client_id,
                },
            )

    async def _publish_timeline(
        self,
        session_id: str,
        thread_id: str,
        conversation: MirroredConversation,
    ) -> None:
        thread = thread_from_conversation(thread_id, conversation.state)
        items = tuple(
            item
            for item in timeline_items_from_thread(
                session_id,
                thread_id,
                thread,
                None,
                self.pending_messages,
            )
            if item.type not in {"turn.start", "turn.end"}
        )
        current = {item.id: item for item in items}
        previous = self._published.get(thread_id)
        complete = history_complete(conversation.state)
        removed = previous is not None and bool(previous.keys() - current.keys())
        if previous is None or (removed and complete):
            await self.host.timeline_sync(
                session_id=session_id,
                runtime="codex",
                items=items,
                external_session_id=thread_id,
                complete=complete,
                metadata={"source": SOURCE},
            )
        else:
            for item in items:
                if previous.get(item.id) != item:
                    await self.host.timeline_item_upsert(item)
        self._published[thread_id] = current

    async def _publish_turns(
        self,
        session_id: str,
        thread_id: str,
        conversation: MirroredConversation,
    ) -> None:
        current = turn_statuses(conversation.state)
        previous = self._turns.get(thread_id)
        self._turns[thread_id] = current
        active = active_turn_id(conversation.state)
        if active is not None:
            self.active_turn_ids[session_id] = active
        else:
            self.active_turn_ids.pop(session_id, None)
        if previous is None:
            return
        for turn_id, status in current.items():
            outcome = TERMINAL_TURN_OUTCOMES.get(status)
            if outcome is None or previous.get(turn_id) == status:
                continue
            if previous.get(turn_id) in TERMINAL_TURN_OUTCOMES:
                continue
            await self.host.session_turn_ended(
                session_id=session_id,
                runtime="codex",
                external_session_id=thread_id,
                turn_id=turn_id,
                outcome=outcome,
                metadata={"source": SOURCE},
            )

    async def _publish_requests(
        self,
        session_id: str,
        thread_id: str,
        conversation: MirroredConversation,
    ) -> None:
        requests = pending_requests(conversation.state)
        current = {str(request["id"]) for request in requests}
        previous = self._requests.get(thread_id, set())
        self._requests[thread_id] = current
        for request in requests:
            request_id = str(request["id"])
            method = request["method"]
            self.request_routes[request_id] = IpcRequestRoute(
                thread_id, method, request["id"]
            )
            if request_id in previous:
                continue
            params = request.get("params")
            params = dict(params) if isinstance(params, dict) else {}
            try:
                if is_approval_request(method):
                    await self.notice_handler.handle_approval_request(
                        session_id=session_id,
                        thread_id=thread_id,
                        method=method,
                        params=params,
                        request_id=request["id"],
                    )
                else:
                    await self.notice_handler.handle_user_input_request(
                        session_id=session_id,
                        thread_id=thread_id,
                        method=method,
                        params=params,
                        request_id=request["id"],
                    )
            except ValueError as exc:
                # The owner still holds the request and can answer it in the
                # App; one unreadable request must not block the projection.
                logger.warning(
                    "codex ipc request notice skipped thread_id={} request_id={} method={} error={}",
                    thread_id,
                    request_id,
                    method,
                    exc,
                )
        for request_id in previous - current:
            self.request_routes.pop(request_id, None)
            await self._close_request_notices(session_id, request_id)

    async def _close_request_notices(
        self,
        session_id: str,
        request_id: str,
        *,
        status: str = "resolved",
        reason: str = "owner_resolved",
    ) -> None:
        """Close AA notices for an owner request that is no longer answerable here."""

        for notice in self.notices.current_for_session(session_id):
            if _notice_request_id(notice.context) != request_id:
                continue
            if notice.status not in {"open", "responding"}:
                continue
            closed = self.notices.transition(
                notice.notice_id,
                status=status,
                response_required=False,
                blocking=None,
                actions=(),
                metadata={"source": SOURCE, "close_reason": reason},
            )
            if closed is not None:
                await self.host.notice_upsert(closed)

    async def _owner_lost(self, thread_id: str) -> None:
        """The App stopped owning the thread; the SDK/polling path takes over."""

        task = self._flushes.pop(thread_id, None)
        if task is not None:
            task.cancel()
        self._published.pop(thread_id, None)
        self._turns.pop(thread_id, None)
        session_id = self._session_id(thread_id)
        # AA can no longer see or answer the owner's turn and requests. The
        # rollout-based sync reports the final state on its next pass.
        self.active_turn_ids.pop(session_id, None)
        for request_id in self._requests.pop(thread_id, set()):
            self.request_routes.pop(request_id, None)
            await self._close_request_notices(
                session_id, request_id, status="expired", reason="owner_unavailable"
            )
        logger.info("codex ipc owner released thread_id={}", thread_id)

    def _session_id(self, thread_id: str) -> str:
        cached = self.session_states.get_by_external_session_id(thread_id)
        if cached is not None:
            return cached.session_id
        return codex_sessions.stable_session_id(
            getattr(self.host, "session_namespace", self.host.connector_id),
            thread_id,
        )


def _notice_request_id(context: Any) -> str | None:
    """Request id stored on a Codex notice, read like the interaction responder."""

    if not isinstance(context, dict):
        return None
    request_id = context.get("requestId")
    if not isinstance(request_id, str | int):
        source = context.get("approvalSource")
        request_id = source.get("requestId") if isinstance(source, dict) else None
    return str(request_id) if isinstance(request_id, str | int) else None


def ipc_request_follower_call(
    route: IpcRequestRoute,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Map an app-server server-request response to the owner's follower method.

    Command and file approvals carry the app-server ``result.decision`` value;
    the other requests forward the whole result object, exactly as the App's
    own follower does.
    """

    request_id = route.request_id
    if route.method == CODEX_COMMAND_APPROVAL_REQUEST:
        return "thread-follower-command-approval-decision", {
            "requestId": request_id,
            "decision": payload.get("decision"),
        }
    if route.method == CODEX_FILE_CHANGE_APPROVAL_REQUEST:
        return "thread-follower-file-approval-decision", {
            "requestId": request_id,
            "decision": payload.get("decision"),
        }
    if route.method == CODEX_PERMISSIONS_APPROVAL_REQUEST:
        return "thread-follower-permissions-request-approval-response", {
            "requestId": request_id,
            "response": payload,
        }
    return "thread-follower-submit-user-input", {
        "requestId": request_id,
        "response": payload,
    }
