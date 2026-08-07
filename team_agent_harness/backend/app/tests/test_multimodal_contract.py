from __future__ import annotations

import base64
from hashlib import sha256
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import (
    PackMappedExecutor,
    WorkflowRunnerError,
    _content_block_snapshot,
    _content_block_snapshot_hash,
    _vision_preprocess_snapshot,
)
from app.core.context_injection import ContextBudgetExceeded, UNTRUSTED_EXTERNAL_DATA_SAFETY_NOTICE
from app.core.model_runtime import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRuntimeError,
    MockModelAdapter,
    OpenAICompatibleModelAdapter,
    context_message_from_envelope,
    _chat_content,
    _responses_content,
)
from app.core.multimodal import MultimodalInputError, multimodal_source_refs, prepare_content_blocks
from app.core.local_code_executor import _artifact_source_refs
from app.core.artifacts import ArtifactStore, ArtifactStoreError
from app.core.models import AgentDefinition, AgentRun, ArtifactType, Run, Task
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger
from app.packs.base import ContextPolicy, WorkflowStep
from app.main import create_app


PNG_BYTES = b"\x89PNG\r\n\x1a\nminimal"


def _image_block(path: str, content: bytes = PNG_BYTES, *, mime_type: str = "image/png") -> dict[str, object]:
    return {
        "type": "image_ref",
        "path": path,
        "mime_type": mime_type,
        "sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _multimodal_executor_env(
    tmp_path,
    *,
    model_gateway: ModelGateway,
    agent_model_config: dict[str, object],
    vision_preprocess: dict[str, object] | None = None,
    stage_input: bool = True,
):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir(exist_ok=True)
    (inputs_dir / "photo.png").write_bytes(PNG_BYTES)
    inputs: dict[str, object] = {
        "allow_external_model_inputs": True,
        "content_blocks": [_image_block("inputs/photo.png")],
    }
    if vision_preprocess is not None:
        inputs["vision_preprocess"] = vision_preprocess
    storage = SQLiteStorage(tmp_path / "harness.sqlite3")
    storage.connect()
    storage.init_schema()
    logger = TraceLogger(storage)
    store = ArtifactStore(tmp_path / "artifacts", storage, logger)
    task = storage.create_task(
        Task(
            id="task-multimodal",
            title="Multimodal",
            goal="Inspect the image.",
            workflow_pack="code_rd",
            inputs=inputs,
        )
    )
    run = Run(
        id="run-multimodal",
        task_id=task.id,
        real_model_access_confirmed=True,
        content_block_snapshot=_content_block_snapshot(task.inputs),
        content_block_snapshot_hash=_content_block_snapshot_hash(task.inputs),
        vision_preprocess_snapshot=_vision_preprocess_snapshot(task.inputs),
        allow_external_model_inputs_snapshot=True,
    )
    if stage_input:
        content_hash = sha256(PNG_BYTES).hexdigest()
        staged_path = store.stage_input_bytes(
            run_id=run.id,
            content_hash=content_hash,
            content=PNG_BYTES,
        )
        run = run.model_copy(update={"content_block_snapshot_files": {content_hash: staged_path}})
    run = storage.create_run(run)
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-multimodal",
            pack_name="code_rd",
            role="Reviewer",
            system_prompt="Inspect the supplied evidence.",
            model_config=agent_model_config,
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="attempt-multimodal",
            run_id=run.id,
            agent_id=agent.id,
            step_name="review",
        )
    )
    step = WorkflowStep(
        name="review",
        agent_role=agent.role,
        produces_artifact_type=ArtifactType.FINAL_REPORT,
    )
    executor = PackMappedExecutor(
        model_gateway=model_gateway,
        artifact_store=store,
        trace_logger=logger,
        config_root=tmp_path,
    )
    return storage, store, task, run, step, agent, agent_run, executor


