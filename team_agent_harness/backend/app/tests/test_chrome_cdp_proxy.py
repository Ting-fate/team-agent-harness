from __future__ import annotations

import importlib.util
import http.client
import json
from pathlib import Path
import socket
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "chrome_cdp_proxy.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("chrome_cdp_proxy", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_find_chrome_path_uses_team_agent_override(tmp_path, monkeypatch) -> None:
    module = load_script_module()
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")

    monkeypatch.setenv("TEAM_AGENT_CHROME_PATH", str(chrome))

    assert module._find_chrome_path() == str(chrome)


def test_managed_chrome_launch_forces_all_network_through_pinned_egress(tmp_path, monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=tmp_path / "profile",
        debug_port=9223,
        startup_timeout=0.1,
    )
    launched = []
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("LITELLM_API_KEY", "litellm-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-secret")
    monkeypatch.setenv("TEAM_AGENT_SAFE_SETTING", "safe-value")

    class FakeEgress:
        running = True
        proxy_url = "http://127.0.0.1:45678"

        def ensure_started(self) -> None:
            return

        def close(self) -> None:
            return

    class FakeProcess:
        def poll(self):
            return None

    state.egress_proxy = FakeEgress()
    state._wait_until_ready = lambda: None
    monkeypatch.setattr(module, "_debug_endpoint_ready", lambda _url: False)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append((args, kwargs)) or FakeProcess(),
    )

    state.ensure_started()

    assert len(launched) == 1
    args, kwargs = launched[0]
    assert "--proxy-server=http://127.0.0.1:45678" in args
    assert "--proxy-bypass-list=<-loopback>" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in args
    assert "--disable-quic" in args
    assert "--disable-features=WebTransport" in args
    assert "--incognito" in args
    assert "--disable-extensions" in args
    assert "--disable-sync" in args
    assert kwargs["env"]["TEAM_AGENT_SAFE_SETTING"] == "safe-value"
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "DEEPSEEK_API_KEY" not in kwargs["env"]
    assert "LITELLM_API_KEY" not in kwargs["env"]
    assert "TAVILY_API_KEY" not in kwargs["env"]


def test_proxy_refuses_unmanaged_chrome_debug_endpoint(tmp_path, monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=tmp_path / "profile",
        debug_port=9223,
        startup_timeout=0.1,
    )
    monkeypatch.setattr(module, "_debug_endpoint_ready", lambda _url: True)

    with pytest.raises(RuntimeError, match="unmanaged Chrome"):
        state.ensure_started()

    assert state.process is None


def test_bridge_binds_listener_before_starting_chrome_and_cleans_state_on_bind_failure(
    tmp_path, monkeypatch
) -> None:
    module = load_script_module()
    events = []

    class FakeState:
        debug_base_url = "http://127.0.0.1:9223"
        profile_dir = tmp_path / "profile"

        def ensure_started(self) -> None:
            events.append("chrome-started")

        def close(self) -> None:
            events.append("state-closed")

    monkeypatch.setattr(
        module,
        "_parse_args",
        lambda: module.argparse.Namespace(
            host="127.0.0.1",
            port=3456,
            chrome_debug_port=9223,
            chrome_path="chrome.exe",
            profile_dir=str(tmp_path / "profile"),
            startup_timeout=1,
        ),
    )
    monkeypatch.setattr(module, "ChromeProxyState", lambda **_kwargs: FakeState())
    monkeypatch.setattr(
        module,
        "ThreadingHTTPServer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("port occupied")),
    )

    with pytest.raises(OSError, match="port occupied"):
        module.main()

    assert events == ["state-closed"]


def test_pinned_egress_connects_to_validated_numeric_address(monkeypatch) -> None:
    module = load_script_module()
    fake_socket = FakeSocket()
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(module.socket, "socket", lambda *_args: fake_socket)

    result = module._connect_public_host("example.com", 443)

    assert result is fake_socket
    assert fake_socket.connected_to == ("93.184.216.34", 443)


def test_pinned_egress_resolves_fake_ip_dns_through_secure_dns_before_connect(monkeypatch) -> None:
    module = load_script_module()
    fake_socket = FakeSocket()
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 443))],
    )
    monkeypatch.setattr(
        module,
        "_resolve_fake_ip_hostname_via_doh",
        lambda _host: ("93.184.216.34",),
    )
    monkeypatch.setattr(module.socket, "socket", lambda *_args: fake_socket)

    module._connect_public_host("example.com", 443)

    assert fake_socket.connected_to == ("93.184.216.34", 443)


