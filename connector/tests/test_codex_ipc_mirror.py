from __future__ import annotations

import asyncio
from typing import Any

import pytest
from codex_ipc_fakes import FakeAppOwner, FakeRouter, wait_until

from connector.runtimes.codex.ipc.mirror import CodexIpcMirror, MirroredConversation
from connector.runtimes.codex.ipc.wire import CodexIpcError

THREAD = "019a0000-0000-7000-8000-000000000001"


def _state(text: str = "he") -> dict[str, Any]:
    return {
        "id": THREAD,
        "title": "Fix tests",
        "requests": [],
        "turnHistory": {
            "kind": "canonical",
            "history": {
                "isComplete": True,
                "islands": [{"id": "i1", "entries": [{"value": "turn:t1"}]}],
                "entitiesByKey": {
                    "turn:t1": {
                        "turnId": "t1",
                        "status": "inProgress",
                        "items": [
                            {"id": "u1", "type": "userMessage", "content": []},
                            {"id": "a1", "type": "agentMessage", "text": text},
                        ],
                    }
                },
            },
        },
        "unknownFutureField": {"kept": True},
    }


class RecordingListener:
    def __init__(self) -> None:
        self.events: list[tuple[str, MirroredConversation | None]] = []

    async def on_conversation_changed(
        self, thread_id: str, conversation: MirroredConversation | None
    ) -> None:
        self.events.append((thread_id, conversation))


async def _setup(*, owner_first: bool = True):
    router = FakeRouter()
    await router.start()
    owner = FakeAppOwner(router)
    if owner_first:
        await owner.connect()
        owner.own(THREAD, _state())
    mirror = CodexIpcMirror(router.connection())
    listener = RecordingListener()
    await mirror.start(listener)
    await wait_until(lambda: mirror.connected)
    return router, owner, mirror, listener


def _text(mirror: CodexIpcMirror) -> str:
    conversation = mirror.conversation(THREAD)
    assert conversation is not None
    turn = conversation.state["turnHistory"]["history"]["entitiesByKey"]["turn:t1"]
    return turn["items"][1]["text"]


def test_follow_receives_snapshot_and_applies_owner_patches() -> None:
    async def scenario() -> None:
        router, owner, mirror, listener = await _setup()
        try:
            await mirror.pin(THREAD)
            await wait_until(lambda: mirror.conversation(THREAD) is not None)
            conversation = mirror.conversation(THREAD)
            assert conversation is not None
            assert conversation.owner_client_id == owner.client_id
            assert conversation.revision == 1
            assert conversation.state["unknownFutureField"] == {"kept": True}
            assert router.client_types[mirror._connection.client_id] == "agents-anywhere"

            path = ["turnHistory", "history", "entitiesByKey", "turn:t1", "items", 1, "text"]
            await owner.send_patches(THREAD, [{"op": "replace", "path": path, "value": "hel"}])
            await owner.send_patches(THREAD, [{"op": "replace", "path": path, "value": "hello"}])
            await wait_until(lambda: _text(mirror) == "hello")
            assert mirror.conversation(THREAD).revision == 3
            assert [event[1].revision for event in listener.events] == [1, 2, 3]
        finally:
            await mirror.stop()
            await owner.disconnect()
            await router.close()

    asyncio.run(scenario())


def test_revision_gap_requests_fresh_snapshot_from_owner() -> None:
    async def scenario() -> None:
        router, owner, mirror, _ = await _setup()
        try:
            await mirror.pin(THREAD)
            await wait_until(lambda: mirror.conversation(THREAD) is not None)
            path = ["turnHistory", "history", "entitiesByKey", "turn:t1", "items", 1, "text"]
            # The owner moved on to revision 5 while AA missed batches 2..4.
            owner.states[THREAD] = _state("hello world")
            owner.revisions[THREAD] = 5
            await owner.send_patches(
                THREAD,
                [{"op": "replace", "path": path, "value": "hello world"}],
                base_revision=4,
            )
            await wait_until(lambda: mirror.conversation(THREAD).revision == 5)
            assert _text(mirror) == "hello world"
            follows = [
                envelope
                for envelope in router.received
                if envelope.get("method") == "thread-stream-following-changed"
            ]
            assert follows[-1]["targetClientIds"] == [owner.client_id]
        finally:
            await mirror.stop()
            await owner.disconnect()
            await router.close()

    asyncio.run(scenario())