def test_multimodal_rejects_traversal_sensitive_names_and_bad_metadata(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "password.png"
    image.write_bytes(PNG_BYTES)

    with pytest.raises(MultimodalInputError):
        prepare_content_blocks(
            {
                "allow_external_model_inputs": True,
                "content_blocks": [_image_block("inputs/password.png")],
            },
            root=tmp_path,
        )
    with pytest.raises(MultimodalInputError):
        prepare_content_blocks(
            {
                "allow_external_model_inputs": True,
                "content_blocks": [_image_block("inputs/../password.png")],
            },
            root=tmp_path,
        )
    with pytest.raises(MultimodalInputError):
        prepare_content_blocks(
            {
                "allow_external_model_inputs": True,
                "content_blocks": [_image_block("inputs/photo.png", mime_type="image/jpeg")],
            },
            root=tmp_path,
        )


def test_task_creation_stages_verified_bytes_for_restart_replay(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    image = inputs / "photo.png"
    image.write_bytes(PNG_BYTES)
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts", config_root=tmp_path)

    with TestClient(app) as client:
        task_response = client.post(
            "/tasks",
            json={
                "title": "Durable image input",
                "goal": "Keep the verified image bytes for recovery.",
                "workflow_pack": "code_rd",
                "inputs": {
                    "allow_external_model_inputs": True,
                    "content_blocks": [_image_block("inputs/photo.png")],
                },
            },
        )
        assert task_response.status_code == 201, task_response.text
        task = task_response.json()
        run_response = client.post("/runs", json={"task_id": task["id"]})
        assert run_response.status_code == 201, run_response.text
        run = run_response.json()

    staged = run["content_block_snapshot_files"]
    content_hash = sha256(PNG_BYTES).hexdigest()
    assert staged[content_hash].startswith(f"{run['id']}/input-")
    image.unlink()
    state = app.state.harness
    replayed = state.artifact_store.read_staged_input(
        staged[content_hash],
        content_hash=content_hash,
        max_size=8 * 1024 * 1024,
    )
    assert replayed == PNG_BYTES


def test_model_gateway_derives_vision_requirement_from_payload() -> None:
    calls: list[ModelRequest] = []
    adapter = SimpleNamespace(
        complete=lambda request: calls.append(request)
        or ModelResponse(text="unexpected", raw_provider="deepseek", adapter="test", mocked=False)
    )
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    request = ModelRequest(
        provider="deepseek",
        model="deepseek-chat",
        system_prompt="System",
        messages=[
            ModelMessage(
                role="user",
                content=[{"type": "image_ref", "data_uri": data_uri}],
            )
        ],
    )

    with pytest.raises(ModelRuntimeError, match="capability contract"):
        ModelGateway({"deepseek": adapter}).complete(request)
    assert calls == []


def test_model_gateway_skips_mock_for_confirmed_vision_fallback() -> None:
    fallback_calls: list[ModelRequest] = []
    fallback = SimpleNamespace(
        complete=lambda request: fallback_calls.append(request)
        or ModelResponse(
            text="Fallback inspected the image.",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            raw_provider="openai",
            adapter="test",
            mocked=False,
        )
    )
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    request = ModelRequest(
        provider="mock",
        model="mock-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content=[{"type": "image_ref", "data_uri": data_uri}])],
        fallbacks=[{"provider": "openai", "model": "gpt-5", "allow_real_calls": True}],
        metadata={"run_bound": True, "real_model_access_confirmed": True},
    )

    response = ModelGateway({"mock": MockModelAdapter(), "openai": fallback}).complete(request)

    assert response.raw_provider == "openai"
    assert len(fallback_calls) == 1
    assert [entry["outcome"] for entry in response.route_receipt] == ["rejected", "succeeded"]


def test_serializers_have_exact_image_shape_and_reject_bare_file_refs() -> None:
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    content = [{"type": "image_ref", "data_uri": data_uri}]

    assert _chat_content(content) == [{"type": "image_url", "image_url": {"url": data_uri}}]
    assert _responses_content(content) == [{"type": "input_image", "image_url": data_uri}]
    with pytest.raises(ModelRuntimeError, match="file_ref"):
        _chat_content([{"type": "file_ref", "data_uri": "data:text/plain;base64,QQ=="}])
    with pytest.raises(ModelRuntimeError, match="file_ref"):
        _responses_content([{"type": "file_ref", "data_uri": "data:text/plain;base64,QQ=="}])


def test_file_ref_becomes_bounded_untrusted_text_with_provenance(tmp_path) -> None:
    content = b"external evidence"
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "evidence.txt").write_bytes(content)
    content_hash = sha256(content).hexdigest()

    prepared = prepare_content_blocks(
        {
            "allow_external_model_inputs": True,
            "content_blocks": [
                {
                    "type": "file_ref",
                    "path": "inputs/evidence.txt",
                    "mime_type": "text/plain",
                    "sha256": content_hash,
                    "size_bytes": len(content),
                }
            ],
        },
        root=tmp_path,
    )

    assert prepared.context_blocks == [
        {
            "type": "file_ref",
            "mime_type": "text/plain",
            "sha256": content_hash,
            "size_bytes": len(content),
        }
    ]
    assert prepared.source_refs == [f"file_ref:{content_hash}"]
    assert prepared.model_blocks[0]["type"] == "text"
    assert "untrusted data" in prepared.model_blocks[0]["text"]
    assert "external evidence" in prepared.model_blocks[0]["text"]

    context = {
        "content_blocks": prepared.context_blocks,
        "vision_preprocess": {"artifact_id": "sidecar-artifact", "input_refs": ["image_ref:image-hash"]},
    }
    assert _artifact_source_refs(context, {"app.py": "source"}) == [
        "app.py",
        f"file_ref:{content_hash}",
        "image_ref:image-hash",
        "artifact:sidecar-artifact",
    ]


def test_invalid_image_data_uri_is_rejected_before_provider_call() -> None:
    with pytest.raises(ModelRuntimeError, match="valid base64"):
        _chat_content([{"type": "image_ref", "data_uri": "data:image/png;base64,not-base64!"}])


def test_run_bound_real_fallback_requires_durable_confirmation() -> None:
    class RetryableMockFailure:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise ModelRuntimeError(
                "mock route failed",
                provider="mock",
                model=request.model,
                error_class="TimeoutError",
                error_summary="classification=timeout_error;retryable=true",
            )

    deepseek_calls: list[ModelRequest] = []
    deepseek = SimpleNamespace(
        complete=lambda request: deepseek_calls.append(request)
        or ModelResponse(text="should not be called", raw_provider="deepseek", adapter="test", mocked=False)
    )
    request = ModelRequest(
        provider="mock",
        model="mock-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        fallbacks=[
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "allow_real_calls": True,
            }
        ],
        metadata={
            "allow_mock_fallback": True,
            "run_bound": True,
            "real_model_access_confirmed": False,
        },
    )

    with pytest.raises(ModelRuntimeError):
        ModelGateway({"mock": RetryableMockFailure(), "deepseek": deepseek}).complete(request)
    assert deepseek_calls == []


