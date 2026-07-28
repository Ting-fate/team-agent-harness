import json
from pathlib import Path

import pytest

from app.core.model_routing import (
    ModelRoutingError,
    apply_model_routing_config,
    load_model_routing_config,
)
from app.packs.code_rd import get_code_rd_pack
from app.packs.code_rd_institutional import get_code_rd_institutional_pack


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_missing_model_routing_config_keeps_pack_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)

    routing = load_model_routing_config()
    pack = get_code_rd_pack()
    packs = apply_model_routing_config({pack.name: pack}, routing)

    assert routing.agents == {}
    assert {agent.model_settings["provider"] for agent in packs[pack.name].agents} == {"mock"}


def test_model_routing_overrides_agent_model_config_with_real_call_opt_in(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    path = tmp_path / "model-routing.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd_institutional-implementation_executor": {
                        "provider": "litellm_proxy",
                        "model": "gpt5.5",
                        "temperature": 0.1,
                        "max_tokens": 1200,
                        "reasoning_effort": "xhigh",
                        "allow_real_calls": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    pack = get_code_rd_institutional_pack()
    routing = load_model_routing_config(path)
    packs = apply_model_routing_config({pack.name: pack}, routing)
    routed_agent = next(
        agent for agent in packs[pack.name].agents if agent.id == "code_rd_institutional-implementation_executor"
    )

    assert routed_agent.model_settings == {
        "provider": "litellm_proxy",
        "model": "gpt5.5",
        "temperature": 0.1,
        "max_tokens": 1200,
        "reasoning_effort": "xhigh",
    }
    assert "allow_real_calls" not in routed_agent.model_settings


def test_model_routing_defaults_real_provider_reasoning_effort_to_xhigh(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    path = tmp_path / "model-routing.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-architect": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "allow_real_calls": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    pack = get_code_rd_pack()
    routing = load_model_routing_config(path)
    packs = apply_model_routing_config({pack.name: pack}, routing)
    routed_agent = next(agent for agent in packs[pack.name].agents if agent.id == "code_rd-architect")

    assert routed_agent.model_settings["reasoning_effort"] == "xhigh"


def test_checked_in_litellm_routes_use_gpt55_except_long_context_review_roles() -> None:
    expected_deepseek_agents = {
        "code_rd-architect",
        "code_rd_institutional-context_reader",
        "code_rd_institutional-context_reviewer",
        "code_rd_institutional-final_reviewer",
        "code_rd_institutional-review_gate",
        "research-reader",
    }
    for relative_path in [
        "config/model-routing.local.json",
        "config/model-routing.litellm.example.json",
    ]:
        routing = json.loads((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
        agents = routing["agents"]
        deepseek_agents = {
            agent_id
            for agent_id, route in agents.items()
            if route["provider"] == "litellm_proxy" and route["model"] == "deepseek-v4-pro"
        }
        assert deepseek_agents <= expected_deepseek_agents
        for route in agents.values():
            if route["provider"] == "litellm_proxy":
                assert route["reasoning_effort"] == "xhigh"
                assert route["model"] in {"gpt5.5", "deepseek-v4-pro"}


def test_model_routing_can_apply_role_file_without_leaking_it_to_model_config(tmp_path) -> None:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    role_file = roles_dir / "code-reviewer.md"
    role_file.write_text(
        "---\nname: Code Reviewer\n---\n\n# Code Reviewer Agent\n\nReview correctness and security.",
        encoding="utf-8",
    )
    path = tmp_path / "model-routing.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "mock-routed-reviewer",
                        "role_file": "roles/code-reviewer.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    pack = get_code_rd_pack()
    routing = load_model_routing_config(path)
    packs = apply_model_routing_config({pack.name: pack}, routing)
    reviewer = next(agent for agent in packs[pack.name].agents if agent.id == "code_rd-reviewer")

    assert reviewer.system_prompt.startswith("# Code Reviewer Agent")
    assert "Review correctness and security." in reviewer.system_prompt
    assert reviewer.model_settings == {
        "provider": "mock",
        "model": "mock-routed-reviewer",
    }
    assert "role_file" not in reviewer.model_settings


def test_model_routing_rejects_invalid_role_files(tmp_path) -> None:
    missing_path = tmp_path / "missing-role.json"
    missing_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "x",
                        "role_file": "roles/missing.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="role_file"):
        load_model_routing_config(missing_path)

    non_markdown = tmp_path / "role.txt"
    non_markdown.write_text("not markdown", encoding="utf-8")
    non_markdown_path = tmp_path / "non-markdown-role.json"
    non_markdown_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "x",
                        "role_file": "role.txt",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match=".md"):
        load_model_routing_config(non_markdown_path)

    outside_role = tmp_path.parent / "outside-role.md"
    outside_role.write_text("Outside role", encoding="utf-8")
    outside_path = tmp_path / "outside-role.json"
    outside_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "x",
                        "role_file": "../outside-role.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(ModelRoutingError, match="role_file"):
            load_model_routing_config(outside_path)
    finally:
        outside_role.unlink(missing_ok=True)


def test_model_routing_rejects_role_file_symlink_escape_large_file_and_empty_prompt(tmp_path) -> None:
    outside_role = tmp_path.parent / "outside-symlink-role.md"
    outside_role.write_text("Outside role", encoding="utf-8")
    symlink_role = tmp_path / "symlink-role.md"
    symlink_created = False
    try:
        symlink_role.symlink_to(outside_role)
        symlink_created = True
    except OSError:
        pass

    if symlink_created:
        symlink_path = tmp_path / "symlink-role.json"
        symlink_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "code_rd-reviewer": {
                            "provider": "mock",
                            "model": "x",
                            "role_file": "symlink-role.md",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ModelRoutingError, match="role_file"):
            load_model_routing_config(symlink_path)

    large_role = tmp_path / "large-role.md"
    large_role.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    large_path = tmp_path / "large-role.json"
    large_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "x",
                        "role_file": "large-role.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="too large"):
        apply_model_routing_config({get_code_rd_pack().name: get_code_rd_pack()}, load_model_routing_config(large_path))

    empty_role = tmp_path / "empty-role.md"
    empty_role.write_text("---\nname: Empty\n---\n\n", encoding="utf-8")
    empty_path = tmp_path / "empty-role.json"
    empty_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "x",
                        "role_file": "empty-role.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="empty"):
        apply_model_routing_config({get_code_rd_pack().name: get_code_rd_pack()}, load_model_routing_config(empty_path))

    outside_role.unlink(missing_ok=True)


def test_model_routing_rejects_real_provider_without_global_opt_in(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    path = tmp_path / "model-routing.json"
    path.write_text(
        json.dumps({"agents": {"code_rd-coder": {"provider": "deepseek", "model": "deepseek-v4-pro"}}}),
        encoding="utf-8",
    )

    routing = load_model_routing_config(path)
    pack = get_code_rd_pack()

    with pytest.raises(ModelRoutingError, match="TEAM_AGENT_ALLOW_REAL_MODEL_CALLS"):
        apply_model_routing_config({pack.name: pack}, routing)


def test_model_routing_rejects_real_provider_without_credentials(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "model-routing.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "openai",
                        "model": "gpt-reviewer",
                        "allow_real_calls": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    routing = load_model_routing_config(path)
    pack = get_code_rd_pack()

    with pytest.raises(ModelRoutingError, match="OPENAI_API_KEY"):
        apply_model_routing_config({pack.name: pack}, routing)


def test_model_routing_rejects_real_provider_without_per_agent_allow_real_calls(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    path = tmp_path / "model-routing.json"
    path.write_text(
        json.dumps({"agents": {"code_rd-reviewer": {"provider": "openai", "model": "gpt-reviewer"}}}),
        encoding="utf-8",
    )

    routing = load_model_routing_config(path)
    pack = get_code_rd_pack()

    with pytest.raises(ModelRoutingError, match="allow_real_calls"):
        apply_model_routing_config({pack.name: pack}, routing)


def test_model_routing_rejects_unknown_agent_provider_sensitive_fields_and_numeric_bounds(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    pack = get_code_rd_pack()

    unknown_agent_path = tmp_path / "unknown-agent.json"
    unknown_agent_path.write_text(
        json.dumps({"agents": {"missing-agent": {"provider": "mock", "model": "mock-model"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="unknown agents"):
        apply_model_routing_config({pack.name: pack}, load_model_routing_config(unknown_agent_path))

    unsupported_provider_path = tmp_path / "bad-provider.json"
    unsupported_provider_path.write_text(
        json.dumps({"agents": {"code_rd-coder": {"provider": "anthropic", "model": "claude-placeholder"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="invalid"):
        load_model_routing_config(unsupported_provider_path)

    sensitive_path = tmp_path / "secret.json"
    sensitive_path.write_text(
        json.dumps({"agents": {"code_rd-coder": {"provider": "mock", "model": "x", "api_key": "secret"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="sensitive field"):
        load_model_routing_config(sensitive_path)

    bad_temperature_path = tmp_path / "bad-temperature.json"
    bad_temperature_path.write_text(
        json.dumps({"agents": {"code_rd-coder": {"provider": "mock", "model": "x", "temperature": 99}}}),
        encoding="utf-8",
    )
    with pytest.raises(ModelRoutingError, match="invalid"):
        load_model_routing_config(bad_temperature_path)
