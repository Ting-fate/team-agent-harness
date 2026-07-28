import json

from app.core.browser_tools import safe_browser_tool_input_summary
from app.core.web_tools import (
    _safe_url_for_trace,
    safe_tool_input_summary,
    safe_tool_output_summary,
)


def test_trace_url_drops_credentials_query_fragment_and_path_tokens() -> None:
    url = "https://alice:hunter2@example.com/reset/super-secret-token?api_key=secret#private"

    assert _safe_url_for_trace(url) == "https://example.com"
    web_summary = safe_tool_input_summary("fetch_page", {"url": url})
    browser_summary = safe_browser_tool_input_summary("browser_fetch", {"url": url})
    assert set(web_summary) == {"url_hash"}
    assert set(browser_summary) == {"url_hash"}
    assert "example.com" not in json.dumps([web_summary, browser_summary])


def test_trace_url_keeps_non_default_port_without_sensitive_parts() -> None:
    assert _safe_url_for_trace("https://example.com:8443/public?q=ignored") == "https://example.com:8443"


def test_trace_url_never_echoes_invalid_input() -> None:
    assert _safe_url_for_trace("secret-token-without-a-url") == "[invalid-url]"


def test_tool_input_summaries_never_store_query_text() -> None:
    query = "topsecret"

    web_summary = safe_tool_input_summary("web_search", {"query": query, "max_results": 3})
    browser_summary = safe_browser_tool_input_summary(
        "browser_search",
        {"query": query, "max_results": 3},
    )

    assert "query_preview" not in web_summary
    assert "query_preview" not in browser_summary
    assert query not in json.dumps([web_summary, browser_summary])
    assert web_summary["query_length"] == len(query)
    assert browser_summary["query_length"] == len(query)


def test_tool_input_summaries_never_store_invalid_max_results() -> None:
    invalid_value = "https://example.com/private?query=TRACE_SECRET"

    summaries = [
        safe_tool_input_summary(
            "web_search",
            {"query": "safe query", "max_results": invalid_value},
        ),
        safe_browser_tool_input_summary(
            "browser_search",
            {"query": "safe query", "max_results": invalid_value},
        ),
    ]

    assert all(summary["max_results"] == "[invalid]" for summary in summaries)
    assert "TRACE_SECRET" not in json.dumps(summaries)


def test_tool_input_summaries_are_total_for_malformed_unicode_urls() -> None:
    malformed_values = ["http://[::1", "https://exa\ud800mple.com/path"]

    for value in malformed_values:
        assert _safe_url_for_trace(value) == "[invalid-url]"
        web_summary = safe_tool_input_summary("fetch_page", {"url": value})
        browser_summary = safe_browser_tool_input_summary("browser_fetch", {"url": value})
        json.dumps([web_summary, browser_summary])
        assert set(web_summary) == {"url_hash"}
        assert set(browser_summary) == {"url_hash"}


def test_legacy_mock_web_tool_aliases_use_the_canonical_safe_summaries() -> None:
    query = "ALIAS_QUERY_SECRET"
    url = "https://example.com/private/ALIAS_URL_SECRET?token=hidden"

    summaries = [
        safe_tool_input_summary("web_search_mock", {"query": query}),
        safe_tool_output_summary(
            "web_search_mock",
            {"results": [{"title": "Mock result", "query": query}]},
        ),
        safe_tool_input_summary("fetch_page_mock", {"url": url}),
        safe_tool_output_summary(
            "fetch_page_mock",
            {"url": url, "content": "mock page content"},
        ),
    ]

    dump = json.dumps(summaries)
    assert query not in dump
    assert "ALIAS_URL_SECRET" not in dump
    assert "token=hidden" not in dump
    assert summaries[0]["query_length"] == len(query)
    assert set(summaries[2]) == {"url_hash"}