def test_sidecar_artifact_write_is_idempotent_for_retry(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        task = storage.create_task(Task(id="task-sidecar", title="Sidecar", goal="Describe", workflow_pack="code_rd"))
        run = storage.create_run(Run(id="run-sidecar", task_id=task.id))
        agent = storage.create_agent_definition(
            AgentDefinition(
                id="agent-sidecar",
                pack_name="code_rd",
                role="Reviewer",
                system_prompt="Review.",
            )
        )
        agent_run = storage.create_agent_run(
            AgentRun(id="attempt-sidecar", run_id=run.id, agent_id=agent.id, step_name="review")
        )
        store = ArtifactStore(tmp_path / "artifacts", storage, TraceLogger(storage))
        first = store.write_text_idempotent(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.IMAGE_DESCRIPTION,
            filename="vision-description-hash-attempt.md",
            content="description",
            source_refs=["image_ref:hash"],
        )
        second = store.write_text_idempotent(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.IMAGE_DESCRIPTION,
            filename="vision-description-hash-attempt.md",
            content="description",
            source_refs=["image_ref:hash"],
        )

        assert second.id == first.id
        assert len(storage.list_artifacts_for_run(run.id)) == 1


def test_idempotent_artifact_adopts_matching_orphan_after_crash(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        task = storage.create_task(Task(id="task-orphan", title="Orphan", goal="Recover", workflow_pack="code_rd"))
        run = storage.create_run(Run(id="run-orphan", task_id=task.id))
        agent = storage.create_agent_definition(
            AgentDefinition(
                id="agent-orphan",
                pack_name="code_rd",
                role="Reviewer",
                system_prompt="Review.",
            )
        )
        agent_run = storage.create_agent_run(
            AgentRun(id="attempt-orphan", run_id=run.id, agent_id=agent.id, step_name="review")
        )
        store = ArtifactStore(tmp_path / "artifacts", storage, TraceLogger(storage))
        orphan_path = store.root_dir / run.id / "vision-description-orphan.md"
        orphan_path.parent.mkdir(parents=True)
        orphan_path.write_text("description", encoding="utf-8", newline="")

        recovered = store.write_text_idempotent(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.IMAGE_DESCRIPTION,
            filename=orphan_path.name,
            content="description",
            source_refs=["image_ref:hash"],
        )

        assert store.read_text_verified(recovered) == "description"
        assert recovered.source_refs == ["image_ref:hash"]
        assert len(storage.list_artifacts_for_run(run.id)) == 1


def test_idempotent_artifact_rejects_missing_or_tampered_durable_copy(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        task = storage.create_task(Task(id="task-artifact-integrity", title="Artifact", goal="Verify", workflow_pack="code_rd"))
        run = storage.create_run(Run(id="run-artifact-integrity", task_id=task.id))
        agent = storage.create_agent_definition(
            AgentDefinition(
                id="agent-artifact-integrity",
                pack_name="code_rd",
                role="Reviewer",
                system_prompt="Review.",
            )
        )
        agent_run = storage.create_agent_run(
            AgentRun(id="attempt-artifact-integrity", run_id=run.id, agent_id=agent.id, step_name="review")
        )
        store = ArtifactStore(tmp_path / "artifacts", storage, TraceLogger(storage))
        first = store.write_text_idempotent(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.IMAGE_DESCRIPTION,
            filename="vision-description.md",
            content="description",
        )

        artifact_path = store.root_dir / first.path
        artifact_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ArtifactStoreError, match="missing or invalid"):
            store.write_text_idempotent(
                run_id=run.id,
                agent_run_id=agent_run.id,
                artifact_type=ArtifactType.IMAGE_DESCRIPTION,
                filename="vision-description.md",
                content="description",
            )

        artifact_path.unlink()
        with pytest.raises(ArtifactStoreError, match="missing or invalid"):
            store.write_text_idempotent(
                run_id=run.id,
                agent_run_id=agent_run.id,
                artifact_type=ArtifactType.IMAGE_DESCRIPTION,
                filename="vision-description.md",
                content="description",
            )
        assert len(storage.list_artifacts_for_run(run.id)) == 1


def test_staged_input_rejects_hardlinks_and_noncanonical_paths(tmp_path) -> None:
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        store = ArtifactStore(tmp_path / "artifacts", storage, TraceLogger(storage))
        payload = b"durable input"
        content_hash = sha256(payload).hexdigest()
        relative_path = store.stage_input_bytes(run_id="run-input-integrity", content_hash=content_hash, content=payload)
        staged_path = store.root_dir / relative_path
        hardlink_path = staged_path.with_name("hardlink.bin")
        try:
            hardlink_path.parent.mkdir(parents=True, exist_ok=True)
            os.link(staged_path, hardlink_path)
        except (AttributeError, OSError) as exc:
            pytest.skip(f"Hard links are unavailable: {exc}")
        try:
            with pytest.raises(ArtifactStoreError, match="hard-linked"):
                store.read_staged_input(relative_path, content_hash=content_hash, max_size=1024)
        finally:
            hardlink_path.unlink(missing_ok=True)

        with pytest.raises(ArtifactStoreError, match="invalid durable shape"):
            store.read_staged_input(
                "run-input-integrity/../input-" + content_hash + ".bin",
                content_hash=content_hash,
                max_size=1024,
            )


def test_multimodal_recovery_never_falls_back_to_mutable_source_path(tmp_path) -> None:
    gateway = ModelGateway(
        {
            "openai": SimpleNamespace(
                complete=lambda request: ModelResponse(
                    text="unexpected",
                    raw_provider="openai",
                    adapter="test",
                    mocked=False,
                )
            )
        }
    )
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "openai", "model": "gpt-5"},
        stage_input=False,
    )
    try:
        with pytest.raises(WorkflowRunnerError, match="snapshot is missing"):
            executor._prepare_multimodal_context(
                task=task,
                run=run,
                step=step,
                agent=agent,
                context={"agent_run_id": agent_run.id},
            )
    finally:
        storage.close()


