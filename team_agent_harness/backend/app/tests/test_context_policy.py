import json
from pathlib import Path

from app.packs.code_rd import get_code_rd_pack
from app.packs.code_rd_institutional import get_code_rd_institutional_pack
from app.packs.research import get_research_pack


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_workflow_packs_define_bounded_context_budgets_by_position() -> None:
    expected = {
        "code_rd": [
            ("clarify_requirements", 2000, 2, 2),
            ("design_implementation", 8000, 3, 3),
            ("prepare_patch", 12000, 4, 4),
            ("test_changes", 12000, 4, 4),
            ("review_delivery", 16000, 5, 5),
            ("finalize_delivery", 16000, 6, 6),
        ],
        "research": [
            ("plan_research", 2000, 2, 2),
            ("collect_sources", 4000, 2, 2),
            ("read_sources", 8000, 3, 3),
            ("verify_claims", 8000, 4, 4),
            ("draft_report", 16000, 5, 5),
            ("review_report", 20000, 6, 6),
        ],
        "code_rd_institutional": [
            ("read_context", 4000, 2, 2),
            ("plan_delivery", 12000, 4, 4),
            ("review_plan", 20000, 5, 5),
            ("dispatch_work", 8000, 5, 5),
            ("prepare_patch", 16000, 6, 4),
            ("test_changes", 16000, 6, 4),
            ("review_context_alignment", 24000, 8, 6),
            ("synthesize_delivery", 20000, 8, 6),
            ("final_review", 24000, 8, 6),
            ("final_approval", 20000, 10, 8),
        ],
    }
    packs = [get_code_rd_pack(), get_research_pack(), get_code_rd_institutional_pack()]

    for pack in packs:
        actual = [
            (
                step.name,
                step.context_policy.artifact_excerpt_chars,
                step.context_policy.max_artifacts,
                step.context_policy.max_upstream_handoffs,
            )
            for step in pack.steps
        ]
        assert actual == expected[pack.name]


def test_active_routes_use_bounded_output_budgets() -> None:
    checked_in_expected = {
        "code_rd_institutional-context_reader": 4096,
        "code_rd_institutional-planner": 700,
        "code_rd_institutional-review_gate": 4096,
        "code_rd_institutional-dispatcher": 500,
        "code_rd_institutional-implementation_executor": 1000,
        "code_rd_institutional-test_executor": 800,
        "code_rd_institutional-context_reviewer": 4096,
        "code_rd_institutional-synthesizer": 700,
        "code_rd_institutional-final_reviewer": 4096,
        "code_rd_institutional-final_approver": 500,
    }
    checked_in_config = json.loads(
        (PROJECT_ROOT / "config/model-routing.litellm.example.json").read_text(encoding="utf-8")
    )
    assert {
        agent_id: checked_in_config["agents"][agent_id]["max_tokens"]
        for agent_id in checked_in_expected
    } == checked_in_expected

    local_routing_path = PROJECT_ROOT / "config/model-routing.local.json"
    if not local_routing_path.is_file():
        return

    local_config = json.loads(local_routing_path.read_text(encoding="utf-8"))
    local_expected = {
        "research-planner": 1000,
        "research-searcher": 4096,
        "research-reader": 4096,
        "research-verifier": 4096,
        "research-writer": 700,
        "research-reviewer": 400,
    }
    assert {
        agent_id: local_config["agents"][agent_id]["max_tokens"] for agent_id in local_expected
    } == local_expected


def test_builtin_agent_runtime_token_caps_are_100k() -> None:
    packs = [get_code_rd_pack(), get_research_pack(), get_code_rd_institutional_pack()]
    for pack in packs:
        assert {
            agent.runtime_limits["max_total_tokens"]
            for agent in pack.agents
        } == {64_000}
