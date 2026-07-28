from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "harness_control.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("harness_control", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, object | None]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, None))
        return self.responses[("GET", path)]

    def post(self, path: str, payload: object):
        self.calls.append(("POST", path, payload))
        return self.responses[("POST", path)]


class FakeLiteLlmClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def chat_completion(self, *, model: str, prompt: str, max_tokens: int, temperature: float) -> object:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return self.responses[model]


class FailingLiteLlmClient:
    def __init__(self, error_text: str) -> None:
        self.error_text = error_text

    def chat_completion(self, *, model: str, prompt: str, max_tokens: int, temperature: float) -> object:
        raise RuntimeError(self.error_text)


class GbkLikeStdout:
    encoding = "gbk"

    def __init__(self) -> None:
        self.value = ""

    def write(self, text: str) -> int:
        text.encode(self.encoding)
        self.value += text
        return len(text)


def test_create_task_posts_codex_operator_payload() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(
        [
            "--base-url",
            "http://127.0.0.1:8014/",
            "create-task",
            "--title",
            "Research task",
            "--goal",
            "Summarize sources.",
            "--workflow-pack",
            "research",
            "--input-json",
            '{"topic": "LLM"}',
            "--constraint",
            "Use safe sources.",
            "--acceptance",
            "Return source list.",
        ]
    )
    client = FakeClient({("POST", "/tasks"): {"id": "task-1", "workflow_pack": "research"}})
    stdout = io.StringIO()

    assert module.run_command(args, client=client, stdout=stdout) == 0

    assert client.calls == [
        (
            "POST",
            "/tasks",
            {
                "title": "Research task",
                "goal": "Summarize sources.",
                "workflow_pack": "research",
                "inputs": {"topic": "LLM"},
                "constraints": ["Use safe sources."],
                "acceptance_criteria": ["Return source list."],
                "created_by": "codex_operator",
            },
        )
    ]
    assert json.loads(stdout.getvalue())["id"] == "task-1"


def test_start_run_requires_confirmation_when_real_model_provider_is_enabled() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(["start-run", "--task-id", "task-1"])
    client = FakeClient(
        {
            ("GET", "/model-providers"): [
                {"name": "mock", "enabled": True, "real_calls": False},
                {"name": "litellm_proxy", "enabled": True, "real_calls": True},
            ]
        }
    )

    with pytest.raises(module.RealModelConfirmationRequired):
        module.run_command(args, client=client, stdout=io.StringIO())

    assert client.calls == [("GET", "/model-providers", None)]


def test_start_run_posts_after_real_model_confirmation() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(["start-run", "--task-id", "task-1", "--confirm-real-models"])
    client = FakeClient(
        {
            ("GET", "/model-providers"): [
                {"name": "litellm_proxy", "enabled": True, "real_calls": True},
            ],
            ("POST", "/runs"): {"id": "run-1", "task_id": "task-1", "status": "waiting"},
        }
    )
    stdout = io.StringIO()

    assert module.run_command(args, client=client, stdout=stdout) == 0

    assert client.calls == [
        ("GET", "/model-providers", None),
        (
            "POST",
            "/runs",
            {
                "task_id": "task-1",
                "confirm_real_models": True,
                "confirm_real_web": False,
                "background": True,
            },
        ),
    ]
    assert json.loads(stdout.getvalue())["id"] == "run-1"


def test_list_approvals_reads_detail_without_mutating_jobs() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(["list-approvals", "--run-id", "run-1"])
    client = FakeClient(
        {
            ("GET", "/runs/run-1/detail"): {
                "runtime_jobs": [
                    {"id": "job-1", "step_name": "prepare_patch", "status": "approval_required"},
                    {"id": "job-2", "step_name": "test_changes", "status": "completed"},
                ]
            }
        }
    )
    stdout = io.StringIO()

    assert module.run_command(args, client=client, stdout=stdout) == 0

    payload = json.loads(stdout.getvalue())
    assert payload == {
        "run_id": "run-1",
        "approval_required_jobs": [
            {"id": "job-1", "step_name": "prepare_patch", "status": "approval_required"}
        ],
    }
    assert client.calls == [("GET", "/runs/run-1/detail", None)]


def test_run_status_reads_lightweight_run_endpoint() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(["run-status", "--run-id", "run-1"])
    client = FakeClient({("GET", "/runs/run-1"): {"id": "run-1", "status": "waiting"}})
    stdout = io.StringIO()

    assert module.run_command(args, client=client, stdout=stdout) == 0

    assert json.loads(stdout.getvalue()) == {"id": "run-1", "status": "waiting"}
    assert client.calls == [("GET", "/runs/run-1", None)]


