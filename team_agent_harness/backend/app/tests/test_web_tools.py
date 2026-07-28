from __future__ import annotations

import io
import json
import socket

import pytest
from fastapi.testclient import TestClient

import app.core.web_tools as web_tools_module
from app.core.artifacts import ArtifactStore
from app.core.models import AgentDefinition, AgentRun, Run, Task, TraceEventType
from app.core.storage import SQLiteStorage
from app.core.tool_gateway import ToolContext, ToolPermissionError, ToolValidationError, create_mock_gateway
from app.core.web_tools import (
    SimpleWebFetchClient,
    TavilySearchClient,
    WebToolProvider,
    normalize_public_source_url,
    web_tool_provider_catalog,
)
from app.core.trace import TraceLogger
from app.main import create_app


class FakeSearchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search(self, *, query: str, max_results: int) -> dict[str, object]:
        self.calls.append({"query": query, "max_results": max_results})
        return {
            "results": [
                {
                    "title": "Harness source",
                    "url": "https://example.com/harness",
                    "content": "Full snippet should not be written to trace.",
                    "published_date": "2026-06-20",
                }
            ]
        }


class FakeFetchClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, *, max_bytes: int) -> dict[str, object]:
        self.calls.append(url)
        return {
            "url": url,
            "content": "Fetched page body should become an artifact, not trace payload.",
            "content_type": "text/html",
            "status_code": 200,
        }


class LargeFetchClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, *, max_bytes: int) -> dict[str, object]:
        self.calls.append(url)
        return {
            "url": url,
            "content": "x" * (max_bytes + 20),
            "content_type": "text/plain",
            "status_code": 200,
        }


class FakeNetworkSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.connected_to = None
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout) -> None:
        return None

    def bind(self, _source_address) -> None:
        return None

    def connect(self, socket_address) -> None:
        self.connected_to = socket_address

    def setsockopt(self, *_args) -> None:
        return None

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def makefile(self, *_args, **_kwargs):
        return io.BytesIO(self.response)

    def close(self) -> None:
        self.closed = True


class FakeTlsContext:
    def __init__(self) -> None:
        self.server_hostnames: list[str] = []

    def wrap_socket(self, sock, *, server_hostname: str):
        self.server_hostnames.append(server_hostname)
        return sock


def install_fake_sockets(monkeypatch: pytest.MonkeyPatch, *responses: bytes) -> list[FakeNetworkSocket]:
    pending = list(responses)
    sockets: list[FakeNetworkSocket] = []

    def create_socket(_family, _socket_type, _protocol):
        assert pending, "unexpected outbound connection"
        sock = FakeNetworkSocket(pending.pop(0))
        sockets.append(sock)
        return sock

    monkeypatch.setattr(web_tools_module.socket, "socket", create_socket)
    return sockets


@pytest.fixture
def tool_env(tmp_path):
    with SQLiteStorage(tmp_path / "harness.sqlite3") as db:
        db.init_schema()
        task = db.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="research"))
        run = db.create_run(Run(id="run-1", task_id=task.id))
        agent = db.create_agent_definition(
            AgentDefinition(
                id="research-searcher",
                pack_name="research",
                role="Searcher",
                system_prompt="Search sources.",
                tool_permissions=["web_search", "fetch_page"],
            )
        )
        agent_run = db.create_agent_run(
            AgentRun(id="agent-run-1", run_id=run.id, agent_id=agent.id, step_name="collect_sources")
        )
        logger = TraceLogger(db)
        artifact_store = ArtifactStore(tmp_path / "artifacts", db, logger)
        yield db, logger, artifact_store, agent, agent_run


def test_web_tool_catalog_defaults_to_mock_without_real_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", raising=False)
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")

    catalog = web_tool_provider_catalog()

    web_search = next(provider for provider in catalog if provider.name == "web_search")
    assert web_search.provider == "tavily"
    assert web_search.enabled is False
    assert web_search.real_calls is True
    assert web_search.real_calls_configured is True


