from __future__ import annotations

import asyncio
import copy
from typing import Any

from test_codex_runtime import FakeHost

from connector.runtime_protocol import RuntimeSessionStateCache
from connector.runtimes.codex.domain.notices import CodexNoticeRegistry
from connector.runtimes.codex.domain.pending_messages import (
    PendingClientMessageRegistry,
)
from connector.runtimes.codex.ipc.mirror import MirroredConversation
from connector.runtimes.codex.ipc.session_sync import (
    CodexIpcSessionSync,
    IpcRequestRoute,
    ipc_request_follower_call,
)
from connector.runtimes.codex.notifications.notices import CodexNoticeHandler

THREAD = "019a0000-0000-7000-8000-000000000002"


def _state(*, text: str = "he", status: str = "inProgress", requests=None) -> dict:
    return {
        "title": "Refactor parser",
        "cwd": "F:/repo",
        "requests": requests or [],
        "turnHistory": {
            "kind": "canonical",
            "history": {
                "isComplete": True,
                "islands": [{"entries": [{"value": "turn:t1"}]}],
                "entitiesByKey": {
                    "turn:t1": {
                        "turnId": "t1",
                        "status": status,
                        "items": [
                            {
                                "id": "u1",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "fix it"}],
                            },
                            {"id": "a1", "type": "agentMessage", "text": text},
                        ],
                    }
                },
            },
        },
    }


class StubMirror:
    def __init__(self) -> None:
        self.conversations: dict[str, MirroredConversation] = {}
        self.revision = 0

    def set(self, state: dict[str, Any]) -> MirroredConversation:
        self.revision += 1
        conversation = MirroredConversation(
            THREAD, "app-owner", self.revision, copy.deepcopy(state)
        )
        self.conversations[THREAD] = conversation
        return conversation

    def conversation(self, thread_id: str) -> MirroredConversation | None:
        return self.conversations.get(thread_id)


def _sync(host: FakeHost, mirror: StubMirror) -> tuple[CodexIpcSessionSync, dict]:
    states = RuntimeSessionStateCache("codex", host)
    active: dict[str, str] = {}
    notices = CodexNoticeRegistry()
    sync = CodexIpcSessionSync(
        host=host,
        mirror=mirror,  # type: ignore[arg-type]
        session_states=states,
        active_turn_ids=active,
        notices=notices,
        notice_handler=CodexNoticeHandler(host, states, active, notices),
        pending_messages=PendingClientMessageRegistry(connector_id="conn_test"),
        coalesce_seconds=0,
    )
    return sync, active


def test_first_publish_syncs_full_timeline_and_running_state() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, active = _sync(host, mirror)
        mirror.set(_state())
        await sync.flush(THREAD)

        assert len(host.timeline_syncs) == 1
        published = host.timeline_syncs[0]
        assert published["external_session_id"] == THREAD
        assert published["complete"] is True
        assert len(published["items"]) == 2
        assert host.state_updates[-1]["status"] == "running"
        assert host.state_updates[-1]["metadata"]["source"] == "codex.ipc"
        assert active[published["session_id"]] == "t1"
        assert sync.owns(THREAD)

    asyncio.run(scenario())


def test_streaming_text_only_upserts_changed_item() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, _ = _sync(host, mirror)
        mirror.set(_state(text="he"))
        await sync.flush(THREAD)
        states_before = len(host.state_updates)

        mirror.set(_state(text="hello"))
        await sync.flush(THREAD)

        assert len(host.timeline_syncs) == 1
        assert len(host.timeline_item_upserts) == 1
        assert "hello" in str(host.timeline_item_upserts[0].content)
        # Unchanged status must not re-publish state on every token.
        assert len(host.state_updates) == states_before

    asyncio.run(scenario())


def test_turn_completion_reports_turn_end_and_idle() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, active = _sync(host, mirror)
        mirror.set(_state())
        await sync.flush(THREAD)
        mirror.set(_state(text="done", status="completed"))
        await sync.flush(THREAD)

        assert [(t["turn_id"], t["outcome"]) for t in host.turn_ends] == [("t1", "completed")]
        assert host.state_updates[-1]["status"] == "idle"
        assert active == {}

        # A later unrelated update does not repeat the turn end.
        mirror.set(_state(text="done!", status="completed"))
        await sync.flush(THREAD)
        assert len(host.turn_ends) == 1

    asyncio.run(scenario())


