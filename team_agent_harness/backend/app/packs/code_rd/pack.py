from __future__ import annotations

from app.core.models import AgentDefinition, ArtifactType
from app.packs.base import ContextPolicy, EvalCheck, WorkflowPack, WorkflowStep


CODE_RD_PACK_NAME = "code_rd"


def get_code_rd_pack() -> WorkflowPack:
    return WorkflowPack(
        name=CODE_RD_PACK_NAME,
        description="Mocked code R&D workflow from clarification through final delivery.",
        agents=[
            _agent(
                "clarifier",
                "Clarifier",
                "Turn the task request into explicit requirements, constraints, and acceptance criteria.",
                ["read_file", "list_files", "search_files"],
                "mock-code-planner",
            ),
            _agent(
                "architect",
                "Architect",
                "Design the implementation approach and identify affected modules.",
                ["read_file", "list_files", "search_files"],
                "mock-code-planner",
            ),
            _agent(
                "coder",
                "Coder",
                "Prepare the implementation patch or changed-file summary.",
                ["read_file", "list_files", "search_files", "write_artifact"],
                "mock-code-builder",
            ),
            _agent(
                "tester",
                "Tester",
                "Run or mock relevant tests and report results with residual risk.",
                ["read_file", "run_test_command", "write_artifact"],
                "mock-code-builder",
            ),
            _agent(
                "reviewer",
                "Reviewer",
                "Review correctness, maintainability, security, performance, and test coverage.",
                ["read_file", "search_files", "write_artifact"],
                "mock-code-reviewer",
            ),
            _agent(
                "finalizer",
                "Finalizer",
                "Summarize the delivery, test state, review outcome, and remaining risk.",
                ["write_artifact"],
                "mock-code-reviewer",
            ),
        ],
        steps=[
            WorkflowStep(
                name="clarify_requirements",
                agent_role="Clarifier",
                required_inputs=["goal"],
                allowed_tools=["read_file", "list_files", "search_files"],
                phase="intake",
                context_policy=ContextPolicy(artifact_excerpt_chars=2000, max_artifacts=2, max_upstream_handoffs=2),
                produces_artifact_type=ArtifactType.SOURCE_SUMMARY.value,
            ),
            WorkflowStep(
                name="design_implementation",
                agent_role="Architect",
                required_artifacts=[ArtifactType.SOURCE_SUMMARY.value],
                allowed_tools=["read_file", "list_files", "search_files"],
                phase="planning",
                context_policy=ContextPolicy(artifact_excerpt_chars=8000, max_artifacts=3, max_upstream_handoffs=3),
                produces_artifact_type=ArtifactType.DESIGN_DOC.value,
            ),
            WorkflowStep(
                name="prepare_patch",
                agent_role="Coder",
                required_artifacts=[ArtifactType.DESIGN_DOC.value],
                allowed_tools=["read_file", "list_files", "search_files", "write_artifact"],
                phase="execution",
                context_policy=ContextPolicy(artifact_excerpt_chars=12000, max_artifacts=4, max_upstream_handoffs=4),
                produces_artifact_type=ArtifactType.PATCH.value,
            ),
            WorkflowStep(
                name="test_changes",
                agent_role="Tester",
                required_artifacts=[ArtifactType.PATCH.value],
                allowed_tools=["read_file", "run_test_command", "write_artifact"],
                phase="verification",
                context_policy=ContextPolicy(artifact_excerpt_chars=12000, max_artifacts=4, max_upstream_handoffs=4),
                produces_artifact_type=ArtifactType.TEST_REPORT.value,
            ),
            WorkflowStep(
                name="review_delivery",
                agent_role="Reviewer",
                required_artifacts=[ArtifactType.TEST_REPORT.value],
                allowed_tools=["read_file", "search_files", "write_artifact"],
                phase="review",
                context_policy=ContextPolicy(artifact_excerpt_chars=16000, max_artifacts=5, max_upstream_handoffs=5),
                produces_artifact_type=ArtifactType.RESEARCH_NOTE.value,
            ),
            WorkflowStep(
                name="finalize_delivery",
                agent_role="Finalizer",
                required_artifacts=[
                    ArtifactType.SOURCE_SUMMARY.value,
                    ArtifactType.DESIGN_DOC.value,
                    ArtifactType.PATCH.value,
                    ArtifactType.TEST_REPORT.value,
                    ArtifactType.RESEARCH_NOTE.value,
                ],
                allowed_tools=["write_artifact"],
                phase="synthesis",
                context_policy=ContextPolicy(artifact_excerpt_chars=16000, max_artifacts=6, max_upstream_handoffs=6),
                produces_artifact_type=ArtifactType.FINAL_REPORT.value,
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="requirements_summary_exists",
                description="Clarification must produce a requirements summary artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.SOURCE_SUMMARY.value],
            ),
            EvalCheck(
                name="implementation_design_exists",
                description="Architecture must produce an implementation design artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.DESIGN_DOC.value],
            ),
            EvalCheck(
                name="patch_summary_exists",
                description="Coding must produce a patch or changed-file summary artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.PATCH.value],
            ),
            EvalCheck(
                name="test_report_exists",
                description="Testing must produce a test report artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.TEST_REPORT.value],
            ),
            EvalCheck(
                name="review_report_exists",
                description="Review must produce a review report artifact.",
                severity="blocker",
                required_artifact_types=[ArtifactType.RESEARCH_NOTE.value],
            ),
            EvalCheck(
                name="final_delivery_summary_exists",
                description="Finalizer must produce a final delivery summary artifact.",
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
        id=f"{CODE_RD_PACK_NAME}-{suffix}",
        pack_name=CODE_RD_PACK_NAME,
        role=role,
        system_prompt=system_prompt,
        model_config={"provider": "mock", "model": model},
        tool_permissions=tool_permissions,
        runtime_limits={"max_steps": 1},
    )