def test_model_smoke_calls_litellm_aliases_with_tiny_prompt_without_leaking_keys() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(
        [
            "model-smoke",
            "--confirm-real-models",
            "--model",
            "gpt5.5",
            "--model",
            "deepseek-v4-pro",
            "--max-tokens",
            "8",
        ]
    )
    litellm_client = FakeLiteLlmClient(
        {
            "gpt5.5": {
                "model": "gpt5.5",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
            "deepseek-v4-pro": {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        }
    )
    stdout = io.StringIO()

    assert module.run_command(args, client=FakeClient(), litellm_client=litellm_client, stdout=stdout) == 0

    assert litellm_client.calls == [
        {"model": "gpt5.5", "prompt": "Reply only with OK.", "max_tokens": 8, "temperature": 0.0},
        {"model": "deepseek-v4-pro", "prompt": "Reply only with OK.", "max_tokens": 8, "temperature": 0.0},
    ]
    payload = json.loads(stdout.getvalue())
    assert payload["ok"] is True
    assert [result["model"] for result in payload["results"]] == ["gpt5.5", "deepseek-v4-pro"]
    assert all(result["ok"] is True for result in payload["results"])
    assert "sk-" not in stdout.getvalue()
    assert "api_key" not in stdout.getvalue().lower()


def test_model_smoke_requires_real_model_confirmation() -> None:
    module = load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(["model-smoke", "--model", "gpt5.5"])

    with pytest.raises(module.RealModelConfirmationRequired):
        module.run_command(
            args,
            client=FakeClient(),
            litellm_client=FailingLiteLlmClient("should not be called"),
            stdout=io.StringIO(),
        )


def test_model_smoke_does_not_accept_custom_prompt() -> None:
    module = load_script_module()
    parser = module.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["model-smoke", "--confirm-real-models", "--model", "gpt5.5", "--prompt", "secret"])


def test_model_smoke_rejects_remote_litellm_base_url_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script_module()
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY", raising=False)
    parser = module.build_parser()
    args = parser.parse_args(
        [
            "model-smoke",
            "--confirm-real-models",
            "--litellm-base-url",
            "https://proxy.example/v1",
            "--model",
            "gpt5.5",
        ]
    )

    with pytest.raises(module.HarnessControlError, match="Remote LiteLLM"):
        module.run_command(args, client=FakeClient(), stdout=io.StringIO())


def test_redact_error_text_ignores_empty_secret_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module()
    monkeypatch.setenv("LITELLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    redacted = module._redact_error_text("failed with sk-test-secret")

    assert redacted == "failed with [REDACTED]"


def test_model_smoke_failure_output_redacts_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    parser = module.build_parser()
    args = parser.parse_args(["model-smoke", "--confirm-real-models", "--model", "gpt5.5"])
    stdout = io.StringIO()

    assert (
        module.run_command(
            args,
            client=FakeClient(),
            litellm_client=FailingLiteLlmClient(
                "provider rejected key sk-test-secret Authorization: Bearer sk-other api_key=abc123"
            ),
            stdout=stdout,
        )
        == 0
    )

    payload = json.loads(stdout.getvalue())
    assert payload["ok"] is False
    assert payload["results"][0]["ok"] is False
    assert payload["results"][0]["error"] == (
        "provider rejected key [REDACTED] Authorization: Bearer [REDACTED] api_key=[REDACTED]"
    )
    assert "sk-test-secret" not in stdout.getvalue()
    assert "sk-other" not in stdout.getvalue()
    assert "abc123" not in stdout.getvalue()


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--max-tokens", "0"),
        ("--max-tokens", "129"),
        ("--temperature", "-0.1"),
        ("--temperature", "2.1"),
        ("--temperature", "nan"),
    ],
)
def test_model_smoke_rejects_out_of_range_cost_options(option: str, value: str) -> None:
    module = load_script_module()
    parser = module.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["model-smoke", "--model", "gpt5.5", option, value])


def test_print_json_falls_back_to_ascii_when_terminal_cannot_encode_unicode() -> None:
    module = load_script_module()
    stdout = GbkLikeStdout()

    module._print_json({"label": "中文", "emoji": "🗺"}, stdout)

    assert json.loads(stdout.value) == {"label": "中文", "emoji": "🗺"}
    assert "\\ud" in stdout.value


def test_parser_does_not_expose_unsafe_approval_or_writeback_commands() -> None:
    module = load_script_module()
    parser = module.build_parser()

    for command in [
        "approve-runtime-job",
        "reject-runtime-job",
        "cancel-runtime-job",
        "writeback-preview",
        "writeback-approve",
    ]:
        with pytest.raises(SystemExit):
            parser.parse_args([command])