def test_pinned_egress_resolves_mixed_fake_ip_dns_through_secure_dns(monkeypatch) -> None:
    module = load_script_module()
    fake_socket = FakeSocket()
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0),
            ),
        ],
    )
    monkeypatch.setattr(
        module,
        "_resolve_fake_ip_hostname_via_doh",
        lambda _host: ("93.184.216.34",),
    )
    monkeypatch.setattr(module.socket, "socket", lambda *_args: fake_socket)

    module._connect_public_host("example.com", 443)

    assert fake_socket.connected_to == ("93.184.216.34", 443)


def test_fake_ip_secure_dns_cache_is_bounded_lru_and_prunes_expired(monkeypatch) -> None:
    module = load_script_module()
    module._DNS_CACHE.clear()
    monkeypatch.setattr(module, "DNS_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(
        module,
        "_doh_query",
        lambda host, record_type: {
            "Answer": [{"type": 1, "data": "93.184.216.34"}]
            if record_type == "A"
            else []
        },
    )

    module._DNS_CACHE["expired.example"] = (0, ("93.184.216.34",))
    module._resolve_fake_ip_hostname_via_doh("first.example")
    module._resolve_fake_ip_hostname_via_doh("second.example")
    module._resolve_fake_ip_hostname_via_doh("first.example")
    module._resolve_fake_ip_hostname_via_doh("third.example")

    assert list(module._DNS_CACHE) == ["first.example", "third.example"]
    assert len(module._DNS_CACHE) == module.DNS_CACHE_MAX_ENTRIES


def test_pinned_egress_rejects_any_private_dns_answer_before_connect(monkeypatch) -> None:
    module = load_script_module()
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    monkeypatch.setattr(
        module.socket,
        "socket",
        lambda *_args: pytest.fail("mixed public/private DNS answers must fail before socket creation"),
    )

    with pytest.raises(RuntimeError, match="local or private"):
        module._connect_public_host("rebind.example", 443)


def test_pinned_egress_proxy_rejects_loopback_connect() -> None:
    module = load_script_module()
    proxy = module._PinnedEgressProxy()
    proxy.ensure_started()
    parsed = module.urlparse(proxy.proxy_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.set_tunnel("127.0.0.1", 443)
    try:
        with pytest.raises(OSError, match="403"):
            connection.connect()
    finally:
        connection.close()
        proxy.close()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_proxy_bind_host_must_be_loopback(host) -> None:
    module = load_script_module()

    with pytest.raises(RuntimeError, match="loopback"):
        module._validate_proxy_bind_host(host)


def test_chrome_proxy_handler_implements_atomic_cdp_bridge_protocol(monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: None
    state.is_ready = lambda: True
    monkeypatch.setattr(module, "_debug_endpoint_ready", lambda _base_url: True)
    monkeypatch.setattr(
        module,
        "_debug_json",
        lambda _base_url, path, method="GET": {"id": "target-1", "url": path, "method": method},
    )
    monkeypatch.setattr(module, "_debug_text", lambda _base_url, path, method="GET": "ok")
    atomic_calls = []
    monkeypatch.setattr(
        module,
        "_navigate_and_evaluate",
        lambda base_url, url, script: atomic_calls.append((base_url, url, script)) or {"value": "ok"},
    )

    handler = module._handler_factory(state)

    assert _call_handler(handler, "GET", "/health") == {
        "status": "ok",
        "connected": True,
        "proxy": "http://127.0.0.1:3456",
        "capabilities": [
            "atomic_navigate_eval_v2",
            "pinned_public_egress_v1",
            "isolated_browser_context_v1",
        ],
    }
    result = _call_handler(
        handler,
        "POST",
        "/navigate-eval?url=https%3A%2F%2Fexample.com",
        body="1 + 1",
    )
    assert result == {"value": "ok"}
    assert atomic_calls == [("http://127.0.0.1:9222", "https://example.com", "1 + 1")]
    status, payload = _call_handler_response(handler, "POST", "/eval?target=target-1", body="1 + 1")
    assert status == 404
    assert payload == {"error": "not_found"}
    assert _call_handler_response(handler, "GET", "/new")[0] == 404
    assert _call_handler_response(handler, "GET", "/close?target=target-1")[0] == 404


def test_shutdown_closes_managed_chrome_before_stopping_proxy_server(tmp_path) -> None:
    module = load_script_module()
    events = []
    server_stopped = threading.Event()

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self) -> None:
            events.append("chrome-terminated")

        def wait(self, timeout: float) -> None:
            events.append(("chrome-waited", timeout))

    class FakeEgress:
        def close(self) -> None:
            events.append("egress-closed")

    class FakeServer:
        def shutdown(self) -> None:
            events.append("server-stopped")
            server_stopped.set()

    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=tmp_path / "profile",
        debug_port=9223,
        startup_timeout=0.1,
    )
    state.process = FakeProcess()
    state.egress_proxy = FakeEgress()

    status, payload = _call_handler_response(
        module._handler_factory(state),
        "POST",
        "/shutdown",
        server=FakeServer(),
    )

    assert status == 200
    assert payload == {"status": "stopping"}
    assert server_stopped.wait(timeout=1)
    assert events[:3] == ["chrome-terminated", ("chrome-waited", 5), "egress-closed"]
    assert events[3:] == ["server-stopped"]


