"""SSRF-safe HTTP GET (F3).

``safe_get(url)`` downloads a URL supplied by an untrusted user (e.g. an image address in a
vendor bulk import) without letting it reach the internal network:

* only ``http`` / ``https``; no ``user@host`` userinfo; only the default ports (80 / 443)
  so the tool cannot be used to probe internal services on odd ports; URL <= 2048 chars,
  no control characters or whitespace;
* the host is resolved exactly ONCE with ``socket.getaddrinfo``; EVERY returned address must
  be public (not private, loopback, link-local, multicast, reserved, unspecified, not an
  IPv4-mapped / 6to4 / NAT64 wrapper of such an address). IP-literal hosts, including
  decimal / hex / octal spellings, are handled because the check is done on the resolved
  address list, never on the text of the URL;
* the connection is opened to that resolved IP (so a second DNS answer can never be used:
  no rebinding window). The ``Host`` header carries the original host name; for https the
  TLS SNI and the certificate verification use the original host name
  (``server_hostname`` / ``assert_hostname``, ``CERT_REQUIRED``);
* redirects are NOT followed (a 3xx is an error), so no redirect target has to be validated;
* the body is streamed with a hard byte limit and one overall deadline (``timeout`` seconds
  for connect + headers + body), not just per socket read.

Failures raise ``SafeFetchError`` whose ``code`` lets the caller build a translated message.
"""
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import urllib3

DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_URL_LENGTH = 2048
USER_AGENT = "LeadersMarketplaceImport/1.0"
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class SafeFetchError(Exception):
    """``code``: scheme, url, resolve, blocked, redirect, status, too_large, timeout, network."""

    def __init__(self, code, detail=None):
        super().__init__("%s%s" % (code, ": %s" % detail if detail else ""))
        self.code = code
        self.detail = detail


def _is_public_address(address):
    """True only for a globally routable unicast address."""
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return _is_public_address(address.ipv4_mapped)
        if address.sixtofour is not None and not _is_public_address(address.sixtofour):
            return False
        if address.teredo is not None:
            return False
        if address in _NAT64:
            return False
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
    ) and address.is_global


def resolve_public(hostname, port):
    """Resolve once; return the first address (str) when ALL answers are public."""
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise SafeFetchError("resolve", hostname)
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(str(info[4][0]).split("%")[0]))
        except ValueError:
            raise SafeFetchError("blocked")
    if not addresses:
        raise SafeFetchError("resolve", hostname)
    if not all(_is_public_address(a) for a in addresses):
        raise SafeFetchError("blocked")
    return str(addresses[0])


def parse_url(url, allowed_schemes=("http", "https")):
    """``(scheme, hostname, port, request_target)`` or SafeFetchError."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise SafeFetchError("url")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise SafeFetchError("url")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in allowed_schemes or scheme not in DEFAULT_PORTS:
            raise SafeFetchError("scheme")
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            raise SafeFetchError("url")
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise SafeFetchError("url")
    if not hostname:
        raise SafeFetchError("url")
    if port is not None and port != DEFAULT_PORTS[scheme]:
        raise SafeFetchError("url", "port")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise SafeFetchError("url")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return scheme, hostname, DEFAULT_PORTS[scheme], target


def _request(scheme, ip, port, hostname, target, timeout):
    """Open a connection to ``ip`` and send the GET (single seam, patched in tests)."""
    host_header = "[%s]" % hostname if ":" in hostname else hostname
    timeout_cfg = urllib3.Timeout(connect=timeout, read=timeout)
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            ip, port=port, timeout=timeout_cfg, retries=False, cert_reqs="CERT_REQUIRED",
            server_hostname=hostname, assert_hostname=hostname)
    else:
        pool = urllib3.HTTPConnectionPool(ip, port=port, timeout=timeout_cfg, retries=False)
    return pool.urlopen(
        "GET", target, headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5",
                                "Accept-Encoding": "identity"},
        redirect=False, retries=False, preload_content=False, decode_content=False)


def safe_get(url, timeout=10, max_bytes=5 * 1024 * 1024, allowed_schemes=("http", "https")):
    """Return the body (bytes) of ``url`` or raise SafeFetchError. See the module docstring."""
    deadline = time.monotonic() + timeout
    scheme, hostname, port, target = parse_url(url, allowed_schemes)
    ip = resolve_public(hostname, port)
    response = None
    try:
        try:
            response = _request(scheme, ip, port, hostname, target, timeout)
        except (urllib3.exceptions.TimeoutError, socket.timeout, TimeoutError):
            raise SafeFetchError("timeout")
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise SafeFetchError("network", exc.__class__.__name__)
        status = response.status
        if 300 <= status < 400:
            raise SafeFetchError("redirect", status)
        if status != 200:
            raise SafeFetchError("status", status)
        length = str(response.headers.get("Content-Length") or "")
        if length.isdigit() and int(length) > max_bytes:
            raise SafeFetchError("too_large")
        chunks, size = [], 0
        try:
            for chunk in response.stream(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise SafeFetchError("too_large")
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise SafeFetchError("timeout")
        except (urllib3.exceptions.TimeoutError, socket.timeout, TimeoutError):
            raise SafeFetchError("timeout")
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise SafeFetchError("network", exc.__class__.__name__)
        return b"".join(chunks)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - never mask the real result
                pass
