from dataclasses import replace
from pathlib import Path

import pytest

from app.core.agent_loop import (
    AgentLoopError,
    AgentLoopExecutor,
    _estimated_request_input_tokens,
)
from app.core.artifacts import ArtifactStore
from app.core.model_runtime import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRuntimeError,
    ModelToolCall,
    ModelToolDefinition,
)
from app.core.model_capabilities import CapabilityRegistry, ModelCapability
from app.core.models import AgentDefinition, AgentRun, Run, Task, TraceEventType
from app.core.storage import SQLiteStorage
from app.core.tool_gateway import create_mock_gateway
from app.core.trace import TraceLogger
from app.packs.base import AgentLoopPolicy, WorkflowStep


class ScriptedAdapter:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _loop_env(tmp_path: Path, responses: list[ModelResponse], *, approved_tools: list[str] | None = None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("loop evidence\n", encoding="utf-8")
    storage = SQLiteStorage(tmp_path / "harness.sqlite3")
    storage.connect()
    storage.init_schema()
    logger = TraceLogger(storage)
    task = storage.create_task(Task(id="task-1", title="Task", goal="Inspect", workflow_pack="research"))
    run = storage.create_run(
        Run(id="run-1", task_id=task.id, approved_side_effect_tools=approved_tools or [])
    )
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="research",
            role="Reader",
            system_prompt="Inspect evidence.",
            tool_permissions=["read_file", "run_test_command"],
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(id="agent-run-1", run_id=run.id, agent_id=agent.id, step_name="inspect")
    )
    step = WorkflowStep(
        name="inspect",
        agent_role=agent.role,
        allowed_tools=["read_file", "run_test_command"],
        produces_artifact_type="final_report",
        agent_loop=AgentLoopPolicy(enabled=True, max_steps=3, max_tool_calls=3),
    )
    adapter = ScriptedAdapter(responses)
    model_gateway = ModelGateway(
        adapters={"scripted": adapter},
        capability_registry=CapabilityRegistry(
            [
                ModelCapability(
                    provider="scripted",
                    model_pattern="*",
                    protocol="test",
                    supports_tools=True,
                )
            ]
        ),
    )
    artifact_store = ArtifactStore(tmp_path / "artifacts", storage, logger)
    tool_gateway = create_mock_gateway(logger, workspace, artifact_store=artifact_store)
    executor = AgentLoopExecutor(
        model_gateway=model_gateway,
        tool_gateway=tool_gateway,
        trace_logger=logger,
    )
    request = ModelRequest(
        provider="scripted",
        model="scripted-model",
        system_prompt=agent.system_prompt,
        messages=[],
        metadata={"agent_run_id": agent_run.id},
    )
    return storage, logger, task, run, step, agent, adapter, executor, request


def _request_with_loop_tools(
    executor: AgentLoopExecutor,
    step: WorkflowStep,
    agent: AgentDefinition,
    request: ModelRequest,
) -> ModelRequest:
    allowed_tools = frozenset(step.allowed_tools) & frozenset(agent.tool_permissions)
    tools = [
        ModelToolDefinition(
            name=spec["name"],
            description=spec["description"],
            input_schema=spec["input_schema"],
        )
        for spec in executor.tool_gateway.model_tool_specs(allowed_tools)
    ]
    return replace(request, tools=tools)


def test_agent_loop_calls_typed_tool_then_revises_with_observation(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="",
            usage={"input_tokens": 4, "output_tokens": 1},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        ),
        ModelResponse(
            text="The repository evidence was inspected.",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, logger, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.text == "The repository evidence was inspected."
        assert result.stop_reason == "finished"
        assert result.tool_call_count == 1
        assert len(result.interactions) == 2
        assert [tool.name for tool in adapter.requests[0].tools] == ["read_file", "run_test_command"]
        assert adapter.requests[1].messages[-1].role == "tool"
        assert "untrusted_tool_output" in adapter.requests[1].messages[-1].content
        assert "loop evidence" in adapter.requests[1].messages[-1].content
        event_types = [event.event_type for event in logger.list_for_run(run.id)]
        assert TraceEventType.TOOL_CALL in event_types
        assert TraceEventType.TOOL_RESULT in event_types
    finally:
        storage.close()


def test_agent_loop_blocks_unapproved_side_effect_and_lets_model_revise(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="",
            usage={"input_tokens": 4, "output_tokens": 1},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[
                ModelToolCall(id="call-1", name="run_test_command", arguments={"command": "pytest -q"})
            ],
        ),
        ModelResponse(
            text="Tests were not executed because approval was absent.",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.stop_reason == "finished"
        assert "ToolPermissionError" in adapter.requests[1].messages[-1].content
        assert "approval" in adapter.requests[1].messages[-1].content
    finally:
        storage.close()


def test_agent_loop_returns_best_text_when_token_budget_is_exhausted(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="Partial but useful result.",
            usage={"total_tokens": 800},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        )
    ]
    storage, logger, task, run, step, agent, _, executor, request = _loop_env(tmp_path, responses)
    step = step.model_copy(
        update={"agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": 700})}
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.text == "Partial but useful result."
        assert result.budget_exhausted is True
        assert result.stop_reason == "token_budget"
        assert not any(
            event.event_type == TraceEventType.TOOL_CALL
            for event in logger.list_for_run(run.id)
        )
    finally:
        storage.close()