def test_bridge_rejects_browser_requests_without_client_header(monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: pytest.fail("unauthorized request must not reach Chrome")

    status, payload = _call_handler_response(
        module._handler_factory(state),
        "POST",
        "/navigate-eval?url=https%3A%2F%2Fexample.com",
        body="1 + 1",
        authorized=False,
    )

    assert status == 403
    assert payload == {"error": "forbidden"}


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("attacker.example:3456", None),
        ("127.0.0.1:9999", None),
        ("localhost:3456", None),
        ("127.0.0.2:3456", None),
        ("[::1]:3456", None),
        ("127.0.0.1:3456", "https://attacker.example"),
    ],
)
def test_bridge_rejects_rebound_host_wrong_port_and_browser_origin(monkeypatch, host, origin) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: pytest.fail("unauthorized request must not reach Chrome")

    status, payload = _call_handler_response(
        module._handler_factory(state),
        "GET",
        "/health",
        host=host,
        origin=origin,
    )

    assert status == 403
    assert payload == {"error": "forbidden"}


def test_bridge_rejects_duplicate_host_headers(monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: pytest.fail("ambiguous Host must not reach Chrome")

    status, payload = _call_handler_response(
        module._handler_factory(state),
        "GET",
        "/health",
        host=["127.0.0.1:3456", "attacker.example:3456"],
    )

    assert status == 403
    assert payload == {"error": "forbidden"}


@pytest.mark.parametrize("path", ["/new", "/new?url=https%3A%2F%2Fexample.com", "/close?target=target-1"])
def test_legacy_target_endpoints_are_closed(monkeypatch, path) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: None
    monkeypatch.setattr(
        module,
        "_debug_json",
        lambda *_args, **_kwargs: pytest.fail("unsafe legacy navigation must not create a target"),
    )

    status, payload = _call_handler_response(module._handler_factory(state), "GET", path)

    assert status == 404
    assert payload == {"error": "not_found"}


def test_atomic_handler_does_not_expose_blocked_url_in_error(monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: None
    monkeypatch.setattr(
        module,
        "_navigate_and_evaluate",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("blocked http://127.0.0.1/private?token=secret")),
    )

    status, payload = _call_handler_response(
        module._handler_factory(state),
        "POST",
        "/navigate-eval?url=https%3A%2F%2Fexample.com",
        body="1 + 1",
    )

    assert status == 400
    assert payload == {
        "error": "browser_operation_failed",
        "message": "Browser CDP proxy request failed.",
    }
    assert "127.0.0.1" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)


def test_isolation_cleanup_failure_resets_managed_browser(monkeypatch) -> None:
    module = load_script_module()
    state = module.ChromeProxyState(
        chrome_path="chrome.exe",
        profile_dir=Path("profile"),
        debug_port=9222,
        startup_timeout=0.1,
    )
    state.ensure_started = lambda: None
    resets = []
    state.close = lambda: resets.append(True)
    monkeypatch.setattr(
        module,
        "_navigate_and_evaluate",
        lambda *_args: (_ for _ in ()).throw(
            module.BrowserIsolationCleanupError("unconfirmed context cleanup")
        ),
    )

    status, payload = _call_handler_response(
        module._handler_factory(state),
        "POST",
        "/navigate-eval?url=https%3A%2F%2Fexample.com",
        body="1 + 1",
    )

    assert status == 400
    assert payload == {
        "error": "browser_operation_failed",
        "message": "Browser CDP proxy request failed.",
    }
    assert resets == [True]


def test_atomic_navigation_blocks_private_request_after_load_before_eval_completes(monkeypatch) -> None:
    module = load_script_module()
    websocket = FakeWebSocket(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {}},
            {"id": 4, "result": {"loaderId": "loader-1", "frameId": "main-frame"}},
            {"method": "Page.loadEventFired", "params": {}},
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "loader-1"},
            },
            {
                "method": "Fetch.requestPaused",
                "params": {"requestId": "request-1", "request": {"url": "http://127.0.0.1/admin"}},
            },
        ]
    )
    browser_websocket, disposed_contexts = _install_isolated_target_mocks(
        module, monkeypatch, websocket
    )
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    with pytest.raises(RuntimeError, match="blocked local or private request"):
        module._navigate_and_evaluate("http://127.0.0.1:9223", "https://example.com", "document.body.innerText")

    commands = [json.loads(raw) for raw in websocket.sent]
    assert [command["method"] for command in commands] == [
        "Page.enable",
        "Page.setLifecycleEventsEnabled",
        "Fetch.enable",
        "Page.navigate",
        "Runtime.evaluate",
        "Fetch.failRequest",
    ]
    assert websocket.closed is True
    assert browser_websocket.closed is True
    assert disposed_contexts == ["context-1"]


