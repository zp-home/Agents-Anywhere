"""Follower-side mirror of Codex App conversations over desktop IPC.

The Codex App (or IDE) that owns a conversation publishes its conversation
state to followers: a full snapshot when a follower subscribes, then Immer
patch batches whose ``revision`` increases by one per batch. This mirror keeps
one copy per followed conversation and only accepts updates from the current
owner at the expected base revision.

Unlike the App's own follower, a revision gap or a failed patch triggers a
targeted re-subscription so the owner sends a fresh snapshot.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from connector.logging import logger
from connector.runtimes.codex.ipc.patches import PatchError, apply_patches
from connector.runtimes.codex.ipc.transport import CodexIpcConnection
from connector.runtimes.codex.ipc.wire import LOCAL_HOST_ID, CodexIpcError

FOLLOWING_CHANGED = "thread-stream-following-changed"
FOLLOWING_STATUS_REQUESTED = "thread-stream-following-status-requested"
STATE_CHANGED = "thread-stream-state-changed"
CLIENT_STATUS_CHANGED = "client-status-changed"
DEFAULT_MAX_FOLLOWED = 64


@dataclass(frozen=True, slots=True)
class MirroredConversation:
    thread_id: str
    owner_client_id: str
    revision: int
    state: dict[str, Any]


class CodexIpcMirrorListener(Protocol):
    async def on_conversation_changed(
        self,
        thread_id: str,
        conversation: MirroredConversation | None,
    ) -> None: ...


class CodexIpcMirror:
    def __init__(
        self,
        connection: CodexIpcConnection,
        *,
        host_id: str = LOCAL_HOST_ID,
        max_followed: int = DEFAULT_MAX_FOLLOWED,
    ) -> None:
        self._connection = connection
        self._host_id = host_id
        self._max_followed = max_followed
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._pinned: set[str] = set()
        self._conversations: dict[str, MirroredConversation] = {}
        self._resyncing: set[str] = set()
        self._listener: CodexIpcMirrorListener | None = None

    async def start(self, listener: CodexIpcMirrorListener) -> None:
        self._listener = listener
        await self._connection.start(self)

    async def stop(self) -> None:
        await self._connection.stop()
        self._conversations.clear()
        self._resyncing.clear()

    @property
    def connected(self) -> bool:
        return self._connection.connected

    def conversation(self, thread_id: str) -> MirroredConversation | None:
        return self._conversations.get(thread_id)

    def owner_of(self, thread_id: str) -> str | None:
        conversation = self._conversations.get(thread_id)
        return conversation.owner_client_id if conversation is not None else None

    def followed(self) -> frozenset[str]:
        return frozenset(self._recent) | frozenset(self._pinned)

    async def pin(self, thread_id: str) -> None:
        """Follow a conversation AA is actively viewing or driving."""

        if thread_id in self.followed():
            self._pinned.add(thread_id)
            return
        self._pinned.add(thread_id)
        await self._announce(thread_id, True)

    async def set_recent(self, thread_ids: Iterable[str]) -> None:
        """Follow the most recently active conversations from inventory sync."""

        wanted: OrderedDict[str, None] = OrderedDict()
        for thread_id in thread_ids:
            if len(wanted) >= self._max_followed:
                break
            wanted[thread_id] = None
        previous = self.followed()
        self._recent = wanted
        current = self.followed()
        for thread_id in current - previous:
            await self._announce(thread_id, True)
        for thread_id in previous - current:
            await self._announce(thread_id, False)
            await self._forget(thread_id)

    async def request_owner(
        self,
        thread_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> Any:
        """Send a ``thread-follower-*`` request to the conversation's current owner.

        The request is targeted, like the App's own follower, so the router does
        not fan out discovery to every client. A timeout means the outcome is
        unknown; callers must not resend.
        """

        owner = self.owner_of(thread_id)
        if owner is None:
            raise CodexIpcError("no-owner")
        return await self._connection.request(
            method,
            {**params, "conversationId": thread_id},
            target_client_id=owner,
            timeout=timeout,
        )

    async def on_connected(self) -> None:
        for thread_id in self.followed():
            await self._announce(thread_id, True)

    async def on_disconnected(self) -> None:
        self._resyncing.clear()
        for thread_id in list(self._conversations):
            await self._forget(thread_id)

    async def on_broadcast(self, envelope: dict[str, Any]) -> None:
        method = envelope.get("method")
        params = envelope.get("params")
        source = envelope.get("sourceClientId")
        if not isinstance(params, dict) or not isinstance(source, str):
            return
        if method == CLIENT_STATUS_CHANGED:
            await self._client_status_changed(params)
            return
        if params.get("hostId") != self._host_id:
            return
        thread_id = params.get("conversationId")
        if not isinstance(thread_id, str) or thread_id not in self.followed():
            return
        if method == STATE_CHANGED:
            await self._state_changed(thread_id, source, params.get("change"))
        elif method == FOLLOWING_STATUS_REQUESTED:
            await self._announce(thread_id, True, [source])

    async def _client_status_changed(self, params: dict[str, Any]) -> None:
        client_id = params.get("clientId")
        if not isinstance(client_id, str) or client_id == self._connection.client_id:
            return
        if params.get("status") == "connected":
            for thread_id in self.followed():
                await self._announce(thread_id, True, [client_id])
            return
        for thread_id, conversation in list(self._conversations.items()):
            if conversation.owner_client_id == client_id:
                await self._forget(thread_id)

    async def _state_changed(self, thread_id: str, source: str, change: Any) -> None:
        if not isinstance(change, dict):
            return
        if change.get("type") == "snapshot":
            state = change.get("conversationState")
            revision = change.get("revision")
            if not isinstance(state, dict) or not isinstance(revision, int):
                return
            self._resyncing.discard(thread_id)
            conversation = MirroredConversation(thread_id, source, revision, state)
            self._conversations[thread_id] = conversation
            await self._notify(thread_id, conversation)
            return
        if change.get("type") != "patches":
            return
        current = self._conversations.get(thread_id)
        base = change.get("baseRevision")
        revision = change.get("revision")
        patches = change.get("patches")
        if (
            current is None
            or current.owner_client_id != source
            or current.revision != base
            or not isinstance(revision, int)
            or not isinstance(patches, list)
        ):
            await self._resync(thread_id, source, "revision-gap")
            return
        try:
            state = apply_patches(current.state, patches)
        except PatchError as exc:
            await self._resync(thread_id, source, f"patch-failed:{exc}")
            return
        if not isinstance(state, dict):
            await self._resync(thread_id, source, "patch-replaced-root")
            return
        conversation = MirroredConversation(thread_id, source, revision, state)
        self._conversations[thread_id] = conversation
        await self._notify(thread_id, conversation)

    async def _resync(self, thread_id: str, owner: str, reason: str) -> None:
        if thread_id in self._resyncing:
            return
        self._resyncing.add(thread_id)
        logger.info(
            "codex ipc resync requested thread_id={} owner={} reason={}",
            thread_id,
            owner,
            reason,
        )
        await self._announce(thread_id, True, [owner])

    async def _forget(self, thread_id: str) -> None:
        self._resyncing.discard(thread_id)
        if self._conversations.pop(thread_id, None) is not None:
            await self._notify(thread_id, None)

    async def _announce(
        self,
        thread_id: str,
        following: bool,
        targets: list[str] | None = None,
    ) -> None:
        if not self._connection.connected:
            return
        try:
            await self._connection.broadcast(
                FOLLOWING_CHANGED,
                {
                    "conversationId": thread_id,
                    "hostId": self._host_id,
                    "following": following,
                },
                target_client_ids=targets,
            )
        except CodexIpcError as exc:
            logger.debug(
                "codex ipc following broadcast skipped thread_id={} error={}",
                thread_id,
                exc.code,
            )

    async def _notify(
        self, thread_id: str, conversation: MirroredConversation | None
    ) -> None:
        if self._listener is not None:
            await self._listener.on_conversation_changed(thread_id, conversation)
