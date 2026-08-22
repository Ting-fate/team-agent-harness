from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import http.client
from ipaddress import IPv4Address, IPv6Address, ip_address
import json
import os
import socket
import ssl
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse, urlunparse

from app.core.models import HarnessModel


ALLOW_REAL_WEB_SEARCH_ENV = "TEAM_AGENT_ALLOW_REAL_WEB_SEARCH"
WEB_SEARCH_PROVIDER_ENV = "TEAM_AGENT_WEB_SEARCH_PROVIDER"
TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
DEFAULT_WEB_SEARCH_MAX_RESULTS = 5
DEFAULT_WEB_FETCH_MAX_BYTES = 64 * 1024
MAX_PUBLIC_URL_CHARS = 2_048
WEB_REQUEST_TIMEOUT_SECONDS = 20.0
WEB_READ_CHUNK_BYTES = 16 * 1024
MAX_TAVILY_RESPONSE_BYTES = 512 * 1024
MAX_TAVILY_JSON_DEPTH = 12
MAX_TAVILY_JSON_ITEMS = 2_048


class WebSearchClient(Protocol):
    def search(self, *, query: str, max_results: int) -> dict[str, Any]:
        ...


class WebFetchClient(Protocol):
    def fetch(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        ...


class ToolProviderInfo(HarnessModel):
    name: str
    provider: str
    adapter: str
    enabled: bool
    real_calls: bool
    real_calls_configured: bool
    requires_credentials: bool
    description: str


@dataclass
class WebToolProvider:
    search_client: WebSearchClient | None = None
    fetch_client: WebFetchClient | None = None

    @property
    def provider_name(self) -> str:
        return os.environ.get(WEB_SEARCH_PROVIDER_ENV, "mock").strip().lower() or "mock"

    @property
    def real_calls_enabled(self) -> bool:
        return os.environ.get(ALLOW_REAL_WEB_SEARCH_ENV) == "1"

    def real_search_access_available(self) -> bool:
        return (
            self.real_calls_enabled
            and self.provider_name == "tavily"
            and bool(os.environ.get(TAVILY_API_KEY_ENV))
        )

    def real_fetch_access_available(self) -> bool:
        return self.real_search_access_available()

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = _validate_query(payload.get("query"))
        max_results = _validate_max_results(payload.get("max_results", DEFAULT_WEB_SEARCH_MAX_RESULTS))
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return _mock_search(query, max_results, provider)
        if provider != "tavily":
            raise _tool_validation_error(f"Unsupported web search provider: {provider}")
        _require_tavily_api_key()
        started = perf_counter()
        client = self.search_client or TavilySearchClient()
        try:
            raw = client.search(query=query, max_results=max_results)
        except Exception as exc:
            raise _tool_validation_error("web_search provider call failed. See server logs for provider details.") from exc
        if not isinstance(raw, dict):
            raise _tool_validation_error("web_search provider returned an invalid response.")
        return {
            "provider": provider,
            "mocked": False,
            "query_hash": _hash(query),
            "latency_ms": _elapsed_ms(started),
            "results": _normalize_search_results(raw, max_results),
        }

    def fetch_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = _validate_public_http_url(payload.get("url"))
        max_bytes = _validate_max_bytes(payload.get("max_bytes", DEFAULT_WEB_FETCH_MAX_BYTES))
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return _mock_fetch(url, provider)
        if provider != "tavily":
            raise _tool_validation_error(f"Unsupported web fetch provider: {provider}")
        _require_tavily_api_key()
        started = perf_counter()
        client = self.fetch_client or SimpleWebFetchClient()
        uses_default_pinned_fetch = self.fetch_client is None
        if not uses_default_pinned_fetch:
            _validate_public_http_url(url, resolve_dns=True)
        try:
            raw = client.fetch(url, max_bytes=max_bytes)
        except Exception as exc:
            raise _tool_validation_error("fetch_page provider call failed. See server logs for provider details.") from exc
        if not isinstance(raw, dict):
            raise _tool_validation_error("fetch_page provider returned an invalid response.")
        final_url = _validate_public_http_url(
            str(raw.get("url", url)),
            resolve_dns=not uses_default_pinned_fetch,
        )
        return {
            "provider": provider,
            "mocked": False,
            "url": normalize_public_source_url(final_url),
            "url_hash": _hash(final_url),
            "latency_ms": _elapsed_ms(started),
            "content": bounded_external_text(raw.get("content"), max_chars=max_bytes, max_bytes=max_bytes),
            "content_type": bounded_external_text(raw.get("content_type"), max_chars=120, max_bytes=480),
            "status_code": _validated_status_code(raw.get("status_code")),
        }


class TavilySearchClient:
    def search(self, *, query: str, max_results: int) -> dict[str, Any]:
        api_key = os.environ.get(TAVILY_API_KEY_ENV)
        if not api_key:
            raise _tool_validation_error(f"Web search provider requires {TAVILY_API_KEY_ENV}.")
        try:
            import httpx
        except ImportError as exc:
            raise _tool_validation_error("httpx is required for Tavily web search.") from exc
        deadline = perf_counter() + WEB_REQUEST_TIMEOUT_SECONDS
        with httpx.Client(
            timeout=WEB_REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            with client.stream(
                "POST",
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
                headers={"Accept-Encoding": "identity"},
            ) as response:
                response.raise_for_status()
                content_encoding = str(response.headers.get("content-encoding", "")).lower()
                if content_encoding not in {"", "identity"}:
                    raise _tool_validation_error(
                        "web_search provider returned an unsupported content encoding."
                    )
                body = bytearray()
                for chunk in response.iter_raw(chunk_size=WEB_READ_CHUNK_BYTES):
                    _remaining_web_timeout(deadline)
                    if len(body) + len(chunk) > MAX_TAVILY_RESPONSE_BYTES:
                        raise _tool_validation_error("web_search provider response is too large.")
                    body.extend(chunk)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise _tool_validation_error("web_search provider returned invalid JSON.") from exc
        _validate_bounded_json(payload)
        if not isinstance(payload, dict):
            raise _tool_validation_error("web_search provider returned an invalid response.")
        return payload


class SimpleWebFetchClient:
    def fetch(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        deadline = perf_counter() + WEB_REQUEST_TIMEOUT_SECONDS
        current_url = url
        for _ in range(5):
            current_url = _validate_public_http_url(current_url)
            parsed = urlparse(current_url)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            resolved_addresses = _resolve_public_addresses(host, port)
            remaining_timeout = _remaining_web_timeout(deadline)
            connection = _pinned_http_connection(
                parsed,
                resolved_addresses,
                timeout_seconds=remaining_timeout,
                deadline=deadline,
            )
            response: http.client.HTTPResponse | None = None
            try:
                connection.request(
                    "GET",
                    _request_target(parsed),
                    headers={"User-Agent": "team-agent-harness/0.1"},
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise _tool_validation_error("fetch_page redirect response is missing Location.")
                    current_url = urljoin(current_url, location)
                    _validate_public_http_url(current_url)
                    continue
                if not 200 <= response.status < 300:
                    raise HTTPError(current_url, response.status, response.reason, response.headers, None)
                final_url = current_url
                content_type = response.getheader("content-type", "")
                body = _read_bounded_response_body(
                    response,
                    connection,
                    max_bytes=max_bytes,
                    deadline=deadline,
                )
                status_code = response.status
                break
            finally:
                if response is not None:
                    response.close()
                connection.close()
        else:
            raise _tool_validation_error("fetch_page redirect limit exceeded.")
        if len(body) > max_bytes:
            body = body[:max_bytes]
        return {
            "url": final_url,
            "content": body.decode("utf-8", errors="replace"),
            "content_type": content_type,
            "status_code": status_code,
        }


@dataclass(frozen=True)
class _ResolvedAddress:
    family: int
    socket_type: int
    protocol: int
    socket_address: tuple[Any, ...]


def _resolve_public_addresses(host: str, port: int) -> tuple[_ResolvedAddress, ...]:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _tool_validation_error("fetch_page URL host could not be resolved.") from exc
    except OSError as exc:
        raise _tool_validation_error("fetch_page URL host resolution failed.") from exc
    if not results:
        raise _tool_validation_error("fetch_page URL host could not be resolved.")

    resolved: list[_ResolvedAddress] = []
    seen: set[tuple[int, int, int, tuple[Any, ...]]] = set()
    for family, socket_type, protocol, _canonical_name, socket_address in results:
        if family not in {socket.AF_INET, socket.AF_INET6} or not socket_address:
            raise _tool_validation_error("fetch_page URL host returned an invalid address.")
        address = str(socket_address[0])
        try:
            ip_address(address)
        except ValueError as exc:
            raise _tool_validation_error("fetch_page URL host returned an invalid address.") from exc
        if _is_blocked_host(address):
            raise _tool_permission_error("fetch_page URL host resolves to a blocked address.")
        key = (family, socket_type, protocol, socket_address)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(
            _ResolvedAddress(
                family=family,
                socket_type=socket_type,
                protocol=protocol,
                socket_address=socket_address,
            )
        )
    return tuple(resolved)


def _pinned_http_connection(
    parsed: Any,
    resolved_addresses: tuple[_ResolvedAddress, ...],
    *,
    timeout_seconds: float,
    deadline: float,
) -> http.client.HTTPConnection:
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if parsed.scheme.lower() == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout_seconds)

    # HTTPConnection still owns Host, SNI, and TLS verification; only its TCP dialer is replaced.
    connection._create_connection = lambda _address, timeout, source_address: _connect_to_resolved_addresses(  # type: ignore[attr-defined]
        resolved_addresses,
        timeout=timeout,
        source_address=source_address,
        deadline=deadline,
    )
    return connection


def _read_bounded_response_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    *,
    max_bytes: int,
    deadline: float,
) -> bytes:
    body = bytearray()
    while len(body) <= max_bytes:
        remaining = _remaining_web_timeout(deadline)
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        chunk = response.read1(min(WEB_READ_CHUNK_BYTES, max_bytes + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


def _remaining_web_timeout(deadline: float) -> float:
    remaining = deadline - perf_counter()
    if remaining <= 0:
        raise _tool_validation_error("web provider request exceeded its total time budget.")
    return remaining


def _validate_bounded_json(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    item_count = 0
    while stack:
        item, depth = stack.pop()
        item_count += 1
        if item_count > MAX_TAVILY_JSON_ITEMS:
            raise _tool_validation_error("web_search provider response contains too many items.")
        if depth > MAX_TAVILY_JSON_DEPTH:
            raise _tool_validation_error("web_search provider response is nested too deeply.")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _connect_to_resolved_addresses(
    resolved_addresses: tuple[_ResolvedAddress, ...],
    *,
    timeout: float | object,
    source_address: tuple[str, int] | None,
    deadline: float | None = None,
) -> socket.socket:
    last_error: OSError | None = None
    for resolved in resolved_addresses:
        sock = socket.socket(resolved.family, resolved.socket_type, resolved.protocol)
        try:
            attempt_timeout = timeout
            if deadline is not None:
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    raise _tool_validation_error(
                        "web provider request exceeded its total time budget."
                    )
                if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                    attempt_timeout = min(float(timeout), remaining)
                else:
                    attempt_timeout = remaining
            sock.settimeout(attempt_timeout)
            if source_address is not None:
                sock.bind(source_address)
            sock.connect(resolved.socket_address)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
        except Exception:
            sock.close()
            raise
    if last_error is not None:
        raise last_error
    raise OSError("No validated address was available for the connection.")


def _request_target(parsed: Any) -> str:
    return urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))


def web_tool_provider_catalog() -> list[ToolProviderInfo]:
    provider = os.environ.get(WEB_SEARCH_PROVIDER_ENV, "mock").strip().lower() or "mock"
    has_key = bool(os.environ.get(TAVILY_API_KEY_ENV))
    real_configured = provider == "tavily" and has_key
    return [
        ToolProviderInfo(
            name="web_search",
            provider=provider,
            adapter="tavily" if provider == "tavily" else "mock",
            enabled=provider == "mock" or (_real_web_search_allowed() and real_configured),
            real_calls=provider != "mock",
            real_calls_configured=real_configured,
            requires_credentials=provider != "mock",
            description=(
                "联网搜索工具。默认本地模拟；Tavily 真实搜索需要 TEAM_AGENT_ALLOW_REAL_WEB_SEARCH=1 "
                "和服务端 TAVILY_API_KEY。"
            ),
        ),
        ToolProviderInfo(
            name="fetch_page",
            provider=provider,
            adapter="http_fetch" if provider == "tavily" else "mock",
            enabled=provider == "mock" or (_real_web_search_allowed() and real_configured),
            real_calls=provider != "mock",
            real_calls_configured=real_configured,
            requires_credentials=provider != "mock",
            description="网页抓取工具。真实模式只允许公开 http(s) URL，并拒绝本机和内网地址。",
        ),
    ]


def safe_tool_input_summary(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool_name in {"web_search", "web_search_mock"}:
        query = str(payload.get("query", ""))
        return {
            "query_hash": _hash(query) if query else "",
            "query_length": len(query),
            "max_results": _safe_max_results_for_trace(
                payload.get("max_results", DEFAULT_WEB_SEARCH_MAX_RESULTS)
            ),
            "provider": (
                "mock"
                if tool_name == "web_search_mock"
                else os.environ.get(WEB_SEARCH_PROVIDER_ENV, "mock")
            ),
        }
    if tool_name in {"fetch_page", "fetch_page_mock"}:
        url = str(payload.get("url", ""))
        return {"url_hash": _hash(url) if url else ""}
    return _generic_safe_summary(payload)


def safe_tool_output_summary(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool_name in {"web_search", "web_search_mock"}:
        results = result.get("results", [])
        urls = [str(item.get("url", "")) for item in results if isinstance(item, dict)]
        return {
            "provider": result.get("provider", "mock"),
            "mocked": bool(result.get("mocked", True)),
            "result_count": len(results) if isinstance(results, list) else 0,
            "urls": [_safe_url_for_trace(url) for url in urls],
            "latency_ms": result.get("latency_ms", 0),
        }
    if tool_name in {"fetch_page", "fetch_page_mock"}:
        content = str(result.get("content", ""))
        return {
            "provider": result.get("provider", "mock"),
            "mocked": bool(result.get("mocked", True)),
            "url": _safe_url_for_trace(str(result.get("url", ""))),
            "status_code": result.get("status_code", 0),
            "content_length": len(content),
            "latency_ms": result.get("latency_ms", 0),
        }
    return _generic_safe_summary(result)


def redact_tool_message(message: str) -> str:
    redacted = message
    for marker in ("api_key", "apikey", "token", "secret", "password"):
        redacted = _redact_after_marker(redacted, marker)
    if "Bearer " in redacted:
        redacted = redacted.split("Bearer ", 1)[0] + "Bearer [REDACTED]"
    tavily_key = os.environ.get(TAVILY_API_KEY_ENV)
    if tavily_key:
        redacted = redacted.replace(tavily_key, "[REDACTED]")
    return redacted


def _normalize_search_results(raw: dict[str, Any], max_results: int) -> list[dict[str, str]]:
    raw_results = raw.get("results", [])
    if not isinstance(raw_results, list):
        return []
    normalized = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        try:
            url = normalize_public_source_url(item.get("url"))
        except Exception:
            continue
        normalized.append(
            {
                "title": bounded_external_text(item.get("title"), max_chars=200, max_bytes=800),
                "url": url,
                "snippet": bounded_external_text(
                    item.get("content", item.get("snippet", "")),
                    max_chars=500,
                    max_bytes=2_000,
                ),
                "published_at": bounded_external_text(
                    item.get("published_date", item.get("published_at", "")),
                    max_chars=40,
                    max_bytes=160,
                ),
            }
        )
    return normalized


def _mock_search(query: str, max_results: int, provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "mocked": True,
        "query_hash": _hash(query),
        "latency_ms": 1,
        "results": [
            {
                "title": "Mock result",
                "url": "https://example.local/mock-search-result",
                "snippet": f"Mock search result for {_preview(query)}.",
                "published_at": "",
            }
        ][:max_results],
    }


def _mock_fetch(url: str, provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "mocked": True,
        "url": url,
        "url_hash": _hash(url),
        "latency_ms": 1,
        "content": "mock page content",
        "content_type": "text/plain",
        "status_code": 200,
    }


def _validate_query(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _tool_validation_error("web_search query must be a non-empty string.")
    query = value.strip()
    if len(query) > 500:
        raise _tool_validation_error("web_search query is too long.")
    return query


def _validate_max_results(value: Any) -> int:
    try:
        max_results = int(value)
    except (TypeError, ValueError) as exc:
        raise _tool_validation_error("max_results must be an integer.") from exc
    if max_results < 1 or max_results > 10:
        raise _tool_validation_error("max_results must be between 1 and 10.")
    return max_results


def _safe_max_results_for_trace(value: Any) -> int | str:
    try:
        return _validate_max_results(value)
    except Exception:
        return "[invalid]"


def _validate_max_bytes(value: Any) -> int:
    try:
        max_bytes = int(value)
    except (TypeError, ValueError) as exc:
        raise _tool_validation_error("max_bytes must be an integer.") from exc
    if max_bytes < 1 or max_bytes > 256 * 1024:
        raise _tool_validation_error("max_bytes must be between 1 and 262144.")
    return max_bytes


def _validate_public_http_url(value: Any, *, resolve_dns: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _tool_validation_error("fetch_page url must be a non-empty string.")
    url = value.strip()
    if len(url) > MAX_PUBLIC_URL_CHARS:
        raise _tool_validation_error("fetch_page URL is too long.")
    try:
        url.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _tool_validation_error("fetch_page URL contains invalid Unicode.") from exc
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc
        username = parsed.username
        password = parsed.password
        fragment = parsed.fragment
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise _tool_validation_error("fetch_page URL is invalid.") from exc
    if scheme not in {"http", "https"} or not netloc or not host:
        raise _tool_permission_error("fetch_page only allows http(s) URLs.")
    if username or password or fragment:
        raise _tool_permission_error("fetch_page URL must not include credentials or fragments.")
    if port is not None and not 1 <= port <= 65_535:
        raise _tool_validation_error("fetch_page URL has an invalid port.")
    normalized_host = _normalize_idna_host(host)
    if _is_blocked_host(normalized_host):
        raise _tool_permission_error("fetch_page URL host is not allowed.")
    if resolve_dns and _host_resolves_to_blocked_address(normalized_host):
        raise _tool_permission_error("fetch_page URL host resolves to a blocked address.")
    return url


def normalize_public_source_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _tool_validation_error("fetch_page url must be a non-empty string.")
    raw_url = value.strip()
    if len(raw_url) > MAX_PUBLIC_URL_CHARS:
        raise _tool_validation_error("fetch_page URL is too long.")
    try:
        raw_url.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _tool_validation_error("fetch_page URL contains invalid Unicode.") from exc
    try:
        parsed = urlparse(raw_url)
        username = parsed.username
        password = parsed.password
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise _tool_validation_error("fetch_page URL is invalid.") from exc
    if username or password:
        raise _tool_permission_error("fetch_page URL must not include credentials.")
    normalized_host = _normalize_idna_host(host)
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    scheme = parsed.scheme.lower()
    validation_netloc = f"{normalized_host}:{port}" if port is not None else normalized_host
    candidate = urlunparse((scheme, validation_netloc, parsed.path or "/", "", "", ""))
    _validate_public_http_url(candidate)
    canonical_netloc = normalized_host
    if port is not None and port != (443 if scheme == "https" else 80):
        canonical_netloc = f"{canonical_netloc}:{port}"
    return urlunparse((scheme, canonical_netloc, parsed.path or "/", "", "", ""))


def _normalize_idna_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise _tool_validation_error("fetch_page URL has an invalid host.") from exc


def bounded_external_text(value: Any, *, max_chars: int, max_bytes: int) -> str:
    text = str(value or "")[:max_chars]
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _validated_status_code(value: Any) -> int:
    if type(value) is not int or not 100 <= value <= 599:
        raise _tool_validation_error("fetch_page provider returned an invalid status code.") from None
    return value


def _is_blocked_host(host: str) -> bool:
    normalized = host.lower().strip("[]").rstrip(".")
    if (
        not normalized
        or normalized in {"localhost", "localhost.localdomain"}
        or normalized.endswith(".localhost")
    ):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return _looks_like_legacy_ipv4(normalized)
    return _is_blocked_address(address)


def _looks_like_legacy_ipv4(host: str) -> bool:
    labels = host.split(".")
    return bool(labels) and all(
        bool(label)
        and (
            label.isdecimal()
            or (
                label.lower().startswith("0x")
                and len(label) > 2
                and all(character in "0123456789abcdef" for character in label[2:].lower())
            )
        )
        for label in labels
    )


def _is_blocked_address(address: IPv4Address | IPv6Address) -> bool:
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
        or (isinstance(address, IPv6Address) and address.is_site_local)
    ):
        return True
    if not isinstance(address, IPv6Address):
        return False
    embedded = [address.ipv4_mapped, address.sixtofour]
    if address.teredo is not None:
        embedded.extend(address.teredo)
    return any(value is not None and _is_blocked_address(value) for value in embedded)


def _host_resolves_to_blocked_address(host: str) -> bool:
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _tool_validation_error("fetch_page URL host could not be resolved.") from exc
    except OSError as exc:
        raise _tool_validation_error("fetch_page URL host resolution failed.") from exc
    if not results:
        raise _tool_validation_error("fetch_page URL host could not be resolved.")
    for result in results:
        if len(result) < 5 or not result[4]:
            raise _tool_validation_error("fetch_page URL host returned an invalid address.")
        address = result[4][0]
        if _is_blocked_host(address):
            return True
    return False


def _real_web_search_allowed() -> bool:
    return os.environ.get(ALLOW_REAL_WEB_SEARCH_ENV) == "1"


def _require_tavily_api_key() -> None:
    if not os.environ.get(TAVILY_API_KEY_ENV):
        raise _tool_validation_error(f"Web tools provider requires {TAVILY_API_KEY_ENV}.")


def _elapsed_ms(started: float) -> int:
    return max(1, int((perf_counter() - started) * 1000))


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _preview(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= 12:
        return compact
    return f"{compact[:9]}..."


def _safe_url_for_trace(url: str) -> str:
    try:
        url.encode("utf-8")
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        parsed_host = parsed.hostname
        if scheme not in {"http", "https"} or parsed_host is None:
            return "[invalid-url]"
        normalized_host = parsed_host.encode("idna").decode("ascii").lower()
        port_value = parsed.port
    except (UnicodeError, ValueError):
        return "[invalid-url]"
    host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    port = f":{port_value}" if port_value is not None else ""
    return f"{scheme}://{host}{port}"


def _generic_safe_summary(value: dict[str, Any]) -> dict[str, Any]:
    summarized: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if normalized in {"content", "body", "raw", "headers"} and isinstance(item, str):
            summarized[f"{key}_length"] = len(item)
        elif any(marker in normalized for marker in ("key", "token", "secret", "password", "authorization")):
            summarized[key] = "[REDACTED]"
        elif isinstance(item, str) and len(item) > 500:
            summarized[f"{key}_length"] = len(item)
        else:
            summarized[key] = item
    return summarized


def _redact_after_marker(message: str, marker: str) -> str:
    lower = message.lower()
    index = lower.find(marker)
    if index < 0:
        return message
    prefix = message[: index + len(marker)]
    suffix = message[index + len(marker) :]
    for sep in ("=", ":", " "):
        if suffix.startswith(sep):
            return prefix + sep + "[REDACTED]"
    return message


def _tool_validation_error(message: str) -> Exception:
    from app.core.tool_gateway import ToolValidationError

    return ToolValidationError(message)


def _tool_permission_error(message: str) -> Exception:
    from app.core.tool_gateway import ToolPermissionError

    return ToolPermissionError(message)