def test_atomic_navigation_and_eval_succeeds_while_interception_is_active(monkeypatch) -> None:
    module = load_script_module()
    websocket = FakeWebSocket(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {}},
            {
                "method": "Fetch.requestPaused",
                "params": {"requestId": "request-1", "request": {"url": "https://example.com/app.js"}},
            },
            {"id": 4, "result": {"loaderId": "loader-1", "frameId": "main-frame"}},
            {"method": "Page.loadEventFired", "params": {}},
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "loader-1"},
            },
            {"id": 6, "result": {"result": {"value": "extracted"}}},
        ]
    )
    browser_websocket, disposed_contexts = _install_isolated_target_mocks(
        module, monkeypatch, websocket
    )
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    result = module._navigate_and_evaluate(
        "http://127.0.0.1:9223",
        "https://example.com",
        "document.body.innerText",
    )

    assert result == {"value": "extracted"}
    commands = [json.loads(raw) for raw in websocket.sent]
    assert [command["method"] for command in commands] == [
        "Page.enable",
        "Page.setLifecycleEventsEnabled",
        "Fetch.enable",
        "Page.navigate",
        "Fetch.continueRequest",
        "Runtime.evaluate",
    ]
    assert commands[-1]["params"]["expression"] == "document.body.innerText"
    assert websocket.closed is True
    assert browser_websocket.closed is True
    assert disposed_contexts == ["context-1"]


def test_stale_loader_event_does_not_start_evaluation(monkeypatch) -> None:
    module = load_script_module()
    websocket = FakeWebSocket(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {}},
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "about-blank-loader"},
            },
            {
                "id": 4,
                "result": {"loaderId": "navigation-loader", "frameId": "main-frame"},
            },
            {
                "method": "Fetch.requestPaused",
                "params": {"requestId": "request-1", "request": {"url": "http://127.0.0.1/private"}},
            },
        ]
    )
    browser_websocket, disposed_contexts = _install_isolated_target_mocks(
        module, monkeypatch, websocket
    )
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    with pytest.raises(RuntimeError, match="blocked local or private request"):
        module._navigate_and_evaluate("http://127.0.0.1:9223", "https://example.com", "1 + 1")

    commands = [json.loads(raw) for raw in websocket.sent]
    assert "Runtime.evaluate" not in [command["method"] for command in commands]
    assert websocket.closed is True
    assert browser_websocket.closed is True
    assert disposed_contexts == ["context-1"]


def test_main_frame_redirect_updates_loader_before_evaluation(monkeypatch) -> None:
    module = load_script_module()
    websocket = FakeWebSocket(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {}},
            {"id": 4, "result": {"loaderId": "loader-1", "frameId": "main-frame"}},
            {
                "method": "Page.frameStartedNavigating",
                "params": {"frameId": "main-frame", "loaderId": "loader-2"},
            },
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "loader-1"},
            },
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "loader-2"},
            },
            {"id": 5, "result": {"result": {"value": "redirected"}}},
        ]
    )
    browser_websocket, disposed_contexts = _install_isolated_target_mocks(
        module, monkeypatch, websocket
    )
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    result = module._navigate_and_evaluate("http://127.0.0.1:9223", "https://example.com", "1 + 1")

    assert result == {"value": "redirected"}
    commands = [json.loads(raw) for raw in websocket.sent]
    assert [command["method"] for command in commands].count("Runtime.evaluate") == 1
    assert websocket.closed is True
    assert browser_websocket.closed is True
    assert disposed_contexts == ["context-1"]


def test_main_frame_navigation_during_evaluation_rejects_stale_result(monkeypatch) -> None:
    module = load_script_module()
    websocket = FakeWebSocket(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {}},
            {"id": 4, "result": {"loaderId": "loader-1", "frameId": "main-frame"}},
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "loader-1", "frameId": "main-frame"},
            },
            {
                "method": "Page.frameStartedNavigating",
                "params": {"frameId": "main-frame", "loaderId": "loader-2"},
            },
            {"id": 5, "result": {"result": {"value": "stale"}}},
        ]
    )
    browser_websocket, disposed_contexts = _install_isolated_target_mocks(
        module, monkeypatch, websocket
    )
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    with pytest.raises(RuntimeError, match="navigated during browser evaluation"):
        module._navigate_and_evaluate("http://127.0.0.1:9223", "https://example.com", "1 + 1")

    assert websocket.closed is True
    assert browser_websocket.closed is True
    assert disposed_contexts == ["context-1"]


