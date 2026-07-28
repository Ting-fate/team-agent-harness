import json

import pytest

from app.core.model_routing import ModelRoutingError, apply_model_routing_config, load_model_routing_config
from app.core.model_runtime import _safe_provider_error_summary
from app.core.role_cards import RoleCardError, read_role_card
from app.core.skill_library import SkillLibrary
from app.packs.code_rd import get_code_rd_pack


PRIVATE_KEY_TEXT = "\n".join(
    (
        "-----BEGIN " + "PRIVATE KEY-----",
        "not-a-real-private-key",
        "-----END " + "PRIVATE KEY-----",
    )
)


def test_existing_role_card_with_private_key_is_rejected_on_read(tmp_path) -> None:
    roles_dir = tmp_path / "config" / "roles"
    roles_dir.mkdir(parents=True)
    (roles_dir / "legacy.md").write_text(f"# Legacy\n\n{PRIVATE_KEY_TEXT}\n", encoding="utf-8")

    with pytest.raises(RoleCardError, match="must not contain"):
        read_role_card(tmp_path, "legacy")


def test_model_routing_rejects_private_key_in_role_file(tmp_path) -> None:
    role_file = tmp_path / "reviewer.md"
    role_file.write_text(f"# Reviewer\n\n{PRIVATE_KEY_TEXT}\n", encoding="utf-8")
    config_path = tmp_path / "routing.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-reviewer": {
                        "provider": "mock",
                        "model": "mock-model",
                        "role_file": "reviewer.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    routing = load_model_routing_config(config_path)
    pack = get_code_rd_pack()
    with pytest.raises(ModelRoutingError, match="secret-like"):
        apply_model_routing_config({pack.name: pack}, routing)


def test_skill_with_private_key_is_omitted(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "leaky"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: leaky\ndescription: test\n---\n\n# Leaky\n\n{PRIVATE_KEY_TEXT}\n",
        encoding="utf-8",
    )

    detail = next(iter(SkillLibrary.from_roots([skill_root]).skills.values()))

    assert "secret_like_content_omitted" in detail.risk_flags
    assert "PRIVATE KEY" not in detail.content


def test_provider_error_summary_omits_actual_configured_provider_key(monkeypatch) -> None:
    provider_key = "opaque-provider-value-1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", provider_key)

    summary = _safe_provider_error_summary(RuntimeError(f"upstream rejected {provider_key}"))

    assert summary == "classification=provider_error;retryable=false"
    assert provider_key not in summary