def test_failed_patch_keeps_previous_state_and_resyncs() -> None:
    async def scenario() -> None:
        router, owner, mirror, listener = await _setup()
        try:
            await mirror.pin(THREAD)
            await wait_until(lambda: mirror.conversation(THREAD) is not None)
            snapshots_before = len(listener.events)
            await owner.send_patches(
                THREAD, [{"op": "replace", "path": ["missing", 3], "value": 1}]
            )
            # The owner answers the targeted re-follow with its current snapshot.
            await wait_until(lambda: len(listener.events) > snapshots_before)
            assert _text(mirror) == "he"
            assert mirror.conversation(THREAD).revision == owner.revisions[THREAD]
        finally:
            await mirror.stop()
            await owner.disconnect()
            await router.close()

    asyncio.run(scenario())


def test_owner_disconnect_forgets_conversation() -> None:
    async def scenario() -> None:
        router, owner, mirror, listener = await _setup()
        try:
            await mirror.pin(THREAD)
            await wait_until(lambda: mirror.conversation(THREAD) is not None)
            await owner.disconnect()
            await wait_until(lambda: mirror.conversation(THREAD) is None)
            assert listener.events[-1] == (THREAD, None)
            assert mirror.owner_of(THREAD) is None
        finally:
            await mirror.stop()
            await router.close()

    asyncio.run(scenario())


def test_owner_started_after_aa_receives_announcement() -> None:
    async def scenario() -> None:
        router, _, mirror, _ = await _setup(owner_first=False)
        owner = FakeAppOwner(router)
        try:
            await mirror.pin(THREAD)
            await asyncio.sleep(0.05)
            assert mirror.conversation(THREAD) is None
            owner.own(THREAD, _state())
            await owner.connect()
            await wait_until(lambda: mirror.conversation(THREAD) is not None)
            assert mirror.owner_of(THREAD) == owner.client_id
        finally:
            await mirror.stop()
            await owner.disconnect()
            await router.close()

    asyncio.run(scenario())


def test_request_owner_is_targeted_and_versioned() -> None:
    async def scenario() -> None:
        router, owner, mirror, _ = await _setup()
        try:
            await mirror.pin(THREAD)
            await wait_until(lambda: mirror.conversation(THREAD) is not None)
            owner.results["thread-follower-start-turn"] = {
                "result": {"turn": {"id": "t2", "status": "inProgress"}}
            }
            result = await mirror.request_owner(
                THREAD,
                "thread-follower-start-turn",
                {"turnStart": {"request": {"threadId": THREAD}, "context": {}}},
                timeout=2,
            )
            assert result == {"result": {"turn": {"id": "t2", "status": "inProgress"}}}
            request = owner.requests[-1]
            assert request["method"] == "thread-follower-start-turn"
            assert request["version"] == 2
            assert request["targetClientId"] == owner.client_id
            assert request["params"]["conversationId"] == THREAD
            assert "hostId" not in request
        finally:
            await mirror.stop()
            await owner.disconnect()
            await router.close()

    asyncio.run(scenario())


def test_request_without_owner_is_not_sent() -> None:
    async def scenario() -> None:
        router, _, mirror, _ = await _setup(owner_first=False)
        try:
            with pytest.raises(CodexIpcError) as exc:
                await mirror.request_owner(
                    THREAD, "thread-follower-steer-turn", {}, timeout=1
                )
            assert exc.value.code == "no-owner"
            assert not [
                envelope
                for envelope in router.received
                if envelope.get("method") == "thread-follower-steer-turn"
            ]
        finally:
            await mirror.stop()
            await router.close()

    asyncio.run(scenario())


def test_aa_never_claims_discovered_requests() -> None:
    async def scenario() -> None:
        router, owner, mirror, _ = await _setup()
        try:
            await owner._send(
                {
                    "type": "request",
                    "requestId": "from-app",
                    "sourceClientId": owner.client_id,
                    "version": 1,
                    "method": "thread-follower-steer-turn",
                    "params": {"conversationId": "not-owned-by-anyone"},
                }
            )
            await wait_until(
                lambda: any(
                    envelope.get("type") == "client-discovery-response"
                    for envelope in router.received
                )
            )
            responses = [
                envelope
                for envelope in router.received
                if envelope.get("type") == "client-discovery-response"
            ]
            assert all(r["response"] == {"canHandle": False} for r in responses)
        finally:
            await mirror.stop()
            await owner.disconnect()
            await router.close()

    asyncio.run(scenario())
