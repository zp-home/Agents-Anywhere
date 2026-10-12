from __future__ import annotations

import struct

import pytest

from connector.runtimes.codex.ipc.patches import PatchError, apply_patches
from connector.runtimes.codex.ipc.wire import (
    CodexIpcError,
    FrameDecoder,
    broadcast_envelope,
    encode_frame,
    request_envelope,
    request_version,
)


def test_frame_decoder_handles_fragmented_and_coalesced_frames() -> None:
    first = encode_frame({"type": "broadcast", "method": "a", "params": {"text": "你好"}})
    second = encode_frame({"type": "response", "requestId": "r1"})
    stream = first + second
    decoder = FrameDecoder()

    frames = []
    for index in range(0, len(stream), 3):
        frames.extend(decoder.feed(stream[index : index + 3]))

    assert frames == [
        {"type": "broadcast", "method": "a", "params": {"text": "你好"}},
        {"type": "response", "requestId": "r1"},
    ]
    assert FrameDecoder().feed(stream) == frames


def test_frame_length_prefix_is_little_endian_byte_length() -> None:
    frame = encode_frame({"k": "é"})
    (length,) = struct.unpack("<I", frame[:4])
    assert length == len(frame) - 4 == len('{"k":"é"}'.encode())


@pytest.mark.parametrize("length", [0, 268_435_457])
def test_frame_decoder_rejects_invalid_lengths(length: int) -> None:
    with pytest.raises(CodexIpcError) as exc:
        FrameDecoder().feed(struct.pack("<I", length) + b"{}")
    assert exc.value.code == "invalid-frame-length"


def test_request_versions_follow_installed_app_rules() -> None:
    assert request_version("thread-follower-start-turn", {}) == 2
    assert request_version("thread-follower-steer-turn", {}) == 1
    assert (
        request_version("thread-follower-interrupt-turn", {"expectedTurnId": "t1"}) == 4
    )
    assert request_version("thread-follower-interrupt-turn", {"mode": "user-stop"}) == 3
    assert request_version("initialize", {"clientType": "x"}) == 0

    envelope = request_envelope(
        method="thread-follower-interrupt-turn",
        params={"conversationId": "c"},
        source_client_id="me",
        target_client_id="owner",
        timeout_ms=1000,
    )
    assert envelope["version"] == 3
    assert envelope["targetClientId"] == "owner"
    assert "hostId" not in envelope


def test_broadcast_envelope_uses_table_version() -> None:
    envelope = broadcast_envelope(
        method="thread-stream-following-changed",
        params={"conversationId": "c", "hostId": "local", "following": True},
        source_client_id="me",
        target_client_ids=["owner"],
    )
    assert envelope == {
        "type": "broadcast",
        "method": "thread-stream-following-changed",
        "sourceClientId": "me",
        "version": 1,
        "params": {"conversationId": "c", "hostId": "local", "following": True},
        "targetClientIds": ["owner"],
    }


def test_apply_patches_object_and_array_operations() -> None:
    state = {
        "title": "old",
        "turns": [{"turnId": "t1", "items": [{"id": "a", "text": "he"}]}],
        "requests": [{"id": 1}, {"id": 2}, {"id": 3}],
    }
    patched = apply_patches(
        state,
        [
            {"op": "replace", "path": ["title"], "value": "new"},
            {"op": "replace", "path": ["turns", 0, "items", 0, "text"], "value": "hello"},
            {"op": "add", "path": ["turns", 0, "items", 1], "value": {"id": "b"}},
            {"op": "add", "path": ["turns", "-"], "value": {"turnId": "t2", "items": []}},
            {"op": "remove", "path": ["requests", 0]},
            {"op": "replace", "path": ["requests", "length"], "value": 1},
            {"op": "add", "path": ["cwd"], "value": "/repo"},
            {"op": "remove", "path": ["cwd"]},
        ],
    )

    assert patched == {
        "title": "new",
        "turns": [
            {"turnId": "t1", "items": [{"id": "a", "text": "hello"}, {"id": "b"}]},
            {"turnId": "t2", "items": []},
        ],
        "requests": [{"id": 2}],
    }
    assert state["title"] == "old"
    assert state["requests"] == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_apply_patches_is_atomic_on_failure() -> None:
    state = {"turns": [{"items": []}]}
    with pytest.raises(PatchError):
        apply_patches(
            state,
            [
                {"op": "add", "path": ["turns", 0, "items", 0], "value": {"id": "x"}},
                {"op": "replace", "path": ["turns", 5, "status"], "value": "done"},
            ],
        )
    assert state == {"turns": [{"items": []}]}


def test_apply_patches_value_is_copied() -> None:
    value = {"nested": [1]}
    patched = apply_patches({}, [{"op": "add", "path": ["v"], "value": value}])
    value["nested"].append(2)
    assert patched == {"v": {"nested": [1]}}