def test_isolated_browser_context_owns_target_and_disposes_all_targets(monkeypatch) -> None:
    module = load_script_module()
    browser_websocket = FakeWebSocket(
        [
            {"id": 1, "result": {"browserContextId": "context-1"}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {"targetId": "target-1"}},
            {"id": 4, "result": {}},
            {"id": 5, "result": {"browserContextIds": []}},
            {"id": 6, "result": {"targetInfos": []}},
        ]
    )
    monkeypatch.setattr(
        module,
        "_browser_websocket_url",
        lambda _base_url: "ws://127.0.0.1:9223/devtools/browser/browser-1",
    )
    monkeypatch.setattr(module, "_connect_websocket", lambda _url: browser_websocket)

    websocket, context_id, target_id = module._open_isolated_target("http://127.0.0.1:9223")
    module._dispose_isolated_context(websocket, context_id, target_id)

    assert websocket is browser_websocket
    assert context_id == "context-1"
    assert target_id == "target-1"
    commands = [json.loads(raw) for raw in browser_websocket.sent]
    assert [command["method"] for command in commands] == [
        "Target.createBrowserContext",
        "Browser.setDownloadBehavior",
        "Target.createTarget",
        "Target.disposeBrowserContext",
        "Target.getBrowserContexts",
        "Target.getTargets",
    ]
    assert commands[0]["params"] == {"disposeOnDetach": True}
    assert commands[1]["params"] == {
        "behavior": "deny",
        "browserContextId": "context-1",
    }
    assert commands[2]["params"] == {
        "url": "about:blank",
        "browserContextId": "context-1",
    }


def test_isolated_browser_context_cleanup_rejects_lingering_target() -> None:
    module = load_script_module()
    browser_websocket = FakeWebSocket(
        [
            {"id": 4, "result": {}},
            {
                "id": 5,
                "result": {"browserContextIds": ["context-1"]},
            },
            {
                "id": 6,
                "result": {
                    "targetInfos": [
                        {"targetId": "popup-1", "browserContextId": "context-1"}
                    ]
                },
            },
        ]
    )

    with pytest.raises(RuntimeError, match="remained active"):
        module._dispose_isolated_context(browser_websocket, "context-1", "target-1")


def test_isolated_browser_context_cleanup_rejects_known_target_without_context_id() -> None:
    module = load_script_module()
    browser_websocket = FakeWebSocket(
        [
            {"id": 4, "result": {}},
            {"id": 5, "result": {"browserContextIds": []}},
            {
                "id": 6,
                "result": {"targetInfos": [{"targetId": "target-1"}]},
            },
        ]
    )

    with pytest.raises(RuntimeError, match="remained active"):
        module._dispose_isolated_context(browser_websocket, "context-1", "target-1")


@pytest.mark.parametrize(
    ("contexts_result", "targets_result"),
    [
        ({}, {"targetInfos": []}),
        ({"browserContextIds": []}, {}),
        ({"browserContextIds": [None]}, {"targetInfos": []}),
        ({"browserContextIds": []}, {"targetInfos": [{}]}),
    ],
)
def test_isolated_browser_context_cleanup_rejects_unconfirmed_cdp_results(
    contexts_result,
    targets_result,
) -> None:
    module = load_script_module()
    browser_websocket = FakeWebSocket(
        [
            {"id": 4, "result": {}},
            {"id": 5, "result": contexts_result},
            {"id": 6, "result": targets_result},
        ]
    )

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        module._dispose_isolated_context(browser_websocket, "context-1", "target-1")


def test_unknown_context_ownership_raises_cleanup_error(monkeypatch) -> None:
    module = load_script_module()
    browser_websocket = FakeWebSocket([{"id": 1, "result": {}}])
    monkeypatch.setattr(
        module,
        "_browser_websocket_url",
        lambda _base_url: "ws://127.0.0.1:9223/devtools/browser/browser-1",
    )
    monkeypatch.setattr(module, "_connect_websocket", lambda _url: browser_websocket)

    with pytest.raises(module.BrowserIsolationCleanupError, match="ownership could not be confirmed"):
        module._open_isolated_target("http://127.0.0.1:9223")

    assert browser_websocket.closed is True