def test_unapproved_vision_fallback_does_not_suppress_configured_sidecar(tmp_path) -> None:
    sidecar_calls: list[ModelRequest] = []
    sidecar = SimpleNamespace(
        complete=lambda request: sidecar_calls.append(request)
        or ModelResponse(
            text="A bounded sidecar description.",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="litellm_proxy",
            adapter="test",
            mocked=False,
        )
    )
    gateway = ModelGateway(
        {
            "deepseek": SimpleNamespace(complete=lambda request: pytest.fail("primary must not run")),
            "openai": SimpleNamespace(complete=lambda request: pytest.fail("fallback is unapproved")),
            "litellm_proxy": sidecar,
        }
    )
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={
            "provider": "deepseek",
            "model": "deepseek-chat",
            "fallbacks": [
                {
                    "provider": "openai",
                    "model": "gpt-5",
                    "allow_real_calls": False,
                }
            ],
        },
        vision_preprocess={
            "provider": "litellm_proxy",
            "model": "gpt5.5",
            "allow_real_calls": True,
        },
    )
    try:
        context = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )

        assert len(sidecar_calls) == 1
        assert context["vision_preprocess"]["provider"] == "litellm_proxy"
        assert all(block.get("type") != "image_ref" for block in context["_model_content_blocks"])
        assert f"image_ref:{sha256(PNG_BYTES).hexdigest()}" in multimodal_source_refs(context)
        assert f"artifact:{context['vision_preprocess']['artifact_id']}" in multimodal_source_refs(context)
    finally:
        storage.close()


