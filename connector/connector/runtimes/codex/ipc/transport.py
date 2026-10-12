"""Client-only connection to the Codex desktop IPC router.

Agents Anywhere never listens on, unlinks or replaces the Codex endpoint and
never takes part in router election. When no Codex App/IDE is running there is
no router; that is a normal state and the connection simply retries later.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from connector.logging import logger
from connector.runtimes.codex.ipc.wire import (
    INITIALIZING_CLIENT_ID,
    CodexIpcError,
    FrameDecoder,
    broadcast_envelope,
    encode_frame,
    method_version,
    request_envelope,
)

CLIENT_TYPE = "agents-anywhere"
WINDOWS_PIPE = r"\\.\pipe\codex-ipc"
DEFAULT_REQUEST_TIMEOUT = 5.0
INITIALIZE_TIMEOUT = 5.0
READ_CHUNK = 1 << 16

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]
Opener = Callable[[str], Awaitable[Streams]]


class CodexIpcListener(Protocol):
    async def on_connected(self) -> None: ...

    async def on_disconnected(self) -> None: ...

    async def on_broadcast(self, envelope: dict[str, Any]) -> None: ...


def default_endpoints(codex_home: str | None) -> tuple[str, ...]:
    """Candidate router endpoints in the order the Codex App itself checks them."""

    if sys.platform == "win32":
        return (WINDOWS_PIPE,)
    home = Path(codex_home) if codex_home else Path.home() / ".codex"
    uid = os.getuid() if hasattr(os, "getuid") else None
    legacy_name = f"ipc-{uid}.sock" if uid else "ipc.sock"
    return (
        str(home / "ipc" / "ipc.sock"),
        str(Path(tempfile.gettempdir()) / "codex-ipc" / legacy_name),
    )


async def open_endpoint(endpoint: str) -> Streams:
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        create_pipe_connection = getattr(loop, "create_pipe_connection", None)
        if create_pipe_connection is None:
            raise CodexIpcError("unsupported-event-loop")
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await create_pipe_connection(lambda: protocol, endpoint)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer
    return await asyncio.open_unix_connection(endpoint)


class CodexIpcConnection:
    def __init__(
        self,
        *,
        endpoints: tuple[str, ...],
        opener: Opener = open_endpoint,
        reconnect_delay: float = 1.0,
        idle_retry_delay: float = 5.0,
        client_type: str = CLIENT_TYPE,
    ) -> None:
        self._endpoints = endpoints
        self._opener = opener
        self._reconnect_delay = reconnect_delay
        self._idle_retry_delay = idle_retry_delay
        self._client_type = client_type
        self._listener: CodexIpcListener | None = None
        self._client_id = INITIALIZING_CLIENT_ID
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._broadcasts: asyncio.Queue[dict[str, Any] | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def client_id(self) -> str | None:
        return None if self._client_id == INITIALIZING_CLIENT_ID else self._client_id

    @property
    def connected(self) -> bool:
        return self.client_id is not None and self._writer is not None

    async def start(self, listener: CodexIpcListener) -> None:
        if self._task is not None:
            return
        self._listener = listener
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="codex-ipc-connection")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        writer = self._writer
        if writer is not None:
            writer.close()
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def request(
        self,
        method: str,
        params: Any,
        *,
        target_client_id: str | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        if not self.connected:
            raise CodexIpcError("not-connected")
        envelope = request_envelope(
            method=method,
            params=params,
            source_client_id=self._client_id,
            target_client_id=target_client_id,
            timeout_ms=int(timeout * 1000),
        )
        response = await self._send_request(envelope, timeout)
        if response.get("resultType") != "success":
            raise CodexIpcError(str(response.get("error") or "error"))
        return response.get("result")

    async def broadcast(
        self,
        method: str,
        params: Any,
        *,
        target_client_ids: list[str] | None = None,
    ) -> None:
        if not self.connected:
            raise CodexIpcError("not-connected")
        if target_client_ids is not None and not target_client_ids:
            return
        await self._write(
            broadcast_envelope(
                method=method,
                params=params,
                source_client_id=self._client_id,
                target_client_ids=target_client_ids,
            )
        )

    async def _run(self) -> None:
        while not self._stopping:
            streams = await self._open_any()
            if streams is None:
                await asyncio.sleep(self._idle_retry_delay)
                continue
            try:
                await self._session(*streams)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "codex ipc session ended error_type={} error={}",
                    exc.__class__.__name__,
                    str(exc) or exc.__class__.__name__,
                )
            if not self._stopping:
                await asyncio.sleep(self._reconnect_delay)

    async def _open_any(self) -> Streams | None:
        for endpoint in self._endpoints:
            try:
                return await self._opener(endpoint)
            except (OSError, CodexIpcError):
                continue
        return None

    async def _session(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writer = writer
        self._broadcasts = asyncio.Queue()
        decoder = FrameDecoder()
        dispatcher = asyncio.create_task(self._dispatch_broadcasts(self._broadcasts))
        reading = asyncio.create_task(self._read_loop(reader, decoder))
        try:
            await self._initialize()
            logger.info("codex ipc connected client_id={}", self._client_id)
            if self._listener is not None:
                await self._broadcasts.put({"type": "_connected"})
            await reading
        finally:
            reading.cancel()
            was_initialized = self.client_id is not None
            self._client_id = INITIALIZING_CLIENT_ID
            self._writer = None
            writer.close()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexIpcError("connection-closed"))
            self._pending.clear()
            if was_initialized:
                await self._broadcasts.put({"type": "_disconnected"})
            await self._broadcasts.put(None)
            try:
                await dispatcher
            except asyncio.CancelledError:
                dispatcher.cancel()
                raise
            self._broadcasts = None

    async def _initialize(self) -> None:
        envelope = request_envelope(
            method="initialize",
            params={"clientType": self._client_type},
            source_client_id=INITIALIZING_CLIENT_ID,
        )
        response = await self._send_request(envelope, INITIALIZE_TIMEOUT)
        result = response.get("result")
        client_id = result.get("clientId") if isinstance(result, dict) else None
        if response.get("resultType") != "success" or not isinstance(client_id, str):
            raise CodexIpcError("initialize-failed")
        self._client_id = client_id

    async def _send_request(
        self, envelope: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        request_id = envelope["requestId"]
        self._pending[request_id] = future
        try:
            await self._write(envelope)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise CodexIpcError("timeout") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _write(self, envelope: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            raise CodexIpcError("not-connected")
        frame = encode_frame(envelope)
        async with self._write_lock:
            writer.write(frame)
            await writer.drain()

    async def _read_loop(
        self, reader: asyncio.StreamReader, decoder: FrameDecoder
    ) -> None:
        try:
            while True:
                data = await reader.read(READ_CHUNK)
                if not data:
                    return
                for envelope in decoder.feed(data):
                    await self._handle(envelope)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexIpcError("connection-closed"))

    async def _handle(self, envelope: dict[str, Any]) -> None:
        kind = envelope.get("type")
        if kind == "response":
            future = self._pending.get(str(envelope.get("requestId")))
            if future is not None and not future.done():
                future.set_result(envelope)
            return
        if kind == "client-discovery-request":
            # AA owns no Codex conversation, so it never claims a request.
            await self._write(
                {
                    "type": "client-discovery-response",
                    "requestId": envelope.get("requestId"),
                    "response": {"canHandle": False},
                }
            )
            return
        if kind == "request":
            await self._write(
                {
                    "type": "response",
                    "requestId": envelope.get("requestId"),
                    "resultType": "error",
                    "error": "no-handler-for-request",
                }
            )
            return
        if kind != "broadcast":
            return
        targets = envelope.get("targetClientIds")
        if isinstance(targets, list) and self._client_id not in targets:
            return
        method = envelope.get("method")
        if not isinstance(method, str):
            return
        if envelope.get("version", 0) != method_version(method):
            return
        if self._broadcasts is not None:
            await self._broadcasts.put(envelope)

    async def _dispatch_broadcasts(
        self, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        while True:
            envelope = await queue.get()
            if envelope is None:
                return
            listener = self._listener
            if listener is None:
                continue
            try:
                kind = envelope.get("type")
                if kind == "_connected":
                    await listener.on_connected()
                elif kind == "_disconnected":
                    await listener.on_disconnected()
                else:
                    await listener.on_broadcast(envelope)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "codex ipc listener failed method={} error_type={} error={}",
                    envelope.get("method") or envelope.get("type"),
                    exc.__class__.__name__,
                    str(exc) or exc.__class__.__name__,
                )