def test_failed_explicit_cleanup_reconnects_and_disposes_context(monkeypatch) -> None:
    module = load_script_module()
    recovery_websocket = FakeWebSocket(
        [
            {"id": 1, "result": {"browserContextIds": ["context-1"]}},
            {
                "id": 2,
                "result": {
                    "targetInfos": [
                        {"targetId": "target-1", "browserContextId": "context-1"}
                    ]
                },
            },
            {"id": 3, "result": {}},
            {"id": 4, "result": {"browserContextIds": []}},
            {"id": 5, "result": {"targetInfos": []}},
        ]
    )
    monkeypatch.setattr(
        module,
        "_browser_websocket_url",
        lambda _base_url: "ws://127.0.0.1:9223/devtools/browser/browser-1",
    )
    monkeypatch.setattr(module, "_connect_websocket", lambda _url: recovery_websocket)

    module._recover_or_confirm_isolated_context(
        "http://127.0.0.1:9223",
        "context-1",
        "target-1",
    )

    commands = [json.loads(raw) for raw in recovery_websocket.sent]
    assert [command["method"] for command in commands] == [
        "Target.getBrowserContexts",
        "Target.getTargets",
        "Target.disposeBrowserContext",
        "Target.getBrowserContexts",
        "Target.getTargets",
    ]
    assert recovery_websocket.closed is True


def test_local_cdp_websocket_explicitly_disables_environment_proxy(monkeypatch) -> None:
    module = load_script_module()
    calls = []

    class FakeConnection:
        pass

    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda url, **kwargs: calls.append((url, kwargs)) or FakeConnection(),
    )

    connection = module._connect_websocket("ws://127.0.0.1:9223/devtools/browser/browser-1")

    assert isinstance(connection, FakeConnection)
    assert calls[0][1]["proxy"] is None
    escape_dense_value = json.dumps({"content": "\0" * (256 * 1024)})
    encoded_response = json.dumps(
        {"id": 1, "result": {"result": {"value": escape_dense_value}}}
    ).encode("utf-8")
    assert calls[0][1]["max_size"] >= len(encoded_response)


@pytest.mark.parametrize(
    "websocket_url",
    [
        "ws://attacker.example:9223/devtools/page/target-1",
        "ws://localhost:9223/devtools/page/target-1",
        "ws://127.0.0.2:9223/devtools/page/target-1",
        "ws://127.0.0.1:9999/devtools/page/target-1",
        "ws://127.0.0.1:9223/devtools/page/other-target",
        "ws://127.0.0.1:9223/devtools/page/target-1?token=secret",
    ],
)
def test_target_websocket_url_must_match_local_debug_authority(
    monkeypatch,
    websocket_url,
) -> None:
    module = load_script_module()
    monkeypatch.setattr(
        module,
        "_debug_json",
        lambda _base_url, _path: [
            {
                "id": "target-1",
                "webSocketDebuggerUrl": websocket_url,
            }
        ],
    )

    with pytest.raises(RuntimeError, match="target DevTools websocket"):
        module._target_websocket_url("http://127.0.0.1:9223", "target-1")


@pytest.mark.parametrize(
    "websocket_url",
    [
        "ws://attacker.example:9223/devtools/browser/browser-1",
        "ws://localhost:9223/devtools/browser/browser-1",
        "ws://127.0.0.2:9223/devtools/browser/browser-1",
        "ws://127.0.0.1:9999/devtools/browser/browser-1",
        "ws://user@127.0.0.1:9223/devtools/browser/browser-1",
        "ws://127.0.0.1:9223/devtools/browser/browser-1?token=secret",
        "ws://127.0.0.1:9223/devtools/browser/",
    ],
)
def test_browser_websocket_url_must_match_local_debug_authority(
    monkeypatch,
    websocket_url,
) -> None:
    module = load_script_module()
    monkeypatch.setattr(
        module,
        "_debug_json",
        lambda _base_url, _path: {"webSocketDebuggerUrl": websocket_url},
    )

    with pytest.raises(RuntimeError, match="browser DevTools websocket"):
        module._browser_websocket_url("http://127.0.0.1:9223")


def test_local_cdp_http_explicitly_disables_proxy_and_redirects(monkeypatch) -> None:
    module = load_script_module()
    handlers = []

    class FakeOpener:
        def open(self, request, *, timeout):
            return request, timeout

    monkeypatch.setattr(
        module,
        "build_opener",
        lambda *values: handlers.extend(values) or FakeOpener(),
    )
    request = module.Request("http://127.0.0.1:9223/json/version")

    assert module._open_local_url(request, timeout=2) == (request, 2)
    assert any(type(handler).__name__ == "ProxyHandler" and handler.proxies == {} for handler in handlers)
    assert any(isinstance(handler, module._RejectRedirects) for handler in handlers)
    assert module._RejectRedirects().redirect_request(None, None, 302, "Found", None, "http://evil") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://100.64.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[64:ff9b::a00:1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[2002:7f00:1::]/",
    ],
)
def test_proxy_rejects_private_navigation_url_without_dns_lookup(monkeypatch, url) -> None:
    module = load_script_module()
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("private literal must be rejected before DNS"),
    )

    with pytest.raises(RuntimeError, match="local or private"):
        module._validate_public_http_url(url)