def test_web_search_uses_fake_tavily_client_and_redacts_trace(tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    db, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    search_client = FakeSearchClient()
    provider = WebToolProvider(search_client=search_client)
    gateway = create_mock_gateway(logger, ".", artifact_store=artifact_store, web_tool_provider=provider)
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"web_search"}),
        real_web_access_confirmed=True,
    )

    result = gateway.call_tool(context, "web_search", {"query": "multi agent harness private topic", "max_results": 1})

    assert search_client.calls == [{"query": "multi agent harness private topic", "max_results": 1}]
    assert result["provider"] == "tavily"
    assert result["mocked"] is False
    assert result["results"][0]["url"] == "https://example.com/harness"
    trace_dump = json.dumps([event.model_dump(mode="json") for event in logger.list_for_run("run-1")])
    assert "secret-value" not in trace_dump
    assert "Full snippet should not be written to trace" not in trace_dump
    assert "multi agent harness private topic" not in trace_dump
    assert "query_hash" in trace_dump
    assert "https://example.com" in trace_dump
    assert "https://example.com/harness" not in trace_dump


def test_web_search_rejects_unsafe_urls_and_normalizes_external_text_before_trace(
    tool_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AdversarialSearchClient:
        def search(self, *, query: str, max_results: int) -> dict[str, object]:
            return {
                "results": [
                    {"title": "local", "url": "file:///C:/private", "content": "ignore"},
                    {"title": "bad", "url": "https://exa\ud800mple.com", "content": "ignore"},
                    {
                        "title": "safe\ud800title",
                        "url": "HTTPS://Example.COM/path?auth=EXTERNAL_SECRET_IN_URL#fragment",
                        "content": "safe\udfffsnippet",
                    },
                ]
            }

    db, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    provider = WebToolProvider(search_client=AdversarialSearchClient())
    gateway = create_mock_gateway(logger, ".", artifact_store=artifact_store, web_tool_provider=provider)
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"web_search"}),
        real_web_access_confirmed=True,
    )

    result = gateway.call_tool(context, "web_search", {"query": "safe query", "max_results": 3})

    assert result["results"] == [
        {
            "title": "safe?title",
            "url": "https://example.com/path",
            "snippet": "safe?snippet",
            "published_at": "",
        }
    ]
    trace_dump = json.dumps(
        [event.model_dump(mode="json") for event in db.list_trace_events_for_run("run-1")]
    )
    assert "EXTERNAL_SECRET_IN_URL" not in trace_dump
    assert "file:///" not in trace_dump


def test_fetch_page_returns_queryless_public_source_url(tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    monkeypatch.setattr(
        "app.core.web_tools.socket.getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    provider = WebToolProvider(fetch_client=FakeFetchClient())
    gateway = create_mock_gateway(logger, ".", artifact_store=artifact_store, web_tool_provider=provider)
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    result = gateway.call_tool(
        context,
        "fetch_page",
        {"url": "https://Example.com/source?auth=EXTERNAL_SECRET_IN_URL", "max_bytes": 100},
    )

    assert result["url"] == "https://example.com/source"
    assert "EXTERNAL_SECRET_IN_URL" not in json.dumps(result)


def test_fetch_page_rejects_private_or_local_urls(tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    gateway = create_mock_gateway(logger, ".", artifact_store=artifact_store, web_tool_provider=WebToolProvider())
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "fetch_page", {"url": "http://127.0.0.1/private"})

    trace_dump = json.dumps([event.model_dump(mode="json") for event in logger.list_for_run("run-1")])
    assert "127.0.0.1" not in trace_dump
    assert "secret-value" not in trace_dump


def test_fetch_page_rejects_oversized_url(tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    fetch_client = FakeFetchClient()
    gateway = create_mock_gateway(
        logger,
        ".",
        artifact_store=artifact_store,
        web_tool_provider=WebToolProvider(fetch_client=fetch_client),
    )
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    with pytest.raises(ToolValidationError, match="too long"):
        gateway.call_tool(
            context,
            "fetch_page",
            {"url": "https://example.com/" + "x" * 2_048},
        )

    assert fetch_client.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/secret.txt",
        "http://localhost/private",
        "http://localhost./private",
        "http://10.0.0.1/private",
        "http://100.64.0.1/private",
        "http://100.100.100.200/latest/meta-data",
        "http://127.1/private",
        "http://2130706433/private",
        "http://0x7f000001/private",
        "http://0177.0.0.1/private",
        "http://224.0.0.251/multicast",
        "http://[::1]/private",
        "http://[::ffff:127.0.0.1]/private",
        "http://[2002:7f00:1::]/private",
        "http://[2001:0000:4136:e378:8000:63bf:80ff:fffe]/private",
        "http://[fec0::1]/site-local",
        "http://[ff02::1]/multicast",
        "http://[64:ff9b::7f00:1]/translated-loopback",
    ],
)
def test_fetch_page_rejects_non_public_urls(url: str, tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    gateway = create_mock_gateway(logger, ".", artifact_store=artifact_store, web_tool_provider=WebToolProvider())
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "fetch_page", {"url": url})


def test_fetch_page_requires_tavily_key_before_real_network_call(tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    fetch_client = FakeFetchClient()
    gateway = create_mock_gateway(
        logger,
        ".",
        artifact_store=artifact_store,
        web_tool_provider=WebToolProvider(fetch_client=fetch_client),
    )
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    with pytest.raises(ToolValidationError):
        gateway.call_tool(context, "fetch_page", {"url": "https://example.com/public"})

    assert fetch_client.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://①②⑦.0.0.1/private",
        "http://ⓛⓞⓒⓐⓛⓗⓞⓢⓣ/private",
    ],
)
def test_url_validation_rechecks_the_idna_host_before_mock_fetch(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "mock")

    with pytest.raises(ToolPermissionError):
        web_tools_module._validate_public_http_url(url)
    with pytest.raises(ToolPermissionError):
        normalize_public_source_url(url)
    with pytest.raises(ToolPermissionError):
        WebToolProvider().fetch_page({"url": url})