def test_owner_request_raises_notice_and_resolves_when_answered_elsewhere() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, _ = _sync(host, mirror)
        request = {
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": THREAD,
                "turnId": "t1",
                "itemId": "cmd1",
                "command": "rm -rf build",
            },
        }
        mirror.set(_state(requests=[request]))
        await sync.flush(THREAD)

        assert len(host.notice_upserts) == 1
        notice = host.notice_upserts[0]
        assert notice.status == "open"
        assert notice.context["approvalSource"]["requestId"] == 7
        assert host.state_updates[-1]["status"] == "waiting_approval"
        assert sync.route_for_request("7") == IpcRequestRoute(
            THREAD, "item/commandExecution/requestApproval", 7
        )

        # The user approved in the Codex App: the owner drops the request.
        mirror.set(_state(requests=[]))
        await sync.flush(THREAD)
        assert host.notice_upserts[-1].notice_id == notice.notice_id
        assert host.notice_upserts[-1].status == "resolved"
        assert sync.route_for_request("7") is None
        assert host.state_updates[-1]["status"] == "running"

    asyncio.run(scenario())


def test_owner_loss_clears_routes_and_stops_owning() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, _ = _sync(host, mirror)
        request = {
            "id": "q1",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": THREAD,
                "turnId": "t1",
                "questions": [
                    {"id": "q", "header": "Pick", "question": "Continue?", "options": None}
                ],
            },
        }
        mirror.set(_state(requests=[request]))
        await sync.flush(THREAD)
        assert sync.route_for_request("q1") is not None
        assert host.notice_upserts[-1].context["requestId"] == "q1"
        mirror.conversations.clear()
        await sync.on_conversation_changed(THREAD, None)

        assert not sync.owns(THREAD)
        assert sync.route_for_request("q1") is None

    asyncio.run(scenario())


def test_request_responses_map_to_owner_follower_methods() -> None:
    command = IpcRequestRoute(THREAD, "item/commandExecution/requestApproval", 7)
    assert ipc_request_follower_call(command, {"decision": "accept"}) == (
        "thread-follower-command-approval-decision",
        {"requestId": 7, "decision": "accept"},
    )
    file_change = IpcRequestRoute(THREAD, "item/fileChange/requestApproval", "f")
    assert ipc_request_follower_call(file_change, {"decision": "decline"}) == (
        "thread-follower-file-approval-decision",
        {"requestId": "f", "decision": "decline"},
    )
    permissions = IpcRequestRoute(THREAD, "item/permissions/requestApproval", 3)
    payload = {"permissions": {"network": True}, "scope": "turn"}
    assert ipc_request_follower_call(permissions, payload) == (
        "thread-follower-permissions-request-approval-response",
        {"requestId": 3, "response": payload},
    )
    question = IpcRequestRoute(THREAD, "item/tool/requestUserInput", "q")
    answers = {"answers": {"q1": {"answers": ["yes"]}}}
    assert ipc_request_follower_call(question, answers) == (
        "thread-follower-submit-user-input",
        {"requestId": "q", "response": answers},
    )


def test_unreadable_request_does_not_block_projection() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, _ = _sync(host, mirror)
        broken = {"id": "bad", "method": "item/tool/requestUserInput", "params": {}}
        mirror.set(_state(requests=[broken]))
        await sync.flush(THREAD)

        assert len(host.timeline_syncs) == 1
        assert host.notice_upserts == []
        # A pending question still blocks the turn even without an AA notice.
        assert host.state_updates[-1]["status"] == "waiting"

    asyncio.run(scenario())


def test_question_waits_and_owner_loss_expires_notices_and_turn() -> None:
    async def scenario() -> None:
        host, mirror = FakeHost(), StubMirror()
        sync, active = _sync(host, mirror)
        question = {
            "id": "q9",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": THREAD,
                "turnId": "t1",
                "questions": [{"id": "q", "header": "H", "question": "Go?", "options": None}],
            },
        }
        mirror.set(_state(requests=[question]))
        await sync.flush(THREAD)
        assert host.state_updates[-1]["status"] == "waiting"
        session_id = host.timeline_syncs[0]["session_id"]
        assert active[session_id] == "t1"

        mirror.conversations.clear()
        await sync.on_conversation_changed(THREAD, None)
        assert session_id not in active
        assert host.notice_upserts[-1].status == "expired"
        assert host.notice_upserts[-1].metadata["close_reason"] == "owner_unavailable"

    asyncio.run(scenario())