def test_debug_endpoint_ready_requires_chrome_browser(monkeypatch) -> None:
    module = load_script_module()
    monkeypatch.setattr(module, "_debug_json", lambda _base_url, _path: {"Browser": "Microsoft Edge/140.0"})
    assert module._debug_endpoint_ready("http://127.0.0.1:9222") is False

    monkeypatch.setattr(module, "_debug_json", lambda _base_url, _path: {"Browser": "Chrome/140.0"})
    assert module._debug_endpoint_ready("http://127.0.0.1:9223") is True


def test_connect_tunnel_requires_tls_port() -> None:
    module = load_script_module()

    with pytest.raises(RuntimeError, match="port is not allowed"):
        module._parse_connect_authority("example.com:80")

    assert module._parse_connect_authority("example.com:443") == ("example.com", 443)


def test_connect_tunnel_does_not_forward_plaintext_websocket_upgrade() -> None:
    module = load_script_module()
    client = FakeTunnelSocket(b"GET /chat HTTP/1.1\r\nUpgrade: websocket\r\n\r\n")
    upstream = FakeTunnelSocket()

    module._relay_tls_tunnel(client, upstream)

    assert upstream.sent == b""


def test_connect_tunnel_forwards_only_after_tls_client_hello(monkeypatch) -> None:
    module = load_script_module()
    client_hello = _tls_client_hello_record()
    client = FakeTunnelSocket(client_hello)
    upstream = FakeTunnelSocket()
    relayed = []
    monkeypatch.setattr(module, "_relay_tunnel", lambda left, right: relayed.append((left, right)))

    module._relay_tls_tunnel(client, upstream)

    assert upstream.sent == client_hello
    assert relayed == [(client, upstream)]


def test_connect_tunnel_rejects_fake_client_hello_prefix_before_plaintext(monkeypatch) -> None:
    module = load_script_module()
    fake_prefix = b"\x16\x03\x01\x00\x04\x01\x00\x00\x00"
    client = FakeTunnelSocket(fake_prefix + b"GET /chat HTTP/1.1\r\n\r\n")
    upstream = FakeTunnelSocket()
    relayed = []
    monkeypatch.setattr(module, "_relay_tunnel", lambda left, right: relayed.append((left, right)))

    module._relay_tls_tunnel(client, upstream)

    assert upstream.sent == b""
    assert relayed == []


def test_connect_tunnel_rejects_client_hello_larger_than_tls_plaintext_limit(monkeypatch) -> None:
    module = load_script_module()
    oversized_client_hello = _oversized_tls_client_hello_record()
    client = FakeTunnelSocket(oversized_client_hello)
    upstream = FakeTunnelSocket()
    relayed = []
    monkeypatch.setattr(module, "_relay_tunnel", lambda left, right: relayed.append((left, right)))

    module._relay_tls_tunnel(client, upstream)

    assert upstream.sent == b""
    assert relayed == []


def test_connect_tunnel_rejects_duplicate_client_hello_extensions(monkeypatch) -> None:
    module = load_script_module()
    duplicate_extensions = _tls_client_hello_record_with_duplicate_extensions()
    client = FakeTunnelSocket(duplicate_extensions)
    upstream = FakeTunnelSocket()
    relayed = []
    monkeypatch.setattr(module, "_relay_tunnel", lambda left, right: relayed.append((left, right)))

    module._relay_tls_tunnel(client, upstream)

    assert upstream.sent == b""
    assert relayed == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.__setitem__(2, 0),
        lambda record: record.__setitem__(2, 4),
        lambda record: record.__setitem__(10, 4),
        lambda record: record.__setitem__(slice(6, 9), b"\x00\x00\x01"),
        lambda record: record.__setitem__(slice(-2, None), b"\x00\x01"),
    ],
)
def test_connect_tunnel_rejects_malformed_client_hello(monkeypatch, mutate) -> None:
    module = load_script_module()
    malformed = bytearray(_tls_client_hello_record())
    mutate(malformed)
    client = FakeTunnelSocket(bytes(malformed))
    upstream = FakeTunnelSocket()
    relayed = []
    monkeypatch.setattr(module, "_relay_tunnel", lambda left, right: relayed.append((left, right)))

    module._relay_tls_tunnel(client, upstream)

    assert upstream.sent == b""
    assert relayed == []