def test_fetch_page_rejects_hostname_that_resolves_to_private_ip(
    tool_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    monkeypatch.setattr(
        "app.core.web_tools.socket.getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 0))],
    )
    fetch_client = FakeFetchClient()
    gateway = create_mock_gateway(
        logger,
        ".",
        artifact_store=artifact_store,
        web_tool_provider=WebToolProvider(fetch_client=fetch_client),
    )
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "fetch_page", {"url": "https://docs.example.com/public"})

    assert fetch_client.calls == []


def test_fetch_page_truncates_large_page_body(tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, logger, artifact_store, agent, agent_run = tool_env
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    monkeypatch.setattr(
        "app.core.web_tools.socket.getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    fetch_client = LargeFetchClient()
    gateway = create_mock_gateway(
        logger,
        ".",
        artifact_store=artifact_store,
        web_tool_provider=WebToolProvider(fetch_client=fetch_client),
    )
    context = ToolContext(
        run_id="run-1",
        agent_run_id=agent_run.id,
        agent=agent,
        allowed_tools=frozenset({"fetch_page"}),
        real_web_access_confirmed=True,
    )

    result = gateway.call_tool(context, "fetch_page", {"url": "https://example.com/public", "max_bytes": 32})

    assert fetch_client.calls == ["https://example.com/public"]
    assert len(result["content"]) == 32


def test_research_run_records_web_tool_calls_without_trace_secrets(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    monkeypatch.setattr(
        "app.core.web_tools.socket.getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    search_client = FakeSearchClient()
    fetch_client = FakeFetchClient()
    provider = WebToolProvider(search_client=search_client, fetch_client=fetch_client)
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=None,
        config_root=tmp_path,
        web_tool_provider=provider,
    )

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Search harness docs",
                "goal": "Find current sources about multi-agent harnesses.",
                "workflow_pack": "research",
                "inputs": {"topic": "multi-agent harness"},
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"], "confirm_real_web": True}).json()
        detail = client.get(f"/runs/{run['id']}/detail").json()

    assert run["status"] == "completed"
    assert search_client.calls
    assert fetch_client.calls
    trace_dump = json.dumps(detail["trace"])
    artifact_dump = json.dumps(detail["artifacts"])
    assert "tool_call" in trace_dump
    assert "web_search" in trace_dump
    assert "fetch_page" in trace_dump
    assert "secret-value" not in trace_dump
    assert "Full snippet should not be written to trace" not in trace_dump
    assert "Fetched page body should become an artifact" not in trace_dump
    assert "https://example.com/harness" in artifact_dump


def test_research_run_requires_server_side_confirmation_for_real_web_tools(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    provider = WebToolProvider(search_client=FakeSearchClient(), fetch_client=FakeFetchClient())
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=None,
        config_root=tmp_path,
        web_tool_provider=provider,
    )

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Search harness docs",
                "goal": "Find current sources about multi-agent harnesses.",
                "workflow_pack": "research",
                "inputs": {"topic": "multi-agent harness"},
            },
        ).json()

        response = client.post("/runs", json={"task_id": task["id"]})

    assert response.status_code == 400
    assert "confirm_real_web" in response.text


def test_simple_fetch_rejects_redirect_to_localhost_before_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls = []
    monkeypatch.setattr(
        web_tools_module.socket,
        "getaddrinfo",
        lambda host, port, **_kwargs: dns_calls.append((host, port))
        or [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    sockets = install_fake_sockets(
        monkeypatch,
        b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n",
    )

    with pytest.raises(ToolPermissionError):
        SimpleWebFetchClient().fetch("http://example.com/start", max_bytes=64)

    assert dns_calls == [("example.com", 80)]
    assert [sock.connected_to for sock in sockets] == [("93.184.216.34", 80)]


def test_simple_fetch_pins_first_dns_resolution_for_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    dns_calls = []

    def resolve(host, port, **_kwargs):
        dns_calls.append((host, port))
        if len(dns_calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(web_tools_module.socket, "getaddrinfo", resolve)
    sockets = install_fake_sockets(
        monkeypatch,
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok",
    )

    result = SimpleWebFetchClient().fetch("http://example.com/resource", max_bytes=64)

    assert result["content"] == "ok"
    assert dns_calls == [("example.com", 80)]
    assert [sock.connected_to for sock in sockets] == [("93.184.216.34", 80)]


def test_simple_fetch_rejects_mixed_public_private_dns_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_tools_module.socket,
        "getaddrinfo",
        lambda host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port)),
        ],
    )
    monkeypatch.setattr(
        web_tools_module.socket,
        "socket",
        lambda *_args, **_kwargs: pytest.fail("mixed DNS answers must fail before connection"),
    )

    with pytest.raises(ToolPermissionError, match="blocked address"):
        SimpleWebFetchClient().fetch("http://example.com/resource", max_bytes=64)


