"""The browser's local proxy must never connect to private destinations."""

import asyncio
import ipaddress

import pytest

from pricewatch.fetching import socks_proxy
from pricewatch.fetching.socks_proxy import SafeSocksProxy


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
