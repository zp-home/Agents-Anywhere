"""In-process fakes of the Codex desktop IPC router and an App owner.

They speak the same length-prefixed JSON frames as the installed Codex App.
The router follows the App's routing rules: it assigns client ids on
``initialize``, excludes the sender from broadcasts, honours
``targetClientIds`` and asks the target with ``client-discovery-request``
before forwarding a request.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Callable
from typing import Any

from connector.runtimes.codex.ipc.transport import CodexIpcConnection
from connector.runtimes.codex.ipc.wire import FrameDecoder, encode_frame


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class FakeRouter:
    def __init__(self) -> None:
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.clients: dict[str, asyncio.StreamWriter] = {}
        self.client_types: dict[str, str] = {}
        self._ids: dict[asyncio.StreamWriter, str] = {}
        self._discovery: dict[str, asyncio.Future[bool]] = {}
        self._forwarded: dict[str, tuple[asyncio.StreamWriter, str]] = {}
        self.received: list[dict[str, Any]] = []
        self._connections: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for writer in list(self._connections):
            writer.close()
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 1.0)
            except TimeoutError:
                pass

    def connection(self, **kwargs: Any) -> CodexIpcConnection:
        async def opener(_endpoint: str):
            return await asyncio.open_connection("127.0.0.1", self.port)

        return CodexIpcConnection(
            endpoints=("fake",),
            opener=opener,
            reconnect_delay=0.05,
            idle_retry_delay=0.05,
            **kwargs,
        )

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        decoder = FrameDecoder()
        self._connections.add(writer)
        try:
            while data := await reader.read(65536):
                for envelope in decoder.feed(data):
                    self.received.append(envelope)
                    await self._handle(writer, envelope)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._connections.discard(writer)
            await self._unregister(writer)
            writer.close()

    async def _send(self, writer: asyncio.StreamWriter, envelope: dict[str, Any]) -> None:
        if writer.is_closing():
            return
        writer.write(encode_frame(envelope))
        await writer.drain()

    async def _handle(self, writer: asyncio.StreamWriter, envelope: dict[str, Any]) -> None:
        kind = envelope.get("type")
        if kind == "request" and envelope.get("method") == "initialize":
            client_id = self._ids.get(writer) or str(uuid.uuid4())
            if writer not in self._ids:
                self._ids[writer] = client_id
                self.clients[client_id] = writer
                self.client_types[client_id] = envelope["params"]["clientType"]
                await self._status(client_id, "connected")
            await self._send(
                writer,
                {
                    "type": "response",
                    "requestId": envelope["requestId"],
                    "resultType": "success",
                    "method": "initialize",
                    "handledByClientId": client_id,
                    "result": {"clientId": client_id},
                },
            )
        elif kind == "broadcast":
            source = self._ids.get(writer, envelope.get("sourceClientId"))
            targets = envelope.get("targetClientIds")
            for client_id, other in list(self.clients.items()):
                if other is writer or (targets is not None and client_id not in targets):
                    continue
                await self._send(other, {**envelope, "sourceClientId": source})
        elif kind == "request":
            asyncio.create_task(self._route_request(writer, envelope))
        elif kind == "response":
            route = self._forwarded.pop(envelope.get("requestId"), None)
            if route is not None:
                origin, original_id = route
                await self._send(origin, {**envelope, "requestId": original_id})
        elif kind == "client-discovery-response":
            future = self._discovery.pop(envelope.get("requestId"), None)
            if future is not None and not future.done():
                future.set_result(bool(envelope.get("response", {}).get("canHandle")))

    async def _route_request(
        self, writer: asyncio.StreamWriter, envelope: dict[str, Any]
    ) -> None:
        target_id = envelope.get("targetClientId")
        candidates = (
            [target_id]
            if target_id
            else [cid for cid, other in self.clients.items() if other is not writer]
        )
        for client_id in candidates:
            target = self.clients.get(client_id)
            if target is None or target is writer:
                continue
            discovery_id = str(uuid.uuid4())
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._discovery[discovery_id] = future
            await self._send(
                target,
                {
                    "type": "client-discovery-request",
                    "requestId": discovery_id,
                    "request": envelope,
                },
            )
            try:
                can_handle = await asyncio.wait_for(future, 1.0)
            except TimeoutError:
                can_handle = False
            if can_handle:
                forwarded_id = str(uuid.uuid4())
                self._forwarded[forwarded_id] = (writer, envelope["requestId"])
                await self._send(target, {**envelope, "requestId": forwarded_id})
                return
        await self._send(
            writer,
            {
                "type": "response",
                "requestId": envelope["requestId"],
                "resultType": "error",
                "error": "no-client-found",
            },
        )

    async def _unregister(self, writer: asyncio.StreamWriter) -> None:
        client_id = self._ids.pop(writer, None)
        if client_id is None:
            return
        self.clients.pop(client_id, None)
        await self._status(client_id, "disconnected")

    async def _status(self, client_id: str, status: str) -> None:
        for other_id, other in list(self.clients.items()):
            if other_id == client_id:
                continue
            await self._send(
                other,
                {
                    "type": "broadcast",
                    "method": "client-status-changed",
                    "sourceClientId": client_id,
                    "version": 0,
                    "params": {
                        "clientId": client_id,
                        "clientType": self.client_types.get(client_id, "?"),
                        "status": status,
                    },
                },
            )


class FakeAppOwner:
    """A Codex App window owning conversations and serving follower requests."""

    def __init__(self, router: FakeRouter) -> None:
        self.router = router
        self.client_id: str | None = None
        self.states: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, int] = {}
        self.followers: dict[str, set[str]] = {}
        self.requests: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}
        self.respond = True
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._init: asyncio.Future[str] | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            "127.0.0.1", self.router.port
        )
        self._init = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._read())
        await self._send(
            {
                "type": "request",
                "requestId": "init",
                "sourceClientId": "initializing-client",
                "version": 0,
                "method": "initialize",
                "params": {"clientType": "codex-desktop"},
            }
        )
        self.client_id = await asyncio.wait_for(self._init, 2.0)

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._task is not None:
            self._task.cancel()

    def own(self, thread_id: str, state: dict[str, Any], revision: int = 1) -> None:
        self.states[thread_id] = copy.deepcopy(state)
        self.revisions[thread_id] = revision

    async def send_patches(
        self,
        thread_id: str,
        patches: list[dict[str, Any]],
        *,
        base_revision: int | None = None,
    ) -> None:
        base = self.revisions[thread_id] if base_revision is None else base_revision
        revision = base + 1
        self.revisions[thread_id] = revision
        await self._broadcast(
            "thread-stream-state-changed",
            11,
            {
                "conversationId": thread_id,
                "hostId": "local",
                "change": {
                    "type": "patches",
                    "baseRevision": base,
                    "revision": revision,
                    "patches": patches,
                },
            },
            sorted(self.followers.get(thread_id, set())),
        )

    async def _send(self, envelope: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write(encode_frame(envelope))
        await self._writer.drain()

    async def _broadcast(
        self, method: str, version: int, params: dict[str, Any], targets: list[str]
    ) -> None:
        await self._send(
            {
                "type": "broadcast",
                "method": method,
                "sourceClientId": self.client_id,
                "version": version,
                "params": params,
                "targetClientIds": targets,
            }
        )

    async def _read(self) -> None:
        assert self._reader is not None
        decoder = FrameDecoder()
        while data := await self._reader.read(65536):
            for envelope in decoder.feed(data):
                await self._handle(envelope)

    async def _handle(self, envelope: dict[str, Any]) -> None:
        kind = envelope.get("type")
        if kind == "response" and envelope.get("requestId") == "init":
            assert self._init is not None
            self._init.set_result(envelope["result"]["clientId"])
        elif kind == "broadcast" and envelope.get("method") == (
            "thread-stream-following-changed"
        ):
            params = envelope["params"]
            thread_id = params["conversationId"]
            source = envelope["sourceClientId"]
            if thread_id not in self.states:
                return
            followers = self.followers.setdefault(thread_id, set())
            if not params["following"]:
                followers.discard(source)
                return
            followers.add(source)
            await self._broadcast(
                "thread-stream-state-changed",
                11,
                {
                    "conversationId": thread_id,
                    "hostId": "local",
                    "change": {
                        "type": "snapshot",
                        "revision": self.revisions[thread_id],
                        "conversationState": self.states[thread_id],
                    },
                },
                [source],
            )
        elif kind == "client-discovery-request":
            request = envelope["request"]
            thread_id = request.get("params", {}).get("conversationId")
            await self._send(
                {
                    "type": "client-discovery-response",
                    "requestId": envelope["requestId"],
                    "response": {"canHandle": thread_id in self.states},
                }
            )
        elif kind == "request":
            self.requests.append(envelope)
            if not self.respond:
                return
            await self._send(
                {
                    "type": "response",
                    "requestId": envelope["requestId"],
                    "resultType": "success",
                    "method": envelope["method"],
                    "handledByClientId": self.client_id,
                    "result": self.results.get(envelope["method"], {"ok": True}),
                }
            )
