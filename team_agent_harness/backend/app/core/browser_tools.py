from __future__ import annotations

from dataclasses import dataclass
import json
import os
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.core.web_tools import (
    DEFAULT_WEB_FETCH_MAX_BYTES,
    DEFAULT_WEB_SEARCH_MAX_RESULTS,
    ToolProviderInfo,
    bounded_external_text,
    _elapsed_ms,
    _generic_safe_summary,
    _hash,
    _preview,
    _safe_max_results_for_trace,
    _safe_url_for_trace,
    _tool_permission_error,
    _tool_validation_error,
    _validated_status_code,
    _validate_max_bytes,
    _validate_max_results,
    _validate_public_http_url,
    _validate_query,
    normalize_public_source_url,
)


ALLOW_BROWSER_ACCESS_ENV = "TEAM_AGENT_ALLOW_BROWSER_ACCESS"
BROWSER_PROVIDER_ENV = "TEAM_AGENT_BROWSER_PROVIDER"
BROWSER_SEARCH_ENGINE_ENV = "TEAM_AGENT_BROWSER_SEARCH_ENGINE"
BROWSER_CDP_URL_ENV = "TEAM_AGENT_BROWSER_CDP_URL"
DEFAULT_BROWSER_CDP_URL = "http://127.0.0.1:3456"
CDP_PROXY_OPERATION_TIMEOUT_SECONDS = 180
MAX_CDP_PROXY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CDP_PROXY_HEALTH_BYTES = 4096
CDP_PROXY_CLIENT_HEADER = "X-Team-Agent-Browser-Proxy"
CDP_PROXY_CLIENT_HEADER_VALUE = "1"
SUPPORTED_BROWSER_PROVIDERS = {"edge", "chrome", "browser_cdp"}


class BrowserSearchClient(Protocol):
    def search(self, *, query: str, max_results: int, search_engine: str) -> dict[str, Any]:
        ...


