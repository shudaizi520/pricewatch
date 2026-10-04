"""The browser's local proxy must never connect to private destinations."""

import asyncio
import ipaddress
import socket

import pytest

from pricewatch.fetching import socks_proxy
from pricewatch.fetching.socks_proxy import SafeSocksProxy


@pytest.mark.parametrize("use_uvloop", [False, True])
def test_copy_stops_when_destination_transport_is_already_closed(use_uvloop):
    async def scenario():
        accepted = asyncio.get_running_loop().create_future()

        def accept(reader, writer):
            accepted.set_result(writer)

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        peer = None
        try:
            _, destination = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            peer = await accepted
            destination.close()
            await destination.wait_closed()
            source = asyncio.StreamReader()
            source.feed_data(b"late response")
            source.feed_eof()
            await socks_proxy._copy(source, destination)
        finally:
            if peer is not None:
                peer.close()
                await peer.wait_closed()
            server.close()
            await server.wait_closed()

    if use_uvloop:
        import uvloop

        uvloop.run(scenario())
    else:
        asyncio.run(scenario())


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio", ("asyncio", {"use_uvloop": True})])
async def test_proxy_exit_closes_client_waiting_for_handshake():
    proxy = SafeSocksProxy()
    await proxy.__aenter__()
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"\x05")  # Incomplete greeting must not keep a handler alive.
        await writer.drain()
        await asyncio.sleep(0)
        await asyncio.wait_for(proxy.__aexit__(), timeout=0.5)
        assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio", ("asyncio", {"use_uvloop": True})])
