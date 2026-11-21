"""Fail-closed URL and DNS policy for isolated browser sessions."""

import ipaddress
import os
import re
import socket
from urllib.parse import parse_qsl, urlsplit, urlunsplit


NON_PUBLIC_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
NON_PUBLIC_IPV6_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "::/96",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:4860:64::/96",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
    )
)


def configured_browser_domains() -> tuple[str, ...]:
    raw = os.getenv("NEX_BROWSER_ALLOWED_DOMAINS", "")
    domains = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        domain = normalize_domain(value)
        if domain not in domains:
            domains.append(domain)
    if not domains:
        raise ValueError("NEX_BROWSER_ALLOWED_DOMAINS must contain an approved domain")
    return tuple(domains)


def normalize_domain(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("Browser allowlist domain is invalid")
    value = value.rstrip(".").lower()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("Browser allowlist must contain hostnames, not IP addresses")
    try:
        domain = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Browser allowlist domain is invalid") from exc
    labels = domain.split(".")
    if (
        len(labels) < 2
        or any(not label or len(label) > 63 for label in labels)
        or any(not label[0].isalnum() or not label[-1].isalnum() for label in labels)
        or any(not all(character.isalnum() or character == "-" for character in label) for label in labels)
    ):
        raise ValueError("Browser allowlist domain is invalid")
    return domain


def _is_allowed_host(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain for domain in domains)


def _is_sensitive_query_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized in {"key", "code"} or any(
        marker in normalized
        for marker in (
            "password",
            "passwd",
            "token",
            "secret",
            "apikey",
            "authorization",
            "credential",
            "sessionid",
            "sessionkey",
        )
    )


def _is_non_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in NON_PUBLIC_IPV4_NETWORKS)
    if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
        return True
    return any(address in network for network in NON_PUBLIC_IPV6_NETWORKS)


def _resolve_public_addresses(host: str, resolver=socket.getaddrinfo) -> tuple[str, ...]:
    try:
        results = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Browser destination could not be resolved") from exc
    addresses = set()
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0].split("%", 1)[0])
        except (IndexError, ValueError, TypeError) as exc:
            raise ValueError("Browser destination returned an invalid address") from exc
        if _is_non_public_address(address):
            raise ValueError("Browser destination resolves to a restricted network")
        addresses.add(address.compressed)
    if not addresses:
        raise ValueError("Browser destination has no usable address")
    return tuple(sorted(addresses))


def validate_browser_url(
    url: str,
    allowed_domains: tuple[str, ...],
    resolver=socket.getaddrinfo,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise ValueError("Browser URL is invalid")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Browser URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("Only credential-free HTTPS URLs on port 443 are allowed")
    if any(
        _is_sensitive_query_name(name)
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError("Browser URLs must not contain credentials or session tokens")
    try:
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Browser URL hostname is invalid") from exc
    if not _is_allowed_host(host, allowed_domains):
        raise ValueError("Browser destination is not in the domain allowlist")
    addresses = _resolve_public_addresses(host, resolver=resolver)
    normalized = urlunsplit(
        ("https", host, parsed.path or "/", parsed.query, "")
    )
    return normalized, addresses


def validate_browser_websocket_url(
    url: str,
    allowed_domains: tuple[str, ...],
    resolver=socket.getaddrinfo,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise ValueError("Only secure WebSocket destinations are allowed")
    normalized, addresses = validate_browser_url(
        "https://" + url[len("wss://"):],
        allowed_domains,
        resolver=resolver,
    )
    return "wss://" + normalized[len("https://"):], addresses


def build_host_resolver_rules(
    allowed_domains: tuple[str, ...],
    resolver=socket.getaddrinfo,
) -> str:
    if not allowed_domains:
        raise ValueError("Browser domain allowlist is empty")
    rules = []
    for value in allowed_domains:
        domain = normalize_domain(value)
        addresses = _resolve_public_addresses(domain, resolver=resolver)
        rules.append(f"MAP {domain} {addresses[0]}")
    rules.append("MAP * ~NOTFOUND")
    return ",".join(rules)