def _call_handler(handler_class, method: str, path: str, body: str = "") -> dict[str, object]:
    status, payload = _call_handler_response(handler_class, method, path, body)
    assert status == 200
    return payload


def _call_handler_response(
    handler_class,
    method: str,
    path: str,
    body: str = "",
    *,
    authorized: bool = True,
    host: str | list[str] = "127.0.0.1:3456",
    origin: str | None = None,
    server=None,
) -> tuple[int, dict[str, object]]:
    instance = object.__new__(handler_class)
    instance.path = path
    instance.headers = http.client.HTTPMessage()
    instance.headers.add_header("Content-Length", str(len(body.encode("utf-8"))))
    for host_value in [host] if isinstance(host, str) else host:
        instance.headers.add_header("Host", host_value)
    if origin is not None:
        instance.headers.add_header("Origin", origin)
    if authorized:
        instance.headers.add_header("X-Team-Agent-Browser-Proxy", "1")
    instance.rfile = FakeReader(body)
    if server is not None:
        instance.server = server
    instance.send_response = lambda status: setattr(instance, "status", status)
    instance.send_header = lambda _name, _value: None
    instance.end_headers = lambda: None
    instance.wfile = FakeWriter()
    if method == "GET":
        instance.do_GET()
    else:
        instance.do_POST()
    return instance.status, json.loads(instance.wfile.value.decode("utf-8"))


class FakeReader:
    def __init__(self, value: str) -> None:
        self.value = value.encode("utf-8")

    def read(self, _length: int) -> bytes:
        return self.value


class FakeWriter:
    def __init__(self) -> None:
        self.value = b""

    def write(self, value: bytes) -> None:
        self.value += value


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[str] = []
        self.closed = False

    def send(self, value: str) -> None:
        self.sent.append(value)

    def recv(self, timeout: float | None = None) -> str:
        assert timeout is None or timeout > 0
        return self.messages.pop(0)

    def close(self) -> None:
        self.closed = True


def _install_isolated_target_mocks(module, monkeypatch, target_websocket):
    browser_websocket = FakeWebSocket([])
    disposed_contexts = []

    monkeypatch.setattr(
        module,
        "_open_isolated_target",
        lambda _base_url: (browser_websocket, "context-1", "target-1"),
    )
    monkeypatch.setattr(
        module,
        "_target_websocket_url",
        lambda _base_url, _target: "ws://127.0.0.1/devtools/page/target-1",
    )
    monkeypatch.setattr(module, "_connect_websocket", lambda _url: target_websocket)

    def dispose_context(websocket, context_id, target_id) -> None:
        assert websocket is browser_websocket
        assert target_websocket.closed is False
        assert target_id == "target-1"
        disposed_contexts.append(context_id)

    monkeypatch.setattr(module, "_dispose_isolated_context", dispose_context)
    return browser_websocket, disposed_contexts


class FakeSocket:
    def __init__(self) -> None:
        self.timeout = None
        self.connected_to = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address) -> None:
        self.connected_to = address

    def close(self) -> None:
        self.closed = True


class FakeTunnelSocket:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = incoming
        self.sent = b""
        self.timeout = None

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout) -> None:
        self.timeout = timeout

    def recv(self, length: int) -> bytes:
        chunk = self.incoming[:length]
        self.incoming = self.incoming[length:]
        return chunk

    def sendall(self, value: bytes) -> None:
        self.sent += value


def _tls_client_hello_record() -> bytes:
    body = (
        b"\x03\x03"
        + (b"\x00" * 32)
        + b"\x00"
        + b"\x00\x02\x00\x2f"
        + b"\x01\x00"
        + b"\x00\x00"
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def _oversized_tls_client_hello_record() -> bytes:
    record_payload_length = 16_385
    extension_data_length = record_payload_length - 51
    body = (
        b"\x03\x03"
        + (b"\x00" * 32)
        + b"\x00"
        + b"\x00\x02\x00\x2f"
        + b"\x01\x00"
        + (extension_data_length + 4).to_bytes(2, "big")
        + b"\xff\x01"
        + extension_data_length.to_bytes(2, "big")
        + (b"\x00" * extension_data_length)
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    assert len(handshake) == record_payload_length
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def _tls_client_hello_record_with_duplicate_extensions() -> bytes:
    extensions = b"\x00\x17\x00\x00" * 2
    body = (
        b"\x03\x03"
        + (b"\x00" * 32)
        + b"\x00"
        + b"\x00\x02\x00\x2f"
        + b"\x01\x00"
        + len(extensions).to_bytes(2, "big")
        + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake
