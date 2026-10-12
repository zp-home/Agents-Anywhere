from __future__ import annotations

import asyncio
from typing import Any

import pytest

from connector.runtime_protocol import RuntimeUpstreamError
from connector.runtimes.codex.ipc.mirror import MirroredConversation
from connector.runtimes.codex.ipc.routing_client import CodexIpcRoutingClient
from connector.runtimes.codex.ipc.session_sync import IpcRequestRoute
from connector.runtimes.codex.ipc.wire import CodexIpcError
from connector.runtimes.codex.sdk.runtime_client import (
    CodexInterruptTurnRequest,
    CodexStartTurnRequest,
    CodexSteerTurnRequest,
    CodexThreadListResult,
    CodexTurnResult,
)

OWNED = "019a0000-0000-7000-8000-00000000000a"
LOCAL = "019a0000-0000-7000-8000-00000000000b"


class FakeInner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def start(self, handler: Any) -> None:
        self.handler = handler

    async def stop(self) -> None:
        self.calls.append(("stop", None))

    async def list_threads(self, limit=100, cursor=None, archived=None):
        return CodexThreadListResult(threads=({"id": OWNED}, {"id": LOCAL}))

    async def start_turn(self, request):
        self.calls.append(("start_turn", request))
        return CodexTurnResult(turn_id="sdk-turn", payload={})

    async def steer_turn(self, request):
        self.calls.append(("steer_turn", request))
        return CodexTurnResult(turn_id=request.turn_id, payload={})

    async def interrupt_turn(self, request):
        self.calls.append(("interrupt_turn", request))
        return CodexTurnResult(turn_id=request.turn_id, payload={})

    async def respond(self, request_id, result=None):
        self.calls.append(("respond", (request_id, result)))


class StubMirror:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any], float]] = []
        self.result: Any = {"ok": True}
        self.error: CodexIpcError | None = None
        self.recent: list[str] = []
        self.pinned: list[str] = []
        self.state = {
            "cwd": "F:/repo",
            "turnHistory": {
                "kind": "canonical",
                "history": {
                    "islands": [{"entries": [{"value": "turn:t9"}]}],
                    "entitiesByKey": {
                        "turn:t9": {"turnId": "t9", "status": "inProgress", "items": []}
                    },
                },
            }
        }

    def conversation(self, thread_id: str):
        if thread_id != OWNED:
            return None
        return MirroredConversation(OWNED, "app", 3, self.state)

    async def request_owner(self, thread_id, method, params, *, timeout):
        self.requests.append((thread_id, method, params, timeout))
        if self.error is not None:
            raise self.error
        return self.result

    async def set_recent(self, thread_ids):
        self.recent = list(thread_ids)

    async def pin(self, thread_id):
        self.pinned.append(thread_id)

    async def start(self, listener):
        pass

    async def stop(self):
        pass


class StubSync:
    def __init__(self) -> None:
        self.routes: dict[str, IpcRequestRoute] = {}

    def owns(self, thread_id: str) -> bool:
        return thread_id == OWNED

    def route_for_request(self, request_id):
        return self.routes.get(str(request_id))

    async def stop(self) -> None:
        pass


def _client() -> tuple[CodexIpcRoutingClient, FakeInner, StubMirror, StubSync]:
    inner, mirror, sync = FakeInner(), StubMirror(), StubSync()
    return CodexIpcRoutingClient(inner, mirror, sync), inner, mirror, sync  # type: ignore[arg-type]


def test_unowned_thread_keeps_sdk_path() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        result = await client.start_turn(
            CodexStartTurnRequest(thread_id=LOCAL, content="hi")
        )
        assert result.turn_id == "sdk-turn"
        assert [name for name, _ in inner.calls] == ["start_turn"]
        assert mirror.requests == []

    asyncio.run(scenario())


def test_owned_thread_start_turn_goes_to_app_owner() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        mirror.result = {"result": {"turn": {"id": "t10", "status": "inProgress"}}}
        result = await client.start_turn(
            CodexStartTurnRequest(
                thread_id=OWNED,
                content="run the tests",
                client_message_id="cm-1",
                model="gpt-5.5",
            )
        )
        assert inner.calls == []
        assert result.turn_id == "t10"
        thread_id, method, params, _ = mirror.requests[0]
        assert (thread_id, method) == (OWNED, "thread-follower-start-turn")
        native = params["turnStart"]["request"]
        assert params["turnStart"]["context"] == {}
        assert native["threadId"] == OWNED
        assert native["clientUserMessageId"] == "cm-1"
        assert native["model"] == "gpt-5.5"
        assert native["input"][0]["type"] == "text"
        assert native["input"][0]["text"] == "run the tests"

    asyncio.run(scenario())


def test_owned_thread_steer_and_interrupt_use_follower_requests() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        await client.steer_turn(
            CodexSteerTurnRequest(
                thread_id=OWNED, turn_id="t9", content="also lint", client_message_id="cm-2"
            )
        )
        mirror.result = {"interruptedTurnId": "t9", "ok": True}
        interrupted = await client.interrupt_turn(
            CodexInterruptTurnRequest(thread_id=OWNED, turn_id="")
        )
        assert inner.calls == []
        steer, interrupt = mirror.requests
        assert steer[1] == "thread-follower-steer-turn"
        assert steer[2]["input"] == [
            {"type": "text", "text": "also lint", "text_elements": []}
        ]
        assert steer[2]["clientUserMessageId"] == "cm-2"
        restore = steer[2]["restoreMessage"]
        assert restore["id"] == "cm-2"
        assert restore["text"] == "also lint"
        assert restore["cwd"] == "F:/repo"
        assert restore["context"]["workspaceRoots"] == ["F:/repo"]
        assert interrupt[1] == "thread-follower-interrupt-turn"
        assert interrupt[2] == {"mode": "user-stop", "expectedTurnId": "t9"}
        assert interrupted.turn_id == "t9"

    asyncio.run(scenario())


def test_approval_response_routes_to_owner() -> None:
    async def scenario() -> None:
        client, inner, mirror, sync = _client()
        sync.routes["7"] = IpcRequestRoute(
            OWNED, "item/commandExecution/requestApproval", 7
        )
        await client.respond("7", {"decision": "acceptForSession"})
        await client.respond("other", {"decision": "accept"})

        assert mirror.requests[0][1:3] == (
            "thread-follower-command-approval-decision",
            {"requestId": 7, "decision": "acceptForSession"},
        )
        assert inner.calls == [("respond", ("other", {"decision": "accept"}))]

    asyncio.run(scenario())


def test_unknown_outcome_is_reported_and_never_resent() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        mirror.error = CodexIpcError("timeout")
        with pytest.raises(RuntimeUpstreamError) as exc:
            await client.start_turn(CodexStartTurnRequest(thread_id=OWNED, content="x"))
        assert "timeout" in str(exc.value)
        assert len(mirror.requests) == 1
        assert inner.calls == []

    asyncio.run(scenario())


def test_owner_gone_before_dispatch_falls_back_to_sdk() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        mirror.error = CodexIpcError("no-owner")
        result = await client.start_turn(
            CodexStartTurnRequest(thread_id=OWNED, content="x")
        )
        assert result.turn_id == "sdk-turn"
        assert [name for name, _ in inner.calls] == ["start_turn"]

    asyncio.run(scenario())


def test_inventory_follows_recent_threads_and_drops_owned_sdk_events() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        received: list[Any] = []

        async def handler(message):
            received.append(message)

        await client.start(handler)
        await client.list_threads()
        assert mirror.recent == [OWNED, LOCAL]
        await client.list_threads(cursor="page-2")
        assert mirror.recent == [OWNED, LOCAL]

        owned = {"method": "item/agentMessage/delta", "params": {"threadId": OWNED}}
        local = {"method": "item/agentMessage/delta", "params": {"threadId": LOCAL}}
        await inner.handler(owned)
        await inner.handler(local)
        assert received == [local]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code", ["no-client-found", "request-version-mismatch", "not-connected"]
)
def test_owner_did_not_execute_start_falls_back_to_sdk(code: str) -> None:
    async def scenario() -> None:
        client, _, mirror, _ = _client()
        mirror.error = CodexIpcError(code)
        result = await client.start_turn(
            CodexStartTurnRequest(thread_id=OWNED, content="x")
        )
        assert result.turn_id == "sdk-turn"
        assert len(mirror.requests) == 1

    asyncio.run(scenario())


def test_owner_error_is_reported_without_fallback() -> None:
    async def scenario() -> None:
        client, inner, mirror, _ = _client()
        mirror.error = CodexIpcError("Conversation must be resumed before loading history")
        with pytest.raises(RuntimeUpstreamError) as exc:
            await client.start_turn(CodexStartTurnRequest(thread_id=OWNED, content="x"))
        assert "报错" in str(exc.value)
        assert "Conversation must be resumed" in str(exc.value)
        assert inner.calls == []

    asyncio.run(scenario())


def test_unanswerable_approval_reports_not_accepted() -> None:
    async def scenario() -> None:
        client, inner, mirror, sync = _client()
        sync.routes["7"] = IpcRequestRoute(
            OWNED, "item/fileChange/requestApproval", 7
        )
        mirror.error = CodexIpcError("no-client-found")
        with pytest.raises(RuntimeUpstreamError) as exc:
            await client.respond("7", {"decision": "decline"})
        assert "未生效" in str(exc.value)
        assert inner.calls == []

    asyncio.run(scenario())