def test_agent_loop_marks_direct_final_response_that_exceeds_budget_as_exhausted(
    tmp_path: Path,
) -> None:
    responses = [
        ModelResponse(
            text="Final response that used too many tokens.",
            usage={"total_tokens": 800},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        )
    ]
    storage, _, task, run, step, agent, _, executor, request = _loop_env(tmp_path, responses)
    step = step.model_copy(
        update={"agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": 700})}
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.text == "Final response that used too many tokens."
        assert result.stop_reason == "token_budget"
        assert result.budget_exhausted is True
    finally:
        storage.close()


def test_agent_loop_preserves_repetition_budget_state_after_finalization(tmp_path: Path) -> None:
    repeated_call = lambda call_id: ModelToolCall(
        id=call_id,
        name="read_file",
        arguments={"path": "README.md"},
    )
    responses = [
        ModelResponse(
            text="",
            usage={"input_tokens": 4, "output_tokens": 1},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[repeated_call("call-1"), repeated_call("call-2"), repeated_call("call-3")],
        ),
        ModelResponse(
            text="Best result after the repeated call was blocked.",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, logger, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_tool_calls": 5, "max_repeated_tool_calls": 1}
            )
        }
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.stop_reason == "repetition_budget"
        assert result.budget_exhausted is True
        assert result.tool_call_count == 3
        assert not adapter.requests[1].tools
        assert sum(
            event.event_type == TraceEventType.TOOL_CALL
            for event in logger.list_for_run(run.id)
        ) == 1
    finally:
        storage.close()


def test_agent_loop_fails_when_budget_exhausts_without_usable_result(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="",
            usage={"total_tokens": 800},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        )
    ]
    storage, _, task, run, step, agent, _, executor, request = _loop_env(tmp_path, responses)
    step = step.model_copy(
        update={"agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": 700})}
    )
    try:
        with pytest.raises(AgentLoopError, match="without a usable result"):
            executor.execute(task=task, run=run, step=step, agent=agent, request=request)
    finally:
        storage.close()


def test_agent_loop_caps_each_request_to_remaining_token_budget(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="",
            usage={"total_tokens": 10},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        ),
        ModelResponse(
            text="Finished within the remaining budget.",
            usage={"total_tokens": 5},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    initial_estimated_input = _estimated_request_input_tokens(
        _request_with_loop_tools(executor, step, agent, request)
    )
    step = step.model_copy(
        update={"agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": 2_000})}
    )
    request = replace(request, max_tokens=5_000)
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.stop_reason == "finished"
        assert [item.max_tokens for item in adapter.requests] == [
            2_000 - initial_estimated_input,
            2_000 - 10 - _estimated_request_input_tokens(adapter.requests[1]),
        ]
    finally:
        storage.close()


def test_agent_loop_rejects_request_when_estimated_input_exhausts_token_budget(
    tmp_path: Path,
) -> None:
    responses = [
        ModelResponse(
            text="This response must never be requested.",
            usage={"input_tokens": 1, "output_tokens": 1},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        )
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(
        tmp_path,
        responses,
    )
    step = step.model_copy(
        update={"agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": 100})}
    )
    request = replace(
        request,
        system_prompt="system " * 128,
        messages=[ModelMessage(role="user", content="prompt " * 128)],
        max_tokens=10,
    )
    try:
        with pytest.raises(AgentLoopError, match="token budget"):
            executor.execute(task=task, run=run, step=step, agent=agent, request=request)
        assert adapter.requests == []
    finally:
        storage.close()


