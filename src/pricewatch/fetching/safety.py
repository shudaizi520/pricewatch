"""Validate public product URLs before connecting to retailer sites."""

import ipaddress
import socket
from collections.abc import Callable, Iterable

from httpx import URL

Resolver = Callable[[str], Iterable[str]]


class UnsafeUrlError(ValueError):
    """A URL resolves outside the public internet."""


def system_resolver(host: str) -> list[str]:
    return sorted(
        {str(answer[4][0]) for answer in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    )


def is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global


def public_addresses(url: URL, resolver: Resolver = system_resolver) -> tuple[str, ...]:
    if url.scheme not in ("http", "https") or not url.host or url.userinfo:
        raise UnsafeUrlError("Only public HTTP(S) URLs without embedded credentials are allowed")
    host = url.host.strip("[]")
    try:
        addresses: tuple[str, ...] = (str(ipaddress.ip_address(host)),)
    except ValueError:
        try:
            addresses = tuple(resolver(host))
        except OSError as error:
            raise UnsafeUrlError("The product hostname could not be resolved") from error
    if not addresses:
        raise UnsafeUrlError("The product hostname has no public address")
    try:
        parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
    except ValueError as error:
        raise UnsafeUrlError("The product hostname returned an invalid address") from error
    if any(is_forbidden_ip(address) for address in parsed_addresses):
        raise UnsafeUrlError("The product URL resolves to a non-public address")
    return addresses


def validate_public_url(url: str, resolver: Resolver = system_resolver) -> URL:
    try:
        parsed = URL(url)
    except (ValueError, TypeError) as error:
        raise UnsafeUrlError("Invalid product URL") from error
    public_addresses(parsed, resolver)
    return parsed
