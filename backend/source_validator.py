import http.client
import ipaddress
import re
import socket
import subprocess
import ssl
from urllib.parse import quote, urljoin, urlsplit


TIMEOUT = 5
MAX_REDIRECTS = 5
USER_AGENT = "NEX-AI-Source-Validator/1.0"


def extract_urls(text):
    """Extract unique HTTP/HTTPS URLs from text or Markdown."""
    if not isinstance(text, str):
        return []

    urls = re.findall(r'https?://[^\s<>"\']+', text, flags=re.IGNORECASE)
    cleaned = []

    for url in urls:
        url = url.split("](", 1)[0]
        url = url.rstrip(".,;:!?)]}")
        if url not in cleaned:
            cleaned.append(url)

    return cleaned


def _resolve_public_address(hostname, port):
    """Resolve with a bounded lookup, reject non-public IPs, and pin an address."""
    try:
        completed = subprocess.run(
            ["getent", "ahosts", hostname],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None

    if completed.returncode != 0:
        return None

    addresses = []
    seen = set()

    for line in completed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue

        try:
            ip = ipaddress.ip_address(fields[0].split("%")[0])
        except ValueError:
            return None

        if not ip.is_global:
            return None

        if ip.version == 4:
            address = (
                socket.AF_INET, socket.SOCK_STREAM,
                socket.IPPROTO_TCP, "", (str(ip), port)
            )
        else:
            address = (
                socket.AF_INET6, socket.SOCK_STREAM,
                socket.IPPROTO_TCP, "", (str(ip), port, 0, 0)
            )

        key = (address[0], address[4])
        if key not in seen:
            seen.add(key)
            addresses.append(address)

    return addresses[0] if addresses else None


def _request_once(parsed, address, port):
    """Send one HTTP request to a previously validated, pinned IP."""
    family, socktype, proto, _, sockaddr = address
    connection = socket.socket(family, socktype, proto)

    try:
        connection.settimeout(TIMEOUT)
        connection.connect(sockaddr)

        if parsed.scheme == "https":
            context = ssl.create_default_context()
            connection = context.wrap_socket(
                connection, server_hostname=parsed.hostname
            )

        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        default_port = 443 if parsed.scheme == "https" else 80
        if port != default_port:
            host = f"{host}:{port}"

        path = quote(
            parsed.path or "/",
            safe="/%:@!$&'()*+,;=-._~"
        )
        if parsed.query:
            path += "?" + quote(
                parsed.query,
                safe="/?%:@!$&'()*+,;=-._~"
            )

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        )
        connection.sendall(request.encode("ascii"))

        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, response.getheader("Location")
    finally:
        connection.close()


def validate_url(url):
    """Check public URL accessibility without following unchecked redirects."""
    if isinstance(url, str):
        candidate = url.strip()
        if candidate.startswith("[") and "](" in candidate and candidate.endswith(")"):
            destination = candidate.rsplit("](", 1)[1][:-1].strip()
            if destination.lower().startswith(("http://", "https://")):
                url = destination

    result = {
        "url": url,
        "status": "invalid_url",
        "http_status": None,
        "final_url": None,
        "message": "",
        "claim_verified": False,
    }

    try:
        current_url = url

        for redirect_count in range(MAX_REDIRECTS + 1):
            parsed = urlsplit(current_url)

            if parsed.scheme not in ("http", "https"):
                result["message"] = "Only HTTP and HTTPS URLs are allowed."
                return result

            if not parsed.hostname or parsed.username or parsed.password:
                result["message"] = "Invalid or credential-bearing URL."
                return result

            port = parsed.port
            expected_port = 443 if parsed.scheme == "https" else 80
            port = port or expected_port

            if port != expected_port:
                result["message"] = "Only standard HTTP/HTTPS ports are allowed."
                return result

            address = _resolve_public_address(parsed.hostname, port)
            if address is None:
                result["message"] = (
                    "Host is not publicly accessible or could not be verified."
                )
                return result

            status, location = _request_once(parsed, address, port)
            result["http_status"] = status
            result["final_url"] = current_url

            if status in (301, 302, 303, 307, 308):
                if not location:
                    result["status"] = "inaccessible"
                    result["message"] = "Redirect has no Location header."
                    return result

                if redirect_count >= MAX_REDIRECTS:
                    result["status"] = "inaccessible"
                    result["message"] = "Too many redirects."
                    return result

                current_url = urljoin(current_url, location)
                continue

            if 200 <= status < 300:
                result["status"] = "accessible"
                result["message"] = (
                    "URL responded successfully; source claims are not verified."
                )
            else:
                result["status"] = "inaccessible"
                result["message"] = f"HTTP response status {status}."

            return result

        result["status"] = "inaccessible"
        result["message"] = "Too many redirects."

    except ValueError as exc:
        result["status"] = "invalid_url"
        result["message"] = f"Invalid URL: {type(exc).__name__}."
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        result["status"] = "inaccessible"
        result["message"] = f"Request failed: {type(exc).__name__}."

    return result


def validate_sources(text):
    """Validate extracted URLs without claiming factual verification."""
    urls = extract_urls(text)

    if not urls:
        return {
            "total_urls": 0,
            "status": "missing",
            "claim_verification": "not_performed",
            "sources": [],
        }

    sources = [validate_url(url) for url in urls]

    return {
        "total_urls": len(sources),
        "status": "checked",
        "claim_verification": "not_performed",
        "sources": sources,
    }