def test_agent_loop_reserves_estimated_input_before_capping_output_tokens(
    tmp_path: Path,
) -> None:
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, [])
    prepared_request = _request_with_loop_tools(executor, step, agent, request)
    estimated_input_tokens = _estimated_request_input_tokens(prepared_request)
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_total_tokens": estimated_input_tokens + 7}
            )
        }
    )
    adapter.responses.append(
        ModelResponse(
            text="Finished inside the reserved boundary.",
            usage={"input_tokens": estimated_input_tokens, "output_tokens": 6},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        )
    )
    request = replace(request, max_tokens=100)
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.stop_reason == "finished"
        assert adapter.requests[0].max_tokens == 7
    finally:
        storage.close()


def test_agent_loop_reserves_each_round_input_after_cumulative_actual_usage(
    tmp_path: Path,
) -> None:
    first_usage = 11
    responses = [
        ModelResponse(
            text="Partial result before the second round.",
            usage={"input_tokens": 5, "output_tokens": 6},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        ),
        ModelResponse(
            text="Finished after cumulative budgeting.",
            usage={"input_tokens": 500, "output_tokens": 1},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    total_budget = 2_000
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": total_budget})
        }
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        second_request = adapter.requests[1]
        expected_second_output = (
            total_budget
            - first_usage
            - _estimated_request_input_tokens(second_request)
        )
        assert expected_second_output > 0
        assert second_request.max_tokens == expected_second_output
        assert result.stop_reason == "finished"
    finally:
        storage.close()


def test_agent_loop_fails_closed_when_provider_omits_usage(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="Partial result before an unmetered tool request.",
            usage={},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        ),
        ModelResponse(
            text="This response must never be requested.",
            usage={"total_tokens": 1},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, logger, task, run, step, agent, adapter, executor, request = _loop_env(
        tmp_path,
        responses,
    )
    step = step.model_copy(
        update={"agent_loop": step.agent_loop.model_copy(update={"max_total_tokens": 700})}
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.text == "Partial result before an unmetered tool request."
        assert result.stop_reason == "token_budget"
        assert result.budget_exhausted is True
        assert len(adapter.requests) == 1
        assert not any(
            event.event_type == TraceEventType.TOOL_CALL
            for event in logger.list_for_run(run.id)
        )
    finally:
        storage.close()


def test_agent_loop_prices_total_only_usage_conservatively(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="Usable result with aggregate-only usage.",
            usage={"total_tokens": 100},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        )
    ]
    storage, _, task, run, step, agent, _, executor, request = _loop_env(tmp_path, responses)
    agent = agent.model_copy(
        update={
            "model_settings": {
                **agent.model_settings,
                "input_usd_per_million": 0,
                "output_usd_per_million": 2,
            }
        }
    )
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_total_tokens": 1_000, "max_cost_usd": 0.00015}
            )
        }
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.stop_reason == "cost_budget"
        assert result.budget_exhausted is True
        assert result.estimated_cost_usd == pytest.approx(0.0002)
    finally:
        storage.close()


def test_agent_loop_rejects_long_request_when_estimated_input_cost_exhausts_budget(
    tmp_path: Path,
) -> None:
    responses = [
        ModelResponse(
            text="This response must never be requested.",
            usage={"input_tokens": 1, "output_tokens": 1},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        )
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    agent = agent.model_copy(
        update={
            "model_settings": {
                "input_usd_per_million": 1_000_000.0,
                "output_usd_per_million": 0.0,
            }
        }
    )
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_total_tokens": 2_000, "max_cost_usd": 10.0}
            )
        }
    )
    request = replace(
        request,
        system_prompt="s" * 256,
        messages=[ModelMessage(role="user", content="m" * 256)],
        max_tokens=10,
    )
    try:
        with pytest.raises(AgentLoopError, match="cost budget"):
            executor.execute(task=task, run=run, step=step, agent=agent, request=request)
        assert adapter.requests == []
    finally:
        storage.close()