def test_direct_vision_context_has_untrusted_notice_and_final_budget_gate(tmp_path) -> None:
    gateway = ModelGateway(
        {
            "openai": SimpleNamespace(
                complete=lambda request: ModelResponse(
                    text="unused",
                    raw_provider="openai",
                    adapter="test",
                    mocked=False,
                )
            )
        }
    )
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "openai", "model": "gpt-5"},
    )
    try:
        context = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )
        assert context["_model_content_blocks"][0] == {
            "type": "text",
            "text": UNTRUSTED_EXTERNAL_DATA_SAFETY_NOTICE,
        }
        assert any(block.get("type") == "image_ref" for block in context["_model_content_blocks"])

        base_context = {"agent_run_id": agent_run.id, "artifact_excerpts": [{"excerpt": ""}]}
        base_length = len(context_message_from_envelope(base_context))
        base_context["artifact_excerpts"][0]["excerpt"] = "x" * (10_000 - base_length - 32)
        assert len(context_message_from_envelope(base_context)) < 10_000
        constrained_step = step.model_copy(
            update={"context_policy": ContextPolicy(max_context_chars=10_000, max_context_bytes=30_000)}
        )
        with pytest.raises(ContextBudgetExceeded, match="exceeds character budget"):
            executor._prepare_multimodal_context(
                task=task,
                run=run,
                step=constrained_step,
                agent=agent,
                context=base_context,
            )
    finally:
        storage.close()


def test_test_changes_receives_multimodal_context(tmp_path) -> None:
    gateway = ModelGateway({"openai": SimpleNamespace(complete=lambda request: pytest.fail("not called"))})
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "openai", "model": "gpt-5"},
    )
    captured: dict[str, object] = {}
    sentinel = object()
    executor.local_code_executor = SimpleNamespace(
        supports=lambda task, step: True,
        execute=lambda **kwargs: captured.update(context=kwargs["context"]) or sentinel,
    )
    try:
        result = executor.execute(
            task=task,
            run=run,
            step=step.model_copy(update={"name": "test_changes"}),
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )

        assert result is sentinel
        model_blocks = captured["context"]["_model_content_blocks"]
        assert any(block.get("type") == "image_ref" for block in model_blocks)
    finally:
        storage.close()


