from __future__ import annotations

from app.core.models import AgentDefinition, ArtifactType
from app.packs.base import ContextPolicy, EvalCheck, WorkflowPack, WorkflowStep


RESEARCH_PACK_NAME = "research"


def get_research_pack() -> WorkflowPack:
    return WorkflowPack(
        name=RESEARCH_PACK_NAME,
        description="Mocked knowledge research workflow from planning through reviewed final report.",
        agents=[
            _agent(
                "planner",
                "Planner",
                "Define research questions, scope, assumptions, and evidence standards.",
                ["write_artifact"],
                "mock-research-planner",
            ),
            _agent(
                "searcher",
                "Searcher",
                "Collect candidate sources and record retrieval metadata.",
                ["web_search", "browser_search", "write_artifact"],
                "mock-research-planner",
            ),
            _agent(
                "reader",
                "Reader",
                "Summarize sources and extract relevant facts with source references.",
                ["fetch_page", "browser_fetch", "write_artifact"],
                "mock-research-reader",
            ),
            _agent(
                "verifier",
                "Verifier",
                "Cross-check claims, flag uncertainty, and represent conflicting evidence.",
                ["fetch_page", "browser_fetch", "write_artifact"],
                "mock-research-verifier",
            ),
            _agent(
                "writer",
                "Writer",
                "Draft a research report grounded in the verified evidence.",
                ["write_artifact"],
                "mock-research-writer",
            ),
            _agent(
                "reviewer",
                "Reviewer",
                "Review source coverage, unsupported claims, conflicts, and final clarity.",
                ["write_artifact"],
                "mock-research-verifier",
            ),
        ],
        steps=[
            WorkflowStep(
                name="plan_research",
                agent_role="Planner",
                required_inputs=["goal"],
                allowed_tools=["write_artifact"],
                phase="planning",
                context_policy=ContextPolicy(artifact_excerpt_chars=2000, max_artifacts=2, max_upstream_handoffs=2),
                produces_artifact_type=ArtifactType.DESIGN_DOC.value,
            ),
            WorkflowStep(
                name="collect_sources",
                agent_role="Searcher",
                required_artifacts=[ArtifactType.DESIGN_DOC.value],
                allowed_tools=["web_search", "browser_search", "write_artifact"],
                phase="execution",
                context_policy=ContextPolicy(artifact_excerpt_chars=4000, max_artifacts=2, max_upstream_handoffs=2),
                produces_artifact_type=ArtifactType.SOURCE_SUMMARY.value,
            ),
            WorkflowStep(
                name="read_sources",
                agent_role="Reader",
                required_artifacts=[ArtifactType.SOURCE_SUMMARY.value],
                allowed_tools=["fetch_page", "browser_fetch", "write_artifact"],
                phase="execution",
                context_policy=ContextPolicy(artifact_excerpt_chars=8000, max_artifacts=3, max_upstream_handoffs=3),
                produces_artifact_type=ArtifactType.RESEARCH_NOTE.value,
            ),
            WorkflowStep(
                name="verify_claims",
                agent_role="Verifier",
                required_artifacts=[ArtifactType.RESEARCH_NOTE.value],
                allowed_tools=["fetch_page", "browser_fetch", "write_artifact"],
                phase="review",
                context_policy=ContextPolicy(artifact_excerpt_chars=8000, max_artifacts=4, max_upstream_handoffs=4),
                produces_artifact_type=ArtifactType.TEST_REPORT.value,
            ),
            WorkflowStep(
                name="draft_report",
                agent_role="Writer",
                required_artifacts=[ArtifactType.TEST_REPORT.value],
                allowed_tools=["write_artifact"],
                phase="synthesis",
                context_policy=ContextPolicy(artifact_excerpt_chars=16000, max_artifacts=5, max_upstream_handoffs=5),
                produces_artifact_type=ArtifactType.FINAL_REPORT.value,
            ),
            WorkflowStep(
                name="review_report",
                agent_role="Reviewer",
                required_artifacts=[
                    ArtifactType.DESIGN_DOC.value,
                    ArtifactType.SOURCE_SUMMARY.value,
                    ArtifactType.RESEARCH_NOTE.value,
                    ArtifactType.TEST_REPORT.value,
                    ArtifactType.FINAL_REPORT.value,
                ],
                allowed_tools=["write_artifact"],
                phase="final_review",
                context_policy=ContextPolicy(artifact_excerpt_chars=20000, max_artifacts=6, max_upstream_handoffs=6),
                produces_artifact_type=ArtifactType.RESEARCH_NOTE.value,
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="research_plan_exists",
                description="Planner must produce a research plan artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.DESIGN_DOC.value],
            ),
            EvalCheck(
                name="source_list_exists",
                description="Searcher must produce a source list artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.SOURCE_SUMMARY.value],
            ),
            EvalCheck(
                name="source_notes_exist",
                description="Reader must produce source notes.",
                severity="blocker",
                required_artifact_types=[ArtifactType.RESEARCH_NOTE.value],
            ),
            EvalCheck(
                name="claim_verification_exists",
                description="Verifier must produce a claim-evidence or uncertainty artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.TEST_REPORT.value],
            ),
            EvalCheck(
                name="final_research_report_exists",
                description="Writer must produce a draft research report artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.FINAL_REPORT.value],
            ),
        ],
        final_artifact_type=ArtifactType.FINAL_REPORT.value,
    )


def _agent(
    suffix: str,
    role: str,
    system_prompt: str,
    tool_permissions: list[str],
    model: str,
) -> AgentDefinition:
    return AgentDefinition(
        id=f"{RESEARCH_PACK_NAME}-{suffix}",
        pack_name=RESEARCH_PACK_NAME,
        role=role,
        system_prompt=system_prompt,
        model_config={"provider": "mock", "model": model},
        tool_permissions=tool_permissions,
        runtime_limits={
            "max_steps": 8,
            "max_tool_calls": 16,
            "max_total_tokens": 64_000,
            "timeout_seconds": 900,
            "max_repeated_tool_calls": 2,
            "max_observation_chars": 20_000,
            "max_cost_usd": 10,
        },
    )