def test_agent_loop_stops_before_next_request_when_no_output_token_fits_cost_budget(
    tmp_path: Path,
) -> None:
    responses = [
        ModelResponse(
            text="Partial result before another tool round.",
            usage={"input_tokens": 0, "output_tokens": 2},
            finish_reason="tool_calls",
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
            tool_calls=[ModelToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
        ),
        ModelResponse(
            text="This response must never be requested.",
            usage={"input_tokens": 0, "output_tokens": 1},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        ),
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    agent = agent.model_copy(
        update={
            "model_settings": {
                "input_usd_per_million": 0.0,
                "output_usd_per_million": 200_000.0,
            }
        }
    )
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_total_tokens": 2_000, "max_cost_usd": 0.45}
            )
        }
    )
    request = replace(request, max_tokens=2)
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.text == "Partial result before another tool round."
        assert result.stop_reason == "cost_budget"
        assert result.budget_exhausted is True
        assert result.estimated_cost_usd == pytest.approx(0.4)
        assert len(adapter.requests) == 1
    finally:
        storage.close()


def test_agent_loop_fails_closed_before_call_when_fallback_price_is_unknown(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            text="This response must never be requested.",
            usage={"input_tokens": 1, "output_tokens": 1},
            raw_provider="scripted",
            adapter="scripted",
            mocked=False,
        )
    ]
    storage, _, task, run, step, agent, adapter, executor, request = _loop_env(tmp_path, responses)
    agent = agent.model_copy(
        update={
            "model_settings": {
                "input_usd_per_million": 1.0,
                "output_usd_per_million": 2.0,
                "fallbacks": [{"provider": "unknown", "model": "unknown-model"}],
            }
        }
    )
    request = replace(
        request,
        fallbacks=[{"provider": "unknown", "model": "unknown-model"}],
    )
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_total_tokens": 100, "max_cost_usd": 1.0}
            )
        }
    )
    try:
        with pytest.raises(AgentLoopError, match="price"):
            executor.execute(task=task, run=run, step=step, agent=agent, request=request)
        assert adapter.requests == []
    finally:
        storage.close()


def test_agent_loop_prices_and_traces_the_selected_fallback_route(tmp_path: Path) -> None:
    storage, logger, task, run, step, agent, _, executor, request = _loop_env(tmp_path, [])

    class RetryableFailureAdapter:
        def complete(self, candidate_request: ModelRequest) -> ModelResponse:
            raise ModelRuntimeError(
                "Timed out.",
                provider=candidate_request.provider,
                model=candidate_request.model,
                error_class="TimeoutError",
                error_summary="classification=timeout_error;retryable=true",
            )

    fallback_adapter = ScriptedAdapter(
        [
            ModelResponse(
                text="Fallback completed the loop.",
                usage={"input_tokens": 10, "output_tokens": 5},
                raw_provider="fallback",
                adapter="scripted",
                mocked=False,
            )
        ]
    )
    executor.model_gateway = ModelGateway(
        adapters={"scripted": RetryableFailureAdapter(), "fallback": fallback_adapter},
        capability_registry=CapabilityRegistry(
            [
                ModelCapability(
                    provider="scripted",
                    model_pattern="*",
                    protocol="test",
                    supports_tools=True,
                ),
                ModelCapability(
                    provider="fallback",
                    model_pattern="*",
                    protocol="test",
                    supports_tools=True,
                ),
            ]
        ),
    )
    agent = agent.model_copy(
        update={
            "model_settings": {
                "input_usd_per_million": 1.0,
                "output_usd_per_million": 2.0,
                "fallbacks": [
                    {
                        "provider": "fallback",
                        "model": "fallback-model",
                        "input_usd_per_million": 10.0,
                        "output_usd_per_million": 20.0,
                    }
                ],
            }
        }
    )
    request = replace(
        request,
        input_usd_per_million=1.0,
        output_usd_per_million=2.0,
        fallbacks=agent.model_settings["fallbacks"],
        max_tokens=100,
    )
    step = step.model_copy(
        update={
            "agent_loop": step.agent_loop.model_copy(
                update={"max_total_tokens": 1_000, "max_cost_usd": 1.0}
            )
        }
    )
    try:
        result = executor.execute(task=task, run=run, step=step, agent=agent, request=request)

        assert result.estimated_cost_usd == pytest.approx(0.0002)
        response_event = next(
            event
            for event in logger.list_for_run(run.id)
            if event.event_type == TraceEventType.MODEL_ACTION
            and event.payload.get("action") == "model_response"
        )
        assert response_event.payload["provider"] == "fallback"
        assert response_event.payload["model"] == "fallback-model"
    finally:
        storage.close()