def test_sidecar_fails_before_model_call_without_durable_attempt(tmp_path) -> None:
    calls: list[ModelRequest] = []
    sidecar = SimpleNamespace(
        complete=lambda request: calls.append(request)
        or ModelResponse(text="description", raw_provider="openai", adapter="test", mocked=False)
    )
    gateway = ModelGateway(
        {
            "deepseek": SimpleNamespace(complete=lambda request: pytest.fail("primary must not run")),
            "openai": sidecar,
        }
    )
    storage, _, task, run, step, agent, _, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "deepseek", "model": "deepseek-chat"},
        vision_preprocess={"provider": "openai", "model": "gpt-5", "allow_real_calls": True},
    )
    try:
        with pytest.raises(WorkflowRunnerError, match="durable artifact storage and an agent attempt"):
            executor._prepare_multimodal_context(
                task=task,
                run=run,
                step=step,
                agent=agent,
                context={},
            )
        with pytest.raises(WorkflowRunnerError, match="durable agent attempt does not match"):
            executor._prepare_multimodal_context(
                task=task,
                run=run,
                step=step,
                agent=agent,
                context={"agent_run_id": "missing-attempt"},
            )
        assert calls == []
    finally:
        storage.close()


def test_sidecar_description_counts_toward_final_context_budget(tmp_path) -> None:
    sidecar = SimpleNamespace(
        complete=lambda request: ModelResponse(
            text="x" * 10_000,
            usage={"input_tokens": 10, "output_tokens": 2_000},
            raw_provider="openai",
            adapter="test",
            mocked=False,
        )
    )
    gateway = ModelGateway(
        {
            "deepseek": SimpleNamespace(complete=lambda request: pytest.fail("primary must not run")),
            "openai": sidecar,
        }
    )
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "deepseek", "model": "deepseek-chat"},
        vision_preprocess={"provider": "openai", "model": "gpt-5", "allow_real_calls": True},
    )
    constrained_step = step.model_copy(
        update={"context_policy": ContextPolicy(max_context_chars=10_000, max_context_bytes=30_000)}
    )
    try:
        with pytest.raises(ContextBudgetExceeded, match="exceeds character budget"):
            executor._prepare_multimodal_context(
                task=task,
                run=run,
                step=constrained_step,
                agent=agent,
                context={"agent_run_id": agent_run.id},
            )
    finally:
        storage.close()


def test_sidecar_retry_accepts_a_different_valid_description(tmp_path) -> None:
    responses = iter(["First description.", "Second description after restart."])
    sidecar = SimpleNamespace(
        complete=lambda request: ModelResponse(
            text=next(responses),
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="openai",
            adapter="test",
            mocked=False,
        )
    )
    gateway = ModelGateway(
        {
            "deepseek": SimpleNamespace(complete=lambda request: pytest.fail("primary must not run")),
            "openai": sidecar,
        }
    )
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "deepseek", "model": "deepseek-chat"},
        vision_preprocess={
            "provider": "openai",
            "model": "gpt-5",
            "allow_real_calls": True,
        },
    )
    try:
        first = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )
        second = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )

        assert first["vision_preprocess"]["artifact_id"] != second["vision_preprocess"]["artifact_id"]
        assert len(storage.list_artifacts_for_run(run.id)) == 2
    finally:
        storage.close()


