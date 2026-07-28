from __future__ import annotations

import argparse
from collections import OrderedDict
import http.client
import ipaddress
import json
import os
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 3456
DEFAULT_CHROME_DEBUG_PORT = 9223
DEFAULT_PROFILE_DIR = Path("output/chrome-cdp-profile")
REQUEST_TIMEOUT_SECONDS = 10
BROWSER_OPERATION_TIMEOUT_SECONDS = 30
MAX_EXPRESSION_BYTES = 256 * 1024
MAX_EGRESS_REQUEST_BODY_BYTES = 1024 * 1024
MAX_CDP_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_TLS_PLAINTEXT_BYTES = 16 * 1024
EGRESS_ALLOWED_PORTS = {80, 443}
EGRESS_CONNECT_ALLOWED_PORTS = {443}
EGRESS_IDLE_TIMEOUT_SECONDS = 30
ATOMIC_NAVIGATE_EVAL_CAPABILITY = "atomic_navigate_eval_v2"
PINNED_EGRESS_CAPABILITY = "pinned_public_egress_v1"
ISOLATED_BROWSER_CONTEXT_CAPABILITY = "isolated_browser_context_v1"
BRIDGE_CLIENT_HEADER = "X-Team-Agent-Browser-Proxy"
BRIDGE_CLIENT_HEADER_VALUE = "1"
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DOH_BOOTSTRAP_IPS = ("1.1.1.1", "1.0.0.1")
DOH_SERVER_NAME = "cloudflare-dns.com"
DNS_CACHE_TTL_SECONDS = 60
DNS_CACHE_MAX_ENTRIES = 256
_DNS_CACHE: OrderedDict[str, tuple[float, tuple[str, ...]]] = OrderedDict()
_DNS_CACHE_LOCK = threading.Lock()
_SENSITIVE_ENV_NAME_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


class BrowserIsolationCleanupError(RuntimeError):
    pass


class _PinnedEgressProxy:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def proxy_url(self) -> str:
        if self.server is None:
            raise RuntimeError("Pinned browser egress proxy is not running.")
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def running(self) -> bool:
        return self.server is not None and self.thread is not None and self.thread.is_alive()

    def ensure_started(self) -> None:
        with self._lock:
            if self.running:
                return
            server = _PinnedEgressHTTPServer(("127.0.0.1", 0), _egress_handler_factory())
            thread = threading.Thread(target=server.serve_forever, name="chrome-pinned-egress", daemon=True)
            thread.start()
            self.server = server
            self.thread = thread

    def close(self) -> None:
        with self._lock:
            server = self.server
            thread = self.thread
            self.server = None
            self.thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)


class _PinnedEgressHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def _egress_handler_factory() -> type[BaseHTTPRequestHandler]:
    class PinnedEgressHandler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler API.
            upstream = None
            try:
                host, port = _parse_connect_authority(self.path)
                upstream = _connect_public_host(host, port)
                self.send_response(200, "Connection Established")
                self.end_headers()
                _relay_tls_tunnel(self.connection, upstream)
            except Exception:
                if upstream is None:
                    self._send_blocked()
            finally:
                if upstream is not None:
                    upstream.close()
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API.
            self._forward_http()

        def _forward_http(self) -> None:
            connection = None
            upstream = None
            try:
                parsed = urlparse(self.path)
                if parsed.scheme != "http" or not parsed.hostname:
                    raise RuntimeError("Pinned egress only supports absolute public HTTP URLs.")
                port = parsed.port or 80
                if port != 80 or parsed.username is not None or parsed.password is not None or parsed.fragment:
                    raise RuntimeError("Pinned egress HTTP URL is not allowed.")
                if self.headers.get("Upgrade") or self.headers.get("Transfer-Encoding"):
                    raise RuntimeError("Pinned egress request mode is not allowed.")

                body_length = int(self.headers.get("Content-Length", "0") or "0")
                if body_length < 0 or body_length > MAX_EGRESS_REQUEST_BODY_BYTES:
                    raise RuntimeError("Pinned egress request body is too large.")
                body = self.rfile.read(body_length) if body_length else None
                if body is not None and len(body) != body_length:
                    raise RuntimeError("Pinned egress request body is incomplete.")

                upstream = _connect_public_host(parsed.hostname, port)
                connection = http.client.HTTPConnection(parsed.hostname, port, timeout=REQUEST_TIMEOUT_SECONDS)
                connection.sock = upstream
                upstream = None
                connection.request(
                    self.command,
                    urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                    body=body,
                    headers=_forward_request_headers(self.headers, parsed.netloc),
                )
                response = connection.getresponse()
                self.send_response(response.status, response.reason)
                for name, value in response.getheaders():
                    if name.lower() not in _HOP_BY_HOP_HEADERS:
                        self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
            except Exception:
                self._send_blocked()
            finally:
                if connection is not None:
                    connection.close()
                if upstream is not None:
                    upstream.close()
                self.close_connection = True

        def _send_blocked(self) -> None:
            if self.wfile.closed:
                return
            body = b"browser egress blocked"
            try:
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=ascii")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                return

        def log_message(self, format: str, *args: Any) -> None:
            return

    return PinnedEgressHandler


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _forward_request_headers(headers: Any, authority: str) -> dict[str, str]:
    connection_tokens = {
        token.strip().lower()
        for token in str(headers.get("Connection", "")).split(",")
        if token.strip()
    }
    forwarded = {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in _HOP_BY_HOP_HEADERS | connection_tokens | {"host"}
    }
    forwarded["Host"] = authority
    forwarded["Connection"] = "close"
    return forwarded