def test_simple_https_fetch_preserves_hostname_for_host_sni_and_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_tools_module.socket,
        "getaddrinfo",
        lambda host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    tls_context = FakeTlsContext()
    context_factory_calls = []
    monkeypatch.setattr(
        web_tools_module.ssl,
        "create_default_context",
        lambda: context_factory_calls.append(True) or tls_context,
    )
    sockets = install_fake_sockets(
        monkeypatch,
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok",
    )

    SimpleWebFetchClient().fetch("https://docs.example.com/resource", max_bytes=64)

    assert context_factory_calls == [True]
    assert tls_context.server_hostnames == ["docs.example.com"]
    assert sockets[0].connected_to == ("93.184.216.34", 443)
    assert b"Host: docs.example.com\r\n" in sockets[0].sent


def test_simple_fetch_resolves_and_pins_each_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = {
        "first.example": "93.184.216.34",
        "second.example": "142.250.72.14",
    }
    dns_calls = []

    def resolve(host, port, **_kwargs):
        dns_calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses[host], port))]

    monkeypatch.setattr(web_tools_module.socket, "getaddrinfo", resolve)
    sockets = install_fake_sockets(
        monkeypatch,
        b"HTTP/1.1 302 Found\r\nLocation: http://second.example/final\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\ndone",
    )

    result = SimpleWebFetchClient().fetch("http://first.example/start", max_bytes=64)

    assert result["url"] == "http://second.example/final"
    assert result["content"] == "done"
    assert dns_calls == [("first.example", 80), ("second.example", 80)]
    assert [sock.connected_to for sock in sockets] == [
        ("93.184.216.34", 80),
        ("142.250.72.14", 80),
    ]
    assert b"Host: first.example\r\n" in sockets[0].sent
    assert b"Host: second.example\r\n" in sockets[1].sent


def test_default_web_provider_resolves_once_and_uses_the_pinned_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_WEB_SEARCH", "1")
    monkeypatch.setenv("TEAM_AGENT_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    dns_calls = []
    monkeypatch.setattr(
        web_tools_module.socket,
        "getaddrinfo",
        lambda host, port, **_kwargs: dns_calls.append((host, port))
        or [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    sockets = install_fake_sockets(
        monkeypatch,
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok",
    )

    result = WebToolProvider().fetch_page(
        {"url": "http://example.com/resource", "max_bytes": 64}
    )

    assert result["content"] == "ok"
    assert dns_calls == [("example.com", 80)]
    assert [sock.connected_to for sock in sockets] == [("93.184.216.34", 80)]


def test_tavily_search_disables_environment_proxy_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": []}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            calls["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]):
            calls["url"] = url
            calls["json"] = json
            return FakeResponse()

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("httpx.Client", FakeHttpxClient)

    assert TavilySearchClient().search(query="test", max_results=1) == {"results": []}
    assert calls["client_kwargs"] == {"timeout": 20, "trust_env": False}