def test_sidecar_preserves_file_context_and_uses_image_only_provenance(tmp_path) -> None:
    sidecar = SimpleNamespace(
        complete=lambda request: ModelResponse(
            text="Image description.",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="openai",
            adapter="test",
            mocked=False,
        )
    )
    gateway = ModelGateway(
        {
            "deepseek": SimpleNamespace(complete=lambda request: pytest.fail("primary must not run")),
            "openai": sidecar,
        }
    )
    storage, store, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "deepseek", "model": "deepseek-chat"},
        vision_preprocess={"provider": "openai", "model": "gpt-5", "allow_real_calls": True},
    )
    try:
        file_content = b"attached evidence"
        (tmp_path / "inputs" / "evidence.txt").write_bytes(file_content)
        file_hash = sha256(file_content).hexdigest()
        file_block = {
            "type": "file_ref",
            "path": "inputs/evidence.txt",
            "mime_type": "text/plain",
            "sha256": file_hash,
            "size_bytes": len(file_content),
        }
        task = task.model_copy(
            update={"inputs": {**task.inputs, "content_blocks": [*task.inputs["content_blocks"], file_block]}}
        )
        staged_file = store.stage_input_bytes(run_id=run.id, content_hash=file_hash, content=file_content)
        run = run.model_copy(
            update={
                "content_block_snapshot": _content_block_snapshot(task.inputs),
                "content_block_snapshot_hash": _content_block_snapshot_hash(task.inputs),
                "content_block_snapshot_files": {
                    **run.content_block_snapshot_files,
                    file_hash: staged_file,
                },
            }
        )

        context = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )

        assert any(block.get("type") == "file_ref" for block in context["content_blocks"])
        assert any("attached evidence" in block.get("text", "") for block in context["_model_content_blocks"])
        sidecar_artifact = storage.get_artifact(context["vision_preprocess"]["artifact_id"])
        assert sidecar_artifact is not None
        assert sidecar_artifact.source_refs == [f"image_ref:{sha256(PNG_BYTES).hexdigest()}"]
        final_refs = multimodal_source_refs(context)
        assert f"file_ref:{file_hash}" in final_refs
        assert f"image_ref:{sha256(PNG_BYTES).hexdigest()}" in final_refs
        assert f"artifact:{sidecar_artifact.id}" in final_refs
    finally:
        storage.close()


def test_sidecar_attempt_hash_prevents_shared_prefix_collision(tmp_path) -> None:
    sidecar = SimpleNamespace(
        complete=lambda request: ModelResponse(
            text="Stable description.",
            usage={"input_tokens": 10, "output_tokens": 5},
            raw_provider="openai",
            adapter="test",
            mocked=False,
        )
    )
    gateway = ModelGateway(
        {
            "deepseek": SimpleNamespace(complete=lambda request: pytest.fail("primary must not run")),
            "openai": sidecar,
        }
    )
    storage, _, task, run, step, agent, _, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "deepseek", "model": "deepseek-chat"},
        vision_preprocess={"provider": "openai", "model": "gpt-5", "allow_real_calls": True},
    )
    try:
        first_attempt = storage.create_agent_run(
            AgentRun(id="shared-prefix-0001-a", run_id=run.id, agent_id=agent.id, step_name=step.name)
        )
        second_attempt = storage.create_agent_run(
            AgentRun(id="shared-prefix-0001-b", run_id=run.id, agent_id=agent.id, step_name=step.name)
        )
        first = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": first_attempt.id},
        )
        second = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": second_attempt.id},
        )

        first_artifact = storage.get_artifact(first["vision_preprocess"]["artifact_id"])
        second_artifact = storage.get_artifact(second["vision_preprocess"]["artifact_id"])
        assert first_artifact is not None and second_artifact is not None
        assert first_artifact.path != second_artifact.path
    finally:
        storage.close()


def test_unready_direct_vision_provider_uses_ready_sidecar(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    sidecar_calls: list[ModelRequest] = []
    sidecar = SimpleNamespace(
        complete=lambda request: sidecar_calls.append(request)
        or ModelResponse(
            text="Sidecar description.",
            usage={"input_tokens": 10, "output_tokens": 5},
            finish_reason="completed",
            raw_provider="litellm_proxy",
            adapter="test",
            mocked=False,
        )
    )
    gateway = ModelGateway(
        {
            "openai": OpenAICompatibleModelAdapter(provider="openai", api_key_env="OPENAI_API_KEY"),
            "litellm_proxy": sidecar,
        }
    )
    storage, _, task, run, step, agent, agent_run, executor = _multimodal_executor_env(
        tmp_path,
        model_gateway=gateway,
        agent_model_config={"provider": "openai", "model": "gpt-5"},
        vision_preprocess={
            "provider": "litellm_proxy",
            "model": "gpt5.5",
            "allow_real_calls": True,
        },
    )
    try:
        context = executor._prepare_multimodal_context(
            task=task,
            run=run,
            step=step,
            agent=agent,
            context={"agent_run_id": agent_run.id},
        )

        assert len(sidecar_calls) == 1
        assert context["vision_preprocess"]["provider"] == "litellm_proxy"
    finally:
        storage.close()