async def test_proxy_exit_drains_active_tunnel_and_both_copy_tasks(monkeypatch):
    initial_tasks = asyncio.all_tasks()
    origin_closed = asyncio.Event()

    async def origin(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            origin_closed.set()

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
    origin_port = origin_server.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection

    async def connect_pinned(address, port):
        if address == "93.184.216.34" and port == 443:
            return await original_connect("127.0.0.1", origin_port)
        return await original_connect(address, port)

    monkeypatch.setattr(socks_proxy.asyncio, "open_connection", connect_pinned)
    writer = None
    proxy = SafeSocksProxy(resolver=lambda _: ["93.184.216.34"])
    try:
        await proxy.__aenter__()
        reader, writer = await original_connect("127.0.0.1", proxy.port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        writer.write(b"\x05\x01\x00\x03\x0dstore.example\x01\xbb")
        await writer.drain()
        assert (await reader.readexactly(10))[:2] == b"\x05\x00"
        await asyncio.wait_for(proxy.__aexit__(), timeout=1)
        assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
        await asyncio.wait_for(origin_closed.wait(), timeout=0.5)
        assert not (asyncio.all_tasks() - initial_tasks)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await proxy.__aexit__()
        origin_server.close()
        await origin_server.wait_closed()


@pytest.mark.anyio
async def test_copy_does_not_suppress_unrelated_runtime_error(monkeypatch):
    accepted = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        accepted.set_result(writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    _, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    peer = await accepted

    def broken_write(_data):
        raise RuntimeError("unexpected write failure")

    monkeypatch.setattr(writer, "write", broken_write)
    source = asyncio.StreamReader()
    source.feed_data(b"response")
    source.feed_eof()
    try:
        with pytest.raises(RuntimeError, match="unexpected write failure"):
            await socks_proxy._copy(source, writer)
    finally:
        writer.close()
        peer.close()
        await writer.wait_closed()
        await peer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio", ("asyncio", {"use_uvloop": True})])
@pytest.mark.parametrize("exit_during_close", [False, True])
async def test_proxy_exit_aborts_buffered_connection_when_peer_stops_reading(
    monkeypatch, exit_during_close
):
    proxy = SafeSocksProxy()
    accepted = asyncio.get_running_loop().create_future()
    original_handle = proxy._handle
    closing_started = asyncio.Event()
    original_wait_closed = socks_proxy._wait_closed

    async def handle(reader, writer):
        accepted.set_result(writer)
        await original_handle(reader, writer)

    async def wait_closed(writer):
        closing_started.set()
        await original_wait_closed(writer)

    monkeypatch.setattr(proxy, "_handle", handle)
    monkeypatch.setattr(socks_proxy, "_wait_closed", wait_closed)
    await proxy.__aenter__()
    _, client = await asyncio.open_connection("127.0.0.1", proxy.port)
    server_writer = await accepted
    try:
        client.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        client.transport.pause_reading()
        server_writer.write(b"x" * (16 * 1024 * 1024))
        assert server_writer.transport.get_write_buffer_size() > 0
        if exit_during_close:
            client.write_eof()
            await asyncio.wait_for(closing_started.wait(), timeout=0.5)
        await asyncio.wait_for(proxy.__aexit__(), timeout=3)
        assert server_writer.transport.get_write_buffer_size() == 0
        assert not proxy._handlers
    finally:
        server_writer.transport.abort()
        client.transport.abort()
        await client.wait_closed()
        await proxy.__aexit__()


async def connect_through_proxy(proxy: SafeSocksProxy, host: str, port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            encoded = host.encode("ascii")
            destination = b"\x03" + bytes([len(encoded)]) + encoded
        else:
            destination = (b"\x01" if parsed.version == 4 else b"\x04") + parsed.packed
        writer.write(
            b"\x05\x01\x00" + destination + port.to_bytes(2, "big")
        )
        await writer.drain()
        return await reader.readexactly(10)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.anyio
async def test_socks_proxy_rejects_loopback_literal():
    async with SafeSocksProxy(resolver=lambda _: ["93.184.216.34"]) as proxy:
        reply = await connect_through_proxy(proxy, "127.0.0.1", 443)
    assert reply[:2] == b"\x05\x02"


@pytest.mark.anyio
async def test_socks_proxy_rejects_ipv6_loopback_literal():
    async with SafeSocksProxy(resolver=lambda _: ["93.184.216.34"]) as proxy:
        reply = await connect_through_proxy(proxy, "::1", 443)
    assert reply[:2] == b"\x05\x02"


@pytest.mark.anyio
async def test_socks_proxy_rejects_domain_with_any_private_dns_answer():
    async with SafeSocksProxy(resolver=lambda _: ["93.184.216.34", "192.168.50.99"]) as proxy:
        reply = await connect_through_proxy(proxy, "store.example", 443)
    assert reply[:2] == b"\x05\x02"


@pytest.mark.anyio
async def test_socks_proxy_rejects_unexpected_port():
    async with SafeSocksProxy(resolver=lambda _: ["93.184.216.34"]) as proxy:
        reply = await connect_through_proxy(proxy, "store.example", 8080)
    assert reply[:2] == b"\x05\x02"


@pytest.mark.anyio
async def test_socks_proxy_pins_public_ip_and_preserves_half_closed_response(monkeypatch):
    received = []

    async def origin(reader, writer):
        received.append(await reader.read())
        writer.write(b"response-after-eof")
        await writer.drain()
        writer.close()

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
    origin_port = origin_server.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection

    async def connect_pinned(address, port):
        if address == "93.184.216.34" and port == 443:
            return await original_connect("127.0.0.1", origin_port)
        return await original_connect(address, port)

    monkeypatch.setattr(socks_proxy.asyncio, "open_connection", connect_pinned)
    try:
        async with SafeSocksProxy(resolver=lambda _: ["93.184.216.34"]) as proxy:
            reader, writer = await original_connect("127.0.0.1", proxy.port)
            try:
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x00"
                writer.write(b"\x05\x01\x00\x03\x0dstore.example\x01\xbb")
                await writer.drain()
                assert (await reader.readexactly(10))[:2] == b"\x05\x00"
                writer.write(b"request")
                await writer.drain()
                writer.write_eof()
                assert await asyncio.wait_for(reader.read(), timeout=5) == b"response-after-eof"
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        origin_server.close()
        await origin_server.wait_closed()
    assert received == [b"request"]


@pytest.mark.anyio
async def test_socks_proxy_forwards_pinned_ip_through_configured_upstream():
    received = []

    async def upstream(reader, writer):
        try:
            received.append(await reader.readexactly(3))
            writer.write(b"\x05\x00")
            await writer.drain()
            received.append(await reader.readexactly(10))
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            received.append(await reader.readexactly(7))
            writer.write(b"response")
            await writer.drain()
        finally:
            writer.close()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    try:
        async with SafeSocksProxy(
            resolver=lambda _: ["93.184.216.34"],
            upstream_socks=("127.0.0.1", upstream_port),
        ) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x00"
                writer.write(b"\x05\x01\x00\x03\x0dstore.example\x01\xbb")
                await writer.drain()
                assert (await reader.readexactly(10))[:2] == b"\x05\x00"
                writer.write(b"request")
                await writer.drain()
                assert await reader.readexactly(8) == b"response"
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        upstream_server.close()
        await upstream_server.wait_closed()

    assert received == [
        b"\x05\x01\x00",
        b"\x05\x01\x00\x01\x5d\xb8\xd8\x22\x01\xbb",
        b"request",
    ]


@pytest.mark.anyio
async def test_socks_proxy_does_not_fall_back_to_direct_when_upstream_rejects(monkeypatch):
    async def rejecting_upstream(reader, writer):
        try:
            await reader.readexactly(3)
            writer.write(b"\x05\xff")
            await writer.drain()
        finally:
            writer.close()

    upstream_server = await asyncio.start_server(rejecting_upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection

    async def no_direct_connection(address, port):
        assert address == "127.0.0.1", "Configured proxy must not fall back to direct access"
        return await original_connect(address, port)

    monkeypatch.setattr(socks_proxy.asyncio, "open_connection", no_direct_connection)
    try:
        async with SafeSocksProxy(
            resolver=lambda _: ["93.184.216.34"],
            upstream_socks=("127.0.0.1", upstream_port),
        ) as proxy:
            reply = await connect_through_proxy(proxy, "store.example", 443)
    finally:
        upstream_server.close()
        await upstream_server.wait_closed()

    assert reply[:2] == b"\x05\x04"
