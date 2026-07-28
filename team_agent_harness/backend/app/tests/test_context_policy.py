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


def test_active_research_routes_use_bounded_output_budgets() -> None:
    config = json.loads((PROJECT_ROOT / "config/model-routing.local.json").read_text(encoding="utf-8"))

    assert {
        agent_id: config["agents"][agent_id]["max_tokens"]
        for agent_id in (
            "research-planner",
            "research-searcher",
            "research-reader",
            "research-verifier",
            "research-writer",
            "research-reviewer",
        )
    } == {
        "research-planner": 1000,
        "research-searcher": 256,
        "research-reader": 4096,
        "research-verifier": 256,
        "research-writer": 700,
        "research-reviewer": 400,
    }