def _parse_connect_authority(authority: str) -> tuple[str, int]:
    parsed = urlparse(f"//{authority}")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RuntimeError("Pinned egress CONNECT authority is invalid.")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise RuntimeError("Pinned egress CONNECT port is invalid.") from exc
    if port not in EGRESS_CONNECT_ALLOWED_PORTS:
        raise RuntimeError("Pinned egress CONNECT port is not allowed.")
    return parsed.hostname, port


def _connect_public_host(host: str, port: int) -> socket.socket:
    normalized = host.rstrip(".").strip("[]").lower()
    if port not in EGRESS_ALLOWED_PORTS or _is_blocked_host(normalized):
        raise RuntimeError("Browser egress host is local or private.")
    results = _resolve_public_addresses(normalized, port)

    last_error: OSError | None = None
    for family, socktype, protocol, _canonical_name, sockaddr in results:
        upstream = socket.socket(family, socktype, protocol)
        upstream.settimeout(REQUEST_TIMEOUT_SECONDS)
        try:
            upstream.connect(sockaddr)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    raise RuntimeError("Browser egress connection failed.") from last_error


def _resolve_public_addresses(host: str, port: int) -> list[tuple[Any, ...]]:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise RuntimeError("Browser egress host could not be resolved.") from exc
    if not results:
        raise RuntimeError("Browser egress host could not be resolved.")

    addresses = [str(result[4][0]) for result in results]
    if any(_is_fake_ip_address(address) for address in addresses):
        addresses = list(_resolve_fake_ip_hostname_via_doh(host))
        results = [_address_record(address, port) for address in addresses]
    if any(_is_blocked_host(address) for address in addresses):
        raise RuntimeError("Browser egress host resolved to a local or private address.")
    return results


def _is_fake_ip_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and address in FAKE_IP_NETWORK


def _address_record(address: str, port: int) -> tuple[Any, ...]:
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        return (socket.AF_INET6, socket.SOCK_STREAM, 0, "", (address, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, port))


def _resolve_fake_ip_hostname_via_doh(host: str) -> tuple[str, ...]:
    now = time.monotonic()
    with _DNS_CACHE_LOCK:
        _prune_dns_cache_locked(now)
        cached = _DNS_CACHE.get(host)
        if cached is not None and cached[0] > now:
            _DNS_CACHE.move_to_end(host)
            return cached[1]

    addresses: list[str] = []
    for record_type, record_code in (("A", 1), ("AAAA", 28)):
        payload = _doh_query(host, record_type)
        answers = payload.get("Answer", []) if isinstance(payload, dict) else []
        for answer in answers if isinstance(answers, list) else []:
            if not isinstance(answer, dict) or answer.get("type") != record_code:
                continue
            value = str(answer.get("data", "")).strip()
            try:
                parsed = ipaddress.ip_address(value)
            except ValueError:
                continue
            addresses.append(str(parsed))
    unique = tuple(dict.fromkeys(addresses))
    if not unique or any(_is_blocked_host(address) for address in unique):
        raise RuntimeError("Browser egress secure DNS did not return public addresses.")
    with _DNS_CACHE_LOCK:
        _prune_dns_cache_locked(now)
        _DNS_CACHE[host] = (now + DNS_CACHE_TTL_SECONDS, unique)
        _DNS_CACHE.move_to_end(host)
        while len(_DNS_CACHE) > DNS_CACHE_MAX_ENTRIES:
            _DNS_CACHE.popitem(last=False)
    return unique


def _prune_dns_cache_locked(now: float) -> None:
    expired = [host for host, (expires_at, _addresses) in _DNS_CACHE.items() if expires_at <= now]
    for host in expired:
        _DNS_CACHE.pop(host, None)