class BrowserFetchClient(Protocol):
    def fetch(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        ...


@dataclass
class BrowserToolProvider:
    search_client: BrowserSearchClient | None = None
    fetch_client: BrowserFetchClient | None = None

    @property
    def provider_name(self) -> str:
        return os.environ.get(BROWSER_PROVIDER_ENV, "mock").strip().lower() or "mock"

    @property
    def search_engine(self) -> str:
        return os.environ.get(BROWSER_SEARCH_ENGINE_ENV, "bing").strip().lower() or "bing"

    @property
    def real_calls_enabled(self) -> bool:
        return os.environ.get(ALLOW_BROWSER_ACCESS_ENV) == "1"

    def real_access_available(self) -> bool:
        search_available, fetch_available = self.real_access_availability()
        return search_available or fetch_available

    def real_search_access_available(self) -> bool:
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return False
        _require_supported_browser_provider(provider)
        return self.search_client is not None or _browser_proxy_health()

    def real_fetch_access_available(self) -> bool:
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return False
        _require_supported_browser_provider(provider)
        return self.fetch_client is not None or _browser_proxy_health()

    def real_access_availability(self) -> tuple[bool, bool]:
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return False, False
        _require_supported_browser_provider(provider)
        proxy_available = False
        if self.search_client is None or self.fetch_client is None:
            proxy_available = _browser_proxy_health()
        return (
            self.search_client is not None or proxy_available,
            self.fetch_client is not None or proxy_available,
        )

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = _validate_query(payload.get("query"))
        max_results = _validate_max_results(payload.get("max_results", DEFAULT_WEB_SEARCH_MAX_RESULTS))
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return _mock_browser_search(query, max_results, provider, self.search_engine)
        _require_supported_browser_provider(provider)
        if self.search_client is None and not _browser_proxy_health():
            raise _tool_validation_error("browser_search requires a reachable local browser CDP proxy.")
        started = perf_counter()
        client = self.search_client or CdpBrowserClient()
        try:
            raw = client.search(query=query, max_results=max_results, search_engine=self.search_engine)
        except Exception as exc:
            raise _tool_validation_error("browser_search provider call failed. See server logs for provider details.") from exc
        return {
            "provider": provider,
            "adapter": "browser_cdp",
            "mocked": False,
            "search_engine": self.search_engine,
            "query_hash": _hash(query),
            "latency_ms": _elapsed_ms(started),
            "results": _normalize_browser_search_results(raw, max_results),
        }

    def fetch_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = _validate_public_http_url(payload.get("url"))
        max_bytes = _validate_max_bytes(payload.get("max_bytes", DEFAULT_WEB_FETCH_MAX_BYTES))
        provider = self.provider_name
        if provider == "mock" or not self.real_calls_enabled:
            return _mock_browser_fetch(url, provider)
        _require_supported_browser_provider(provider)
        client = self.fetch_client or CdpBrowserClient()
        uses_cdp_proxy = isinstance(client, CdpBrowserClient)
        if uses_cdp_proxy and not _browser_proxy_health():
            raise _tool_validation_error("browser_fetch requires a reachable local browser CDP proxy.")
        url = _validate_public_http_url(url, resolve_dns=not uses_cdp_proxy)
        started = perf_counter()
        try:
            raw = client.fetch(url, max_bytes=max_bytes)
        except Exception as exc:
            raise _tool_validation_error("browser_fetch provider call failed. See server logs for provider details.") from exc
        final_url = _validate_public_http_url(
            str(raw.get("url", url)),
            resolve_dns=not uses_cdp_proxy,
        )
        content = _truncate_utf8(str(raw.get("content", "")), max_bytes)
        return {
            "provider": provider,
            "adapter": "browser_cdp",
            "mocked": False,
            "url": normalize_public_source_url(final_url),
            "url_hash": _hash(final_url),
            "latency_ms": _elapsed_ms(started),
            "title": bounded_external_text(raw.get("title"), max_chars=200, max_bytes=800),
            "content": content,
            "content_type": bounded_external_text(raw.get("content_type"), max_chars=120, max_bytes=480),
            "status_code": _validated_status_code(raw.get("status_code")),
        }


class CdpBrowserClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = _validate_cdp_base_url(base_url or os.environ.get(BROWSER_CDP_URL_ENV, DEFAULT_BROWSER_CDP_URL))

    def search(self, *, query: str, max_results: int, search_engine: str) -> dict[str, Any]:
        script = _search_extract_script(max_results)
        raw = self._navigate_and_eval(_search_url(query, search_engine), script)
        parsed = _decode_eval_json(raw)
        return {"results": parsed if isinstance(parsed, list) else []}

    def fetch(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        script = _fetch_extract_script(max_bytes)
        raw = self._navigate_and_eval(url, script)
        parsed = _decode_eval_json(raw)
        if not isinstance(parsed, dict):
            raise _tool_validation_error("browser_fetch returned an unexpected browser response.")
        return parsed

    def _navigate_and_eval(self, url: str, script: str) -> str:
        response = self._request("POST", f"/navigate-eval?{urlencode({'url': url})}", body=script)
        data = _decode_proxy_response(response)
        if isinstance(data, dict):
            for key in ("result", "value", "text", "data"):
                if key in data:
                    return str(data[key])
            raise _tool_validation_error("Browser CDP proxy did not return an evaluation value.")
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False)

    def _request(self, method: str, path: str, body: str | None = None) -> str:
        url = self.base_url.rstrip("/") + path
        data = body.encode("utf-8") if body is not None else None
        request = Request(url, data=data, method=method)
        request.add_header(CDP_PROXY_CLIENT_HEADER, CDP_PROXY_CLIENT_HEADER_VALUE)
        if body is not None:
            request.add_header("Content-Type", "text/plain; charset=utf-8")
        with _open_local_url(
            request,
            timeout=CDP_PROXY_OPERATION_TIMEOUT_SECONDS,
        ) as response:
            raw_response = response.read(MAX_CDP_PROXY_RESPONSE_BYTES + 1)
        if len(raw_response) > MAX_CDP_PROXY_RESPONSE_BYTES:
            raise _tool_validation_error("Browser CDP proxy response exceeded the allowed size.")
        try:
            return raw_response.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _tool_validation_error("Browser CDP proxy returned invalid UTF-8.") from exc


def browser_tool_provider_catalog(provider_instance: BrowserToolProvider | None = None) -> list[ToolProviderInfo]:
    provider_instance = provider_instance or BrowserToolProvider()
    provider = os.environ.get(BROWSER_PROVIDER_ENV, "mock").strip().lower() or "mock"
    real_configured = provider in SUPPORTED_BROWSER_PROVIDERS
    if provider != "mock" and _real_browser_access_allowed():
        search_connected, fetch_connected = provider_instance.real_access_availability()
    else:
        search_connected, fetch_connected = False, False
    description_suffix = (
        "真实浏览器桥接需要 TEAM_AGENT_ALLOW_BROWSER_ACCESS=1，"
        "并通过本机 CDP Proxy 访问浏览器；不需要 API key。"
    )
    return [
        ToolProviderInfo(
            name="browser_search",
            provider=provider,
            adapter="browser_cdp" if real_configured else "mock",
            enabled=provider == "mock" or search_connected,
            real_calls=provider != "mock",
            real_calls_configured=real_configured,
            requires_credentials=False,
            description=(
                f"浏览器联网搜索工具。默认本地模拟；{description_suffix}"
                f" 当前 CDP 连接：{'可用' if search_connected else '不可用或未启用'}。"
            ),
        ),
        ToolProviderInfo(
            name="browser_fetch",
            provider=provider,
            adapter="browser_cdp" if real_configured else "mock",
            enabled=provider == "mock" or fetch_connected,
            real_calls=provider != "mock",
            real_calls_configured=real_configured,
            requires_credentials=False,
            description=(
                "浏览器网页读取工具。真实模式只允许公开 http(s) URL，并拒绝本机、内网、凭据和片段 URL。"
                f" 当前 CDP 连接：{'可用' if fetch_connected else '不可用或未启用'}。"
            ),
        ),
    ]


def browser_access_enabled(provider_instance: BrowserToolProvider | None = None) -> bool:
    provider_instance = provider_instance or BrowserToolProvider()
    return provider_instance.real_access_available()


def browser_search_access_enabled(provider_instance: BrowserToolProvider | None = None) -> bool:
    provider_instance = provider_instance or BrowserToolProvider()
    return provider_instance.real_search_access_available()


def browser_fetch_access_enabled(provider_instance: BrowserToolProvider | None = None) -> bool:
    provider_instance = provider_instance or BrowserToolProvider()
    return provider_instance.real_fetch_access_available()


def safe_browser_tool_input_summary(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "browser_search":
        query = str(payload.get("query", ""))
        return {
            "query_hash": _hash(query) if query else "",
            "query_length": len(query),
            "max_results": _safe_max_results_for_trace(
                payload.get("max_results", DEFAULT_WEB_SEARCH_MAX_RESULTS)
            ),
            "provider": os.environ.get(BROWSER_PROVIDER_ENV, "mock"),
            "search_engine": os.environ.get(BROWSER_SEARCH_ENGINE_ENV, "bing"),
        }
    if tool_name == "browser_fetch":
        url = str(payload.get("url", ""))
        return {"url_hash": _hash(url) if url else ""}
    return _generic_safe_summary(payload)


def safe_browser_tool_output_summary(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "browser_search":
        results = result.get("results", [])
        urls = [str(item.get("url", "")) for item in results if isinstance(item, dict)]
        return {
            "provider": result.get("provider", "mock"),
            "mocked": bool(result.get("mocked", True)),
            "search_engine": result.get("search_engine", "bing"),
            "result_count": len(results) if isinstance(results, list) else 0,
            "urls": [_safe_url_for_trace(url) for url in urls],
            "latency_ms": result.get("latency_ms", 0),
        }
    if tool_name == "browser_fetch":
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


def _normalize_browser_search_results(raw: dict[str, Any], max_results: int) -> list[dict[str, str]]:
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
                    item.get("snippet", item.get("content", "")),
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


def _mock_browser_search(query: str, max_results: int, provider: str, search_engine: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "adapter": "mock",
        "mocked": True,
        "search_engine": search_engine,
        "query_hash": _hash(query),
        "latency_ms": 1,
        "results": [
            {
                "title": "Mock browser result",
                "url": "https://example.local/browser-search-result",
                "snippet": f"Mock browser search result for {_preview(query)}.",
                "published_at": "",
            }
        ][:max_results],
    }


def _mock_browser_fetch(url: str, provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "adapter": "mock",
        "mocked": True,
        "url": url,
        "url_hash": _hash(url),
        "latency_ms": 1,
        "title": "Mock browser page",
        "content": "mock browser page content",
        "content_type": "text/plain",
        "status_code": 200,
    }


def _require_supported_browser_provider(provider: str) -> None:
    if provider not in SUPPORTED_BROWSER_PROVIDERS:
        raise _tool_validation_error(f"Unsupported browser provider: {provider}")


def _real_browser_access_allowed() -> bool:
    return os.environ.get(ALLOW_BROWSER_ACCESS_ENV) == "1"


def _validate_cdp_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _tool_validation_error("Browser CDP proxy URL must be http(s).")
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise _tool_permission_error("Browser CDP proxy must be local-only.")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _tool_validation_error("Browser CDP proxy URL must be an origin without credentials or a path.")
    try:
        parsed.port
    except ValueError as exc:
        raise _tool_validation_error("Browser CDP proxy URL port is invalid.") from exc
    return value.rstrip("/")


def _browser_proxy_health() -> bool:
    try:
        base_url = _validate_cdp_base_url(os.environ.get(BROWSER_CDP_URL_ENV, DEFAULT_BROWSER_CDP_URL))
        request = Request(
            f"{base_url}/health",
            headers={CDP_PROXY_CLIENT_HEADER: CDP_PROXY_CLIENT_HEADER_VALUE},
        )
        with _open_local_url(request, timeout=2) as response:
            payload = _read_bounded_response(response, MAX_CDP_PROXY_HEALTH_BYTES).decode("utf-8")
    except (HTTPError, OSError, TypeError, UnicodeDecodeError, URLError, ValueError):
        return False
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    capabilities = data.get("capabilities", [])
    return (
        data.get("status") == "ok"
        and data.get("connected") is True
        and data.get("proxy") == base_url
        and isinstance(capabilities, list)
        and "atomic_navigate_eval_v2" in capabilities
        and "pinned_public_egress_v1" in capabilities
        and "isolated_browser_context_v1" in capabilities
    )


def _read_bounded_response(response: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
        if not isinstance(chunk, bytes):
            raise TypeError("HTTP response body must be bytes.")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("HTTP response body exceeded the allowed size.")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        return None


def _open_local_url(request: Request, *, timeout: float):
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=timeout)


def _search_url(query: str, search_engine: str) -> str:
    if search_engine not in {"bing", "google"}:
        raise _tool_validation_error(f"Unsupported browser search engine: {search_engine}")
    if search_engine == "google":
        return f"https://www.google.com/search?q={quote_plus(query)}"
    return f"https://www.bing.com/search?q={quote_plus(query)}"


def _search_extract_script(max_results: int) -> str:
    return f"""
(() => {{
  const maxResults = {max_results};
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const seen = new Set();
  const results = [];
  for (const anchor of anchors) {{
    const href = anchor.href || "";
    const title = (anchor.innerText || anchor.textContent || "").replace(/\\s+/g, " ").trim();
    if (!href.startsWith("http") || !title || title.length < 3) continue;
    const url = new URL(href);
    if (url.hostname.includes("bing.com") || url.hostname.includes("google.com")) continue;
    const normalized = url.href.split("#")[0];
    if (seen.has(normalized)) continue;
    seen.add(normalized);
    const container = anchor.closest("li, article, div");
    const snippet = container ? (container.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 500) : "";
    results.push({{ title: title.slice(0, 200), url: normalized, snippet }});
    if (results.length >= maxResults) break;
  }}
  return JSON.stringify(results);
}})()
""".strip()


def _fetch_extract_script(max_bytes: int) -> str:
    return f"""
(() => {{
  const source = (document.body && document.body.innerText ? document.body.innerText : "")
    .replace(/\\s+\\n/g, "\\n")
    .trim();
  const encoded = new TextEncoder().encode(source);
  let end = Math.min(encoded.length, {max_bytes});
  while (end > 0 && (encoded[end] & 0xc0) === 0x80) end -= 1;
  const text = encoded.length <= {max_bytes}
    ? source
    : new TextDecoder("utf-8", {{ fatal: true }}).decode(encoded.slice(0, end));
  return JSON.stringify({{
    url: location.href,
    title: (document.title || "").slice(0, 200),
    content: text,
    content_type: (document.contentType || "text/html").slice(0, 120),
    status_code: 200
  }});
}})()
""".strip()


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _decode_proxy_response(response: str) -> Any:
    text = response.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _decode_eval_json(response: str) -> Any:
    text = response.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _tool_validation_error("Browser CDP eval returned invalid JSON.") from exc
    if isinstance(data, dict) and "value" in data:
        value = data["value"]
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
    return data
