"""Loopback SOCKS5 tunnel that pins browser connections to public IPs."""

import asyncio
import ipaddress
import socket
from contextlib import suppress
from typing import Self

from httpx import URL

from pricewatch.fetching.safety import Resolver, UnsafeUrlError, public_addresses, system_resolver

SOCKS_OK = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
SOCKS_DENIED = b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00"
SOCKS_UNREACHABLE = b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00"


def _write(writer: asyncio.StreamWriter, data: bytes) -> None:
    if writer.is_closing():
        raise ConnectionError("SOCKS destination is closed")
    try:
        writer.write(data)
    except RuntimeError as error:
        # uvloop raises RuntimeError rather than OSError for a closed TCP transport.
        if not writer.is_closing():
            raise
        raise ConnectionError("SOCKS destination is closed") from error


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await asyncio.wait_for(reader.read(65536), timeout=60):
            _write(writer, chunk)
            await writer.drain()
    except (ConnectionError, OSError, TimeoutError):
        pass
    finally:
        if writer.can_write_eof():
            with suppress(ConnectionError, OSError, RuntimeError):
                writer.write_eof()


async def _wait_closed(writer: asyncio.StreamWriter) -> None:
    closed = asyncio.create_task(writer.wait_closed())
    try:
        # Shield the protocol's shared close waiter from wait_for cancellation.
        await asyncio.wait_for(asyncio.shield(closed), timeout=1)
    except TimeoutError:
        writer.transport.abort()
        await closed
    finally:
        if not closed.done():
            # Cancellation during cleanup must also discard stalled TCP buffers.
            writer.transport.abort()
            closed.cancel()
            await asyncio.gather(closed, return_exceptions=True)


class SafeSocksProxy:
    """Serve only HTTP(S) tunnels whose every DNS answer is globally routable."""

    def __init__(
        self,
        resolver: Resolver = system_resolver,
        upstream_socks: tuple[str, int] | None = None,
    ) -> None:
        self.resolver = resolver
        self.upstream_socks = upstream_socks
        self.server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._closing = False

    async def __aenter__(self) -> Self:
        self._closing = False
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_args: object) -> None:
        self._closing = True
        if self.server is not None:
            self.server.close()
            # Let already-accepted clients enter _handle before draining their tasks.
            await asyncio.sleep(0)
            while self._handlers:
                handlers = tuple(self._handlers)
                for task in handlers:
                    task.cancel()
                await asyncio.gather(*handlers, return_exceptions=True)
            await self.server.wait_closed()

    @property
    def port(self) -> int:
        if self.server is None or not self.server.sockets:
            raise RuntimeError("SOCKS proxy has not started")
        return int(self.server.sockets[0].getsockname()[1])

    def _public_address(self, host: str, port: int) -> str:
        if port not in (80, 443):
            raise UnsafeUrlError("Only HTTP(S) ports are allowed")
        url_host = f"[{host}]" if ":" in host else host
        target = URL(f"https://{url_host}:{port}/")
        if target.host is None or target.host.casefold() != host.casefold():
            raise UnsafeUrlError("Invalid proxy destination")
        return public_addresses(target, self.resolver)[0]

    async def _open_target(
        self, address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.upstream_socks is None:
            return await asyncio.open_connection(address, port)

        reader, writer = await asyncio.open_connection(*self.upstream_socks)
        try:
            _write(writer, b"\x05\x01\x00")
            await writer.drain()
            if await reader.readexactly(2) != b"\x05\x00":
                raise OSError("Upstream SOCKS authentication unavailable")

            target = ipaddress.ip_address(address)
            kind = b"\x01" if target.version == 4 else b"\x04"
            _write(writer, b"\x05\x01\x00" + kind + target.packed + port.to_bytes(2, "big"))
            await writer.drain()
            version, status, _, address_type = await reader.readexactly(4)
            if version != 5 or status != 0:
                raise OSError("Upstream SOCKS connection refused")
            if address_type == 1:
                length = 4
            elif address_type == 4:
                length = 16
            elif address_type == 3:
                length = (await reader.readexactly(1))[0]
            else:
                raise OSError("Invalid upstream SOCKS response")
            await reader.readexactly(length + 2)
            return reader, writer
        except BaseException:
            writer.close()
            raise

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        assert handler is not None
        self._handlers.add(handler)
        upstream: asyncio.StreamWriter | None = None
        tasks: tuple[asyncio.Task[None], ...] = ()
        try:
            if self._closing:
                return
            version, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            if version != 5 or 0 not in methods:
                return
            _write(writer, b"\x05\x00")
            await writer.drain()

            version, command, _, address_type = await reader.readexactly(4)
            if version != 5 or command != 1:
                return
            if address_type == 3:
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode("ascii")
            elif address_type == 1:
                host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            elif address_type == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            else:
                return
            port = int.from_bytes(await reader.readexactly(2), "big")
            try:
                address = await asyncio.to_thread(self._public_address, host, port)
            except (UnsafeUrlError, ValueError, UnicodeError):
                _write(writer, SOCKS_DENIED)
                await writer.drain()
                return
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    self._open_target(address, port), timeout=10
                )
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                _write(writer, SOCKS_UNREACHABLE)
                await writer.drain()
                return
            upstream = upstream_writer
            _write(writer, SOCKS_OK)
            await writer.drain()
            tasks = (
                asyncio.create_task(_copy(reader, upstream_writer)),
                asyncio.create_task(_copy(upstream_reader, writer)),
            )
            await asyncio.gather(*tasks)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, UnicodeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            writer.close()
            if upstream is not None:
                upstream.close()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                for connection in (writer, upstream):
                    if connection is not None:
                        with suppress(ConnectionError, OSError):
                            await _wait_closed(connection)
            finally:
                # A second cancellation can interrupt one writer's cleanup; do not
                # leave the other transport flushing after the handler disappears.
                writer.transport.abort()
                if upstream is not None:
                    upstream.transport.abort()
                self._handlers.discard(handler)