def _doh_query(host: str, record_type: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for bootstrap_ip in DOH_BOOTSTRAP_IPS:
        raw_socket = None
        tls_socket = None
        connection = None
        try:
            raw_socket = _connect_numeric_address(bootstrap_ip, 443)
            context = ssl.create_default_context()
            tls_socket = context.wrap_socket(raw_socket, server_hostname=DOH_SERVER_NAME)
            raw_socket = None
            connection = http.client.HTTPSConnection(
                DOH_SERVER_NAME,
                443,
                timeout=REQUEST_TIMEOUT_SECONDS,
                context=context,
            )
            connection.sock = tls_socket
            tls_socket = None
            path = "/dns-query?" + urlencode({"name": host, "type": record_type})
            connection.request(
                "GET",
                path,
                headers={"Accept": "application/dns-json", "Host": DOH_SERVER_NAME, "Connection": "close"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("Secure DNS resolver rejected the query.")
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("Status") != 0:
                raise RuntimeError("Secure DNS resolver did not return a successful answer.")
            return payload
        except Exception as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
            if tls_socket is not None:
                tls_socket.close()
            if raw_socket is not None:
                raw_socket.close()
    raise RuntimeError("Secure DNS resolution failed.") from last_error


def _connect_numeric_address(address: str, port: int) -> socket.socket:
    record = _address_record(address, port)
    upstream = socket.socket(record[0], record[1], record[2])
    upstream.settimeout(REQUEST_TIMEOUT_SECONDS)
    try:
        upstream.connect(record[4])
        return upstream
    except OSError:
        upstream.close()
        raise


def _relay_tunnel(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, EGRESS_IDLE_TIMEOUT_SECONDS)
        if exceptional or not readable:
            return
        for source in readable:
            try:
                data = source.recv(64 * 1024)
            except OSError:
                return
            if not data:
                return
            destination = upstream if source is client else client
            destination.sendall(data)


def _relay_tls_tunnel(client: socket.socket, upstream: socket.socket) -> None:
    previous_timeout = client.gettimeout()
    client.settimeout(REQUEST_TIMEOUT_SECONDS)
    try:
        record_header = _recv_exact(client, 5)
        record_length = int.from_bytes(record_header[3:5], "big")
        if (
            record_header[0] != 0x16
            or record_header[1] != 0x03
            or not 0x01 <= record_header[2] <= 0x03
            or record_length < 4
            or record_length > MAX_TLS_PLAINTEXT_BYTES
        ):
            return
        record_payload = _recv_exact(client, record_length)
        if not _is_complete_tls_client_hello(record_payload):
            return
        upstream.sendall(record_header + record_payload)
    finally:
        client.settimeout(previous_timeout)
    _relay_tunnel(client, upstream)


def _is_complete_tls_client_hello(record_payload: bytes) -> bool:
    if len(record_payload) < 4 or record_payload[0] != 0x01:
        return False
    handshake_length = int.from_bytes(record_payload[1:4], "big")
    if handshake_length != len(record_payload) - 4:
        return False
    body = record_payload[4:]
    if len(body) < 41 or body[0] != 0x03 or not 0x01 <= body[1] <= 0x03:
        return False

    offset = 2 + 32
    session_id_length = body[offset]
    if session_id_length > 32:
        return False
    offset += 1
    if offset + session_id_length + 2 > len(body):
        return False
    offset += session_id_length

    cipher_suites_length = int.from_bytes(body[offset : offset + 2], "big")
    offset += 2
    if cipher_suites_length < 2 or cipher_suites_length % 2:
        return False
    if offset + cipher_suites_length + 1 > len(body):
        return False
    offset += cipher_suites_length

    compression_methods_length = body[offset]
    offset += 1
    if compression_methods_length < 1 or offset + compression_methods_length > len(body):
        return False
    if 0 not in body[offset : offset + compression_methods_length]:
        return False
    offset += compression_methods_length
    if offset == len(body):
        return True
    if offset + 2 > len(body):
        return False

    extensions_length = int.from_bytes(body[offset : offset + 2], "big")
    offset += 2
    if offset + extensions_length != len(body):
        return False
    extensions_end = offset + extensions_length
    seen_extension_types: set[int] = set()
    while offset < extensions_end:
        if offset + 4 > extensions_end:
            return False
        extension_type = int.from_bytes(body[offset : offset + 2], "big")
        if extension_type in seen_extension_types:
            return False
        seen_extension_types.add(extension_type)
        extension_length = int.from_bytes(body[offset + 2 : offset + 4], "big")
        offset += 4
        if offset + extension_length > extensions_end:
            return False
        offset += extension_length
    return offset == extensions_end


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise RuntimeError("Browser egress tunnel closed before TLS negotiation.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class ChromeProxyState:
    def __init__(
        self,
        *,
        chrome_path: str,
        profile_dir: Path,
        debug_port: int,
        startup_timeout: float,
        proxy_host: str = DEFAULT_PROXY_HOST,
        proxy_port: int = DEFAULT_PROXY_PORT,
    ) -> None:
        self.chrome_path = chrome_path
        self.profile_dir = profile_dir
        self.debug_port = debug_port
        self.startup_timeout = startup_timeout
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.process: subprocess.Popen[str] | None = None
        self.egress_proxy = _PinnedEgressProxy()
        self._start_lock = threading.Lock()

    @property
    def debug_base_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"

    @property
    def proxy_url(self) -> str:
        return f"http://{self.proxy_host}:{self.proxy_port}"

    def is_ready(self) -> bool:
        return (
            self.process is not None
            and self.process.poll() is None
            and self.egress_proxy.running
            and _debug_endpoint_ready(self.debug_base_url)
        )

    def ensure_started(self) -> None:
        with self._start_lock:
            if self.is_ready():
                return
            if (self.process is None or self.process.poll() is not None) and _debug_endpoint_ready(
                self.debug_base_url
            ):
                raise RuntimeError("Refusing to reuse an unmanaged Chrome DevTools endpoint.")
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("Managed Chrome is running without its pinned egress boundary.")

            self.egress_proxy.ensure_started()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            args = [
                self.chrome_path,
                f"--remote-debugging-port={self.debug_port}",
                f"--user-data-dir={self.profile_dir}",
                f"--proxy-server={self.egress_proxy.proxy_url}",
                "--proxy-bypass-list=<-loopback>",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--disable-quic",
                "--disable-features=WebTransport",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--incognito",
                "--disable-extensions",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "about:blank",
            ]
            self.process = subprocess.Popen(  # noqa: S603 - chrome_path is resolved locally or explicitly configured.
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=_sanitized_chrome_environment(),
            )
            self._wait_until_ready()

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if _debug_endpoint_ready(self.debug_base_url):
                return
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("Chrome exited before the DevTools endpoint became available.")
            time.sleep(0.2)
        raise RuntimeError("Chrome DevTools endpoint did not become available before timeout.")

    def close(self) -> None:
        with self._start_lock:
            process = self.process
            self.process = None
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            finally:
                self.egress_proxy.close()


def main() -> int:
    args = _parse_args()
    _validate_proxy_bind_host(args.host)
    chrome_path = args.chrome_path or _find_chrome_path()
    if not chrome_path:
        raise SystemExit("Google Chrome executable not found. Set TEAM_AGENT_CHROME_PATH or pass --chrome-path.")
    profile_dir = Path(args.profile_dir or os.environ.get("TEAM_AGENT_CHROME_PROFILE_DIR") or DEFAULT_PROFILE_DIR)
    state = ChromeProxyState(
        chrome_path=chrome_path,
        profile_dir=profile_dir.expanduser().resolve(),
        debug_port=args.chrome_debug_port,
        startup_timeout=args.startup_timeout,
        proxy_host=args.host,
        proxy_port=args.port,
    )
    server = None
    try:
        server = ThreadingHTTPServer((args.host, args.port), _handler_factory(state))
        state.ensure_started()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "proxy": f"http://{args.host}:{args.port}",
                    "chrome_debug_url": state.debug_base_url,
                    "profile_dir": str(state.profile_dir),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.server_close()
        state.close()
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Chrome DevTools proxy for Team Agent Harness.")
    parser.add_argument("--host", default=DEFAULT_PROXY_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--chrome-debug-port", type=int, default=DEFAULT_CHROME_DEBUG_PORT)
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--profile-dir", default="")
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    return parser.parse_args()


def _validate_proxy_bind_host(host: str) -> None:
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise RuntimeError("Chrome CDP proxy must bind to loopback.") from exc
    if not address.is_loopback:
        raise RuntimeError("Chrome CDP proxy must bind to loopback.")


def _is_expected_bridge_authority(
    authority: str,
    expected_host: str,
    expected_port: int,
) -> bool:
    try:
        parsed = urlparse(f"//{authority}")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        _validate_proxy_bind_host(parsed.hostname)
        normalized_host = parsed.hostname.rstrip(".").lower()
        normalized_expected_host = expected_host.strip("[]").rstrip(".").lower()
        return normalized_host == normalized_expected_host and parsed.port == expected_port
    except (RuntimeError, ValueError):
        return False


def _header_values(headers: Any, name: str) -> list[Any]:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        return list(get_all(name) or [])
    value = headers.get(name)
    return [] if value is None else [value]


def _single_header_value(headers: Any, name: str) -> str | None:
    values = _header_values(headers, name)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


def _handler_factory(state: ChromeProxyState) -> type[BaseHTTPRequestHandler]:
    class ChromeCdpProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
            if not self._client_is_authorized():
                self._send_json({"error": "forbidden"}, status=403)
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/health":
                    self._send_json(
                        {
                            "status": "ok",
                            "connected": state.is_ready(),
                            "proxy": state.proxy_url,
                            "capabilities": [
                                ATOMIC_NAVIGATE_EVAL_CAPABILITY,
                                PINNED_EGRESS_CAPABILITY,
                                ISOLATED_BROWSER_CONTEXT_CAPABILITY,
                            ],
                        }
                    )
                    return
                self._send_json({"error": "not_found"}, status=404)
            except Exception:
                self._send_operation_error()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            if not self._client_is_authorized():
                self._send_json({"error": "forbidden"}, status=403)
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path != "/navigate-eval":
                    self._send_json({"error": "not_found"}, status=404)
                    return
                state.ensure_started()
                query = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0 or length > MAX_EXPRESSION_BYTES:
                    raise RuntimeError("Browser expression length is invalid.")
                raw_script = self.rfile.read(length)
                if len(raw_script) != length:
                    raise RuntimeError("Browser expression body is incomplete.")
                script = raw_script.decode("utf-8")
                url = _required_query_value(query, "url")
                result = _navigate_and_evaluate(state.debug_base_url, url, script)
                self._send_json(result)
            except BrowserIsolationCleanupError:
                try:
                    state.close()
                except Exception:
                    pass
                self._send_operation_error()
            except Exception:
                self._send_operation_error()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _client_is_authorized(self) -> bool:
            bridge_header = _single_header_value(self.headers, BRIDGE_CLIENT_HEADER)
            host_header = _single_header_value(self.headers, "Host")
            return (
                bridge_header == BRIDGE_CLIENT_HEADER_VALUE
                and not _header_values(self.headers, "Origin")
                and host_header is not None
                and _is_expected_bridge_authority(
                    host_header,
                    state.proxy_host,
                    state.proxy_port,
                )
            )

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_operation_error(self) -> None:
            self._send_json(
                {
                    "error": "browser_operation_failed",
                    "message": "Browser CDP proxy request failed.",
                },
                status=400,
            )

    return ChromeCdpProxyHandler


def _evaluation_result(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("exceptionDetails"):
        raise RuntimeError("Chrome browser evaluation failed.")
    result = payload.get("result", {})
    if not isinstance(result, dict):
        return {"value": None}
    if "value" in result:
        return {"value": result["value"]}
    if "description" in result:
        return {"value": result["description"]}
    return {"value": None}


def _navigate_and_evaluate(debug_base_url: str, url: str, expression: str) -> dict[str, Any]:
    _validate_public_http_url(url)
    browser_ws = None
    context_id = None
    target_id = None
    target_ws = None
    cleanup_error: Exception | None = None
    try:
        browser_ws, context_id, target_id = _open_isolated_target(debug_base_url)
        websocket_url = _target_websocket_url(debug_base_url, target_id)
        if websocket_url is None:
            raise RuntimeError("Chrome target was not available.")
        target_ws = _connect_websocket(websocket_url)
        return _navigate_and_evaluate_target(target_ws, url, expression)
    finally:
        if browser_ws is not None and context_id is not None:
            try:
                _dispose_isolated_context(browser_ws, context_id, target_id)
            except Exception as exc:
                cleanup_error = exc
        # Closing the browser socket triggers disposeOnDetach. Keep request
        # interception connected while a fresh browser socket confirms cleanup.
        _close_websocket(browser_ws)
        if cleanup_error is not None and context_id is not None:
            try:
                _recover_or_confirm_isolated_context(debug_base_url, context_id, target_id)
            except Exception as exc:
                cleanup_error = exc
            else:
                cleanup_error = None
        _close_websocket(target_ws)
        if cleanup_error is not None:
            raise BrowserIsolationCleanupError("Chrome isolated browser context cleanup failed.") from cleanup_error


def _open_isolated_target(debug_base_url: str) -> tuple[Any, str, str]:
    browser_ws = _connect_websocket(_browser_websocket_url(debug_base_url))
    context_id: str | None = None
    target_id: str | None = None
    context_creation_requested = False
    try:
        context_creation_requested = True
        _send_socket_command(
            browser_ws,
            1,
            "Target.createBrowserContext",
            {"disposeOnDetach": True},
        )
        context_result = _wait_for_command_response(browser_ws, 1)
        context_id = context_result.get("browserContextId")
        if not isinstance(context_id, str) or not context_id:
            raise RuntimeError("Chrome did not create an isolated browser context.")

        _send_socket_command(
            browser_ws,
            2,
            "Browser.setDownloadBehavior",
            {"behavior": "deny", "browserContextId": context_id},
        )
        _wait_for_command_response(browser_ws, 2)

        _send_socket_command(
            browser_ws,
            3,
            "Target.createTarget",
            {"url": "about:blank", "browserContextId": context_id},
        )
        target_result = _wait_for_command_response(browser_ws, 3)
        target_id = target_result.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise RuntimeError("Chrome did not create an isolated browser target.")
        return browser_ws, context_id, target_id
    except Exception as operation_error:
        _close_websocket(browser_ws)
        if context_creation_requested and context_id is None:
            raise BrowserIsolationCleanupError(
                "Chrome isolated browser context ownership could not be confirmed."
            ) from operation_error
        if context_id is not None:
            try:
                _recover_or_confirm_isolated_context(debug_base_url, context_id, target_id)
            except Exception as cleanup_error:
                raise BrowserIsolationCleanupError(
                    "Chrome isolated browser context cleanup failed during target creation."
                ) from cleanup_error
        if isinstance(operation_error, BrowserIsolationCleanupError):
            raise
        raise


def _dispose_isolated_context(browser_ws: Any, context_id: str, target_id: str) -> None:
    _send_socket_command(
        browser_ws,
        4,
        "Target.disposeBrowserContext",
        {"browserContextId": context_id},
    )
    _wait_for_command_response(browser_ws, 4)
    _confirm_isolated_context_absent(
        browser_ws,
        context_id,
        target_id,
        first_command_id=5,
    )


def _confirm_isolated_context_absent(
    browser_ws: Any,
    context_id: str,
    target_id: str | None,
    *,
    first_command_id: int,
) -> None:
    _send_socket_command(browser_ws, first_command_id, "Target.getBrowserContexts", {})
    contexts_result = _wait_for_command_response(browser_ws, first_command_id)
    context_ids = contexts_result.get("browserContextIds")
    _send_socket_command(browser_ws, first_command_id + 1, "Target.getTargets", {})
    targets_result = _wait_for_command_response(browser_ws, first_command_id + 1)
    target_infos = targets_result.get("targetInfos")
    if not isinstance(context_ids, list) or any(
        not isinstance(value, str) or not value for value in context_ids
    ):
        raise RuntimeError("Chrome isolated browser context cleanup could not be confirmed.")
    if not isinstance(target_infos, list) or any(
        not isinstance(info, dict)
        or not isinstance(info.get("targetId"), str)
        or not info.get("targetId")
        or (
            "browserContextId" in info
            and (
                not isinstance(info.get("browserContextId"), str)
                or not info.get("browserContextId")
            )
        )
        for info in target_infos
    ):
        raise RuntimeError("Chrome isolated browser context cleanup could not be confirmed.")
    if (
        context_id in context_ids
        or any(info.get("browserContextId") == context_id for info in target_infos)
        or (
            target_id is not None
            and any(info.get("targetId") == target_id for info in target_infos)
        )
    ):
        raise RuntimeError("Chrome isolated browser context remained active after disposal.")


def _recover_or_confirm_isolated_context(
    debug_base_url: str,
    context_id: str,
    target_id: str | None,
) -> None:
    recovery_ws = _connect_websocket(_browser_websocket_url(debug_base_url))
    try:
        try:
            _confirm_isolated_context_absent(
                recovery_ws,
                context_id,
                target_id,
                first_command_id=1,
            )
            return
        except RuntimeError:
            pass
        _send_socket_command(
            recovery_ws,
            3,
            "Target.disposeBrowserContext",
            {"browserContextId": context_id},
        )
        _wait_for_command_response(recovery_ws, 3)
        _confirm_isolated_context_absent(
            recovery_ws,
            context_id,
            target_id,
            first_command_id=4,
        )
    finally:
        _close_websocket(recovery_ws)


def _close_websocket(ws: Any | None) -> None:
    if ws is None:
        return
    try:
        ws.close()
    except Exception:
        return


def _navigate_and_evaluate_target(
    ws: Any,
    url: str,
    expression: str,
) -> dict[str, Any]:
    _validate_public_http_url(url)
    _send_socket_command(ws, 1, "Page.enable", {})
    _wait_for_command_response(ws, 1)
    _send_socket_command(ws, 2, "Page.setLifecycleEventsEnabled", {"enabled": True})
    _wait_for_command_response(ws, 2)
    _send_socket_command(
        ws,
        3,
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
    )
    _wait_for_command_response(ws, 3)
    _send_socket_command(ws, 4, "Page.navigate", {"url": url})

    deadline = time.monotonic() + BROWSER_OPERATION_TIMEOUT_SECONDS
    navigation_frame_id: str | None = None
    navigation_loader_id: str | None = None
    frame_loader_ids: dict[str, str] = {}
    loaded_loader_ids: set[str] = set()
    evaluation_command_id: int | None = None
    evaluation_loader_id: str | None = None
    next_command_id = 5
    while time.monotonic() < deadline:
        timeout = max(0.01, deadline - time.monotonic())
        payload = json.loads(ws.recv(timeout=timeout))
        if payload.get("id") == 4:
            if "error" in payload:
                raise RuntimeError("Chrome rejected browser navigation.")
            result = payload.get("result", {})
            if not isinstance(result, dict) or result.get("errorText"):
                raise RuntimeError("Chrome browser navigation failed.")
            loader_id = result.get("loaderId")
            if not isinstance(loader_id, str) or not loader_id:
                raise RuntimeError("Chrome navigation did not identify its document loader.")
            frame_id = result.get("frameId")
            if not isinstance(frame_id, str) or not frame_id:
                raise RuntimeError("Chrome navigation did not identify its main frame.")
            navigation_frame_id = frame_id
            navigation_loader_id = frame_loader_ids.get(frame_id, loader_id)
        elif payload.get("method") == "Page.frameStartedNavigating":
            params = payload.get("params", {})
            if isinstance(params, dict):
                frame_id = params.get("frameId")
                loader_id = params.get("loaderId")
                if frame_id == navigation_frame_id and evaluation_command_id is not None and (
                    not isinstance(loader_id, str) or loader_id != evaluation_loader_id
                ):
                    raise RuntimeError("Chrome main frame navigated during browser evaluation.")
                if isinstance(frame_id, str) and isinstance(loader_id, str) and loader_id:
                    frame_loader_ids[frame_id] = loader_id
                    if frame_id == navigation_frame_id:
                        navigation_loader_id = loader_id
        elif payload.get("method") == "Page.frameNavigated":
            params = payload.get("params", {})
            frame = params.get("frame", {}) if isinstance(params, dict) else {}
            if isinstance(frame, dict):
                frame_id = frame.get("id")
                loader_id = frame.get("loaderId")
                if frame_id == navigation_frame_id and evaluation_command_id is not None and (
                    not isinstance(loader_id, str) or loader_id != evaluation_loader_id
                ):
                    raise RuntimeError("Chrome main frame navigated during browser evaluation.")
                if isinstance(frame_id, str) and isinstance(loader_id, str) and loader_id:
                    frame_loader_ids[frame_id] = loader_id
                    if frame_id == navigation_frame_id:
                        navigation_loader_id = loader_id
        elif payload.get("method") == "Page.lifecycleEvent":
            params = payload.get("params", {})
            if isinstance(params, dict) and params.get("name") == "load":
                loader_id = params.get("loaderId")
                if (
                    params.get("frameId") == navigation_frame_id
                    and evaluation_command_id is not None
                    and loader_id != evaluation_loader_id
                ):
                    raise RuntimeError("Chrome main frame navigated during browser evaluation.")
                if isinstance(loader_id, str) and loader_id:
                    loaded_loader_ids.add(loader_id)
        elif payload.get("method") == "Fetch.requestPaused":
            next_command_id = _handle_paused_request(ws, payload, next_command_id)
        elif evaluation_command_id is not None and payload.get("id") == evaluation_command_id:
            if navigation_loader_id != evaluation_loader_id:
                raise RuntimeError("Chrome main frame changed during browser evaluation.")
            if "error" in payload:
                raise RuntimeError("Chrome rejected browser evaluation.")
            result = payload.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError("Chrome browser evaluation returned an invalid response.")
            return _evaluation_result(result)

        if (
            navigation_loader_id is not None
            and navigation_loader_id in loaded_loader_ids
            and evaluation_command_id is None
        ):
            evaluation_command_id = next_command_id
            evaluation_loader_id = navigation_loader_id
            next_command_id += 1
            _send_socket_command(
                ws,
                evaluation_command_id,
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
    raise RuntimeError("Chrome browser operation did not finish before timeout.")


def _handle_paused_request(ws: Any, payload: dict[str, Any], command_id: int) -> int:
    params = payload.get("params", {})
    request = params.get("request", {}) if isinstance(params, dict) else {}
    request_url = str(request.get("url", "")) if isinstance(request, dict) else ""
    request_id = str(params.get("requestId", "")) if isinstance(params, dict) else ""
    try:
        _validate_public_http_url(request_url)
    except RuntimeError:
        _send_socket_command(
            ws,
            command_id,
            "Fetch.failRequest",
            {"requestId": request_id, "errorReason": "BlockedByClient"},
        )
        raise RuntimeError("Chrome navigation attempted a blocked local or private request.")
    _send_socket_command(ws, command_id, "Fetch.continueRequest", {"requestId": request_id})
    return command_id + 1


def _connect_websocket(websocket_url: str):
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise RuntimeError("Chrome CDP navigation requires the websockets package.") from exc
    return connect(
        websocket_url,
        open_timeout=REQUEST_TIMEOUT_SECONDS,
        close_timeout=REQUEST_TIMEOUT_SECONDS,
        max_size=MAX_CDP_WEBSOCKET_MESSAGE_BYTES,
        proxy=None,
    )


def _send_socket_command(ws: Any, command_id: int, method: str, params: dict[str, Any]) -> None:
    ws.send(json.dumps({"id": command_id, "method": method, "params": params}))


def _wait_for_command_response(ws: Any, command_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        payload = json.loads(ws.recv(timeout=max(0.01, deadline - time.monotonic())))
        if payload.get("id") != command_id:
            continue
        if "error" in payload:
            raise RuntimeError("Chrome rejected a DevTools command.")
        result = payload.get("result", {})
        return result if isinstance(result, dict) else {}
    raise RuntimeError("Chrome DevTools command did not finish before timeout.")


def _validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("Browser navigation requires a public http(s) URL.")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise RuntimeError("Browser navigation URL must not contain credentials or a fragment.")
    host = parsed.hostname.rstrip(".").lower()
    if _is_blocked_host(host):
        raise RuntimeError("Browser navigation URL host is local or private.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RuntimeError("Browser navigation URL port is invalid.") from exc
    if port not in EGRESS_ALLOWED_PORTS:
        raise RuntimeError("Browser navigation URL port is not allowed.")
    try:
        _resolve_public_addresses(host, port)
    except RuntimeError as exc:
        raise RuntimeError("Browser navigation URL host could not be resolved.") from exc


def _is_blocked_host(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return _is_blocked_address(address)


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        return True
    if not isinstance(address, ipaddress.IPv6Address):
        return False

    embedded = [address.ipv4_mapped, address.sixtofour]
    if address.teredo is not None:
        embedded.extend(address.teredo)
    return any(value is not None and _is_blocked_address(value) for value in embedded)


def _target_websocket_url(debug_base_url: str, target_id: str) -> str | None:
    targets = _debug_json(debug_base_url, "/json/list")
    if not isinstance(targets, list):
        return None
    for target in targets:
        if not isinstance(target, dict):
            continue
        if str(target.get("id", "")) == target_id:
            websocket_url = target.get("webSocketDebuggerUrl")
            if not isinstance(websocket_url, str) or not websocket_url:
                return None
            parsed = urlparse(websocket_url)
            debug_url = urlparse(debug_base_url)
            try:
                _validate_proxy_bind_host(parsed.hostname or "")
                _validate_proxy_bind_host(debug_url.hostname or "")
            except RuntimeError as exc:
                raise RuntimeError("Chrome target DevTools websocket was not local.") from exc
            if (
                parsed.scheme != "ws"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.hostname != debug_url.hostname
                or parsed.port != debug_url.port
                or parsed.path != f"/devtools/page/{target_id}"
                or parsed.query
                or parsed.fragment
            ):
                raise RuntimeError("Chrome target DevTools websocket was invalid.")
            return websocket_url
    return None


def _browser_websocket_url(debug_base_url: str) -> str:
    payload = _debug_json(debug_base_url, "/json/version")
    value = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError("Chrome browser DevTools websocket was not available.")
    parsed = urlparse(value)
    debug_url = urlparse(debug_base_url)
    try:
        _validate_proxy_bind_host(parsed.hostname or "")
        _validate_proxy_bind_host(debug_url.hostname or "")
    except RuntimeError as exc:
        raise RuntimeError("Chrome browser DevTools websocket was not local.") from exc
    browser_path_prefix = "/devtools/browser/"
    if (
        parsed.scheme != "ws"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname != debug_url.hostname
        or parsed.port != debug_url.port
        or not parsed.path.startswith(browser_path_prefix)
        or parsed.path == browser_path_prefix
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Chrome browser DevTools websocket was invalid.")
    return value


def _sanitized_chrome_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.lower() for marker in _SENSITIVE_ENV_NAME_MARKERS)
    }


def _find_chrome_path() -> str:
    candidates = [
        os.environ.get("TEAM_AGENT_CHROME_PATH", ""),
        os.environ.get("CHROME_PATH", ""),
        shutil.which("chrome") or "",
        shutil.which("chrome.exe") or "",
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        str(Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("LocalAppData", "")) / "Google/Chrome/Application/chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return ""


def _debug_endpoint_ready(debug_base_url: str) -> bool:
    try:
        data = _debug_json(debug_base_url, "/json/version")
    except Exception:
        return False
    browser = str(data.get("Browser", "")) if isinstance(data, dict) else ""
    return browser.startswith("Chrome/")


def _debug_json(debug_base_url: str, path: str, *, method: str = "GET") -> Any:
    return json.loads(_debug_text(debug_base_url, path, method=method))


def _debug_text(debug_base_url: str, path: str, *, method: str = "GET") -> str:
    request = Request(debug_base_url.rstrip("/") + path, method=method)
    try:
        with _open_local_url(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read(1024 * 1024).decode("utf-8", errors="replace")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Chrome DevTools request failed: {path}") from exc


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        return None


def _open_local_url(request: Request, *, timeout: float):
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=timeout)


def _single_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return values[0] if values else None


def _required_query_value(query: dict[str, list[str]], key: str) -> str:
    value = _single_query_value(query, key)
    if not value:
        raise RuntimeError(f"Missing query parameter: {key}")
    return value


if __name__ == "__main__":
    sys.exit(main())
