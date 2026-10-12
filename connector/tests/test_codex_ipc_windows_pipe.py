from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any

import pytest
from codex_ipc_fakes import wait_until

from connector.runtimes.codex.ipc.transport import CodexIpcConnection
from connector.runtimes.codex.ipc.wire import FrameDecoder, encode_frame

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows named pipe transport"
)


class _PipeRouter(asyncio.Protocol):
    """Minimal router answering ``initialize`` over a real Windows named pipe."""

    def __init__(self, received: list[dict[str, Any]]) -> None:
        self.received = received
        self.decoder = FrameDecoder()
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        for envelope in self.decoder.feed(data):
            self.received.append(envelope)
            if envelope.get("method") == "initialize":
                assert self.transport is not None
                self.transport.write(
                    encode_frame(
                        {
                            "type": "response",
                            "requestId": envelope["requestId"],
                            "resultType": "success",
                            "method": "initialize",
                            "handledByClientId": "aa-1",
                            "result": {"clientId": "aa-1"},
                        }
                    )
                )


def test_connection_initializes_over_windows_named_pipe() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        name = rf"\\.\pipe\aa-codex-ipc-test-{uuid.uuid4().hex}"
        received: list[dict[str, Any]] = []
        servers = await loop.start_serving_pipe(  # type: ignore[attr-defined]
            lambda: _PipeRouter(received), name
        )
        connection = CodexIpcConnection(endpoints=(name,), reconnect_delay=0.05)

        class Listener:
            connected = 0

            async def on_connected(self) -> None:
                Listener.connected += 1

            async def on_disconnected(self) -> None:
                pass

            async def on_broadcast(self, envelope: dict[str, Any]) -> None:
                pass

        try:
            await connection.start(Listener())
            await wait_until(lambda: connection.connected and Listener.connected == 1)
            assert connection.client_id == "aa-1"
            assert received[0]["method"] == "initialize"
            assert received[0]["sourceClientId"] == "initializing-client"
            assert received[0]["params"] == {"clientType": "agents-anywhere"}
        finally:
            await connection.stop()
            for server in servers:
                server.close()

    asyncio.run(scenario())


def test_missing_pipe_is_a_quiet_retry_not_a_failure() -> None:
    async def scenario() -> None:
        name = rf"\\.\pipe\aa-codex-ipc-absent-{uuid.uuid4().hex}"
        connection = CodexIpcConnection(endpoints=(name,), idle_retry_delay=0.01)

        class Listener:
            async def on_connected(self) -> None:
                raise AssertionError("no router exists")

            async def on_disconnected(self) -> None:
                pass

            async def on_broadcast(self, envelope: dict[str, Any]) -> None:
                pass

        await connection.start(Listener())
        await asyncio.sleep(0.1)
        assert not connection.connected
        await connection.stop()

    asyncio.run(scenario())
