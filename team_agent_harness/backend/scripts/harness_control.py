from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from time import perf_counter
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8014"
SMOKE_TEST_PROMPT = "Reply only with OK."


class HarnessControlError(RuntimeError):
    pass


class RealModelConfirmationRequired(HarnessControlError):
    pass


class HarnessApiClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self._request("POST", path, payload)

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                text = response.read().decode("utf-8")
        except HTTPError as exc:
            raise HarnessControlError(_safe_http_error_message(exc)) from exc
        except URLError as exc:
            raise HarnessControlError(f"Cannot reach harness API at {self.base_url}: {exc.reason}") from exc
        if not text:
            return None
        return json.loads(text)


class LiteLlmSmokeClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = _validated_litellm_base_url(
            base_url or os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
        ).rstrip("/") + "/"
        self.api_key = api_key or os.environ.get("LITELLM_API_KEY", "sk-dev-local-key")
        self.timeout_seconds = timeout_seconds

    def chat_completion(self, *, model: str, prompt: str, max_tokens: int, temperature: float) -> object:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request = Request(
            urljoin(self.base_url, "chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise HarnessControlError(_safe_http_error_message(exc)) from exc
        except URLError as exc:
            raise HarnessControlError(f"Cannot reach LiteLLM Proxy at {self.base_url}: {exc.reason}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safe Codex operator CLI for team_agent_harness.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Harness API base URL.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="Read /health.")

    create_task = subparsers.add_parser("create-task", help="Create a task without starting a run.")
    create_task.add_argument("--title", required=True)
    create_task.add_argument("--goal", required=True)
    create_task.add_argument("--workflow-pack", required=True)
    create_task.add_argument("--input-json", default="{}", help="Task inputs as a JSON object.")
    create_task.add_argument("--constraint", action="append", default=[])
    create_task.add_argument("--acceptance", action="append", default=[])
    create_task.add_argument("--created-by", default="codex_operator")

    start_run = subparsers.add_parser("start-run", help="Start a run for an existing task.")
    start_run.add_argument("--task-id", required=True)
    start_run.add_argument(
        "--confirm-real-models",
        action="store_true",
        help="Required when any real model provider is enabled.",
    )
    start_run.add_argument(
        "--confirm-real-web",
        action="store_true",
        help="Required by the server when the selected workflow can call enabled real web/browser tools.",
    )

    run_status = subparsers.add_parser("run-status", help="Read lightweight run status.")
    run_status.add_argument("--run-id", required=True)

    run_detail = subparsers.add_parser("run-detail", help="Read aggregated run detail.")
    run_detail.add_argument("--run-id", required=True)

    list_approvals = subparsers.add_parser("list-approvals", help="List runtime jobs waiting for approval.")
    list_approvals.add_argument("--run-id", required=True)

    subparsers.add_parser("latest-runs", help="List recent runs.")
    subparsers.add_parser("workflow-packs", help="List workflow packs.")
    subparsers.add_parser("model-providers", help="List model provider status.")

    model_smoke = subparsers.add_parser("model-smoke", help="Smoke test LiteLLM model aliases with tiny requests.")
    model_smoke.add_argument(
        "--confirm-real-models",
        action="store_true",
        help="Required because smoke tests may call paid external model APIs through LiteLLM.",
    )
    model_smoke.add_argument("--model", action="append", required=True, help="LiteLLM model alias to test.")
    model_smoke.add_argument("--max-tokens", type=_bounded_int(1, 128), default=8)
    model_smoke.add_argument("--temperature", type=_bounded_float(0.0, 2.0), default=0.0)
    model_smoke.add_argument("--litellm-base-url", default=None, help="LiteLLM OpenAI-compatible /v1 base URL.")

    return parser


def run_command(
    args: argparse.Namespace,
    *,
    client: Any | None = None,
    litellm_client: Any | None = None,
    stdout: TextIO = sys.stdout,
) -> int:
    api = client or HarnessApiClient(args.base_url, args.timeout)
    command = args.command

    if command == "health":
        _print_json(api.get("/health"), stdout)
        return 0
    if command == "create-task":
        _print_json(api.post("/tasks", _task_payload(args)), stdout)
        return 0
    if command == "start-run":
        _require_real_model_confirmation(api, args.confirm_real_models)
        _print_json(
            api.post(
                "/runs",
                {
                    "task_id": args.task_id,
                    "confirm_real_models": args.confirm_real_models,
                    "confirm_real_web": args.confirm_real_web,
                    "background": True,
                },
            ),
            stdout,
        )
        return 0
    if command == "run-status":
        _print_json(api.get(f"/runs/{args.run_id}"), stdout)
        return 0
    if command == "run-detail":
        _print_json(api.get(f"/runs/{args.run_id}/detail"), stdout)
        return 0
    if command == "list-approvals":
        detail = api.get(f"/runs/{args.run_id}/detail")
        _print_json(_approval_summary(args.run_id, detail), stdout)
        return 0
    if command == "latest-runs":
        _print_json(api.get("/runs"), stdout)
        return 0
    if command == "workflow-packs":
        _print_json(api.get("/workflow-packs"), stdout)
        return 0
    if command == "model-providers":
        _print_json(api.get("/model-providers"), stdout)
        return 0
    if command == "model-smoke":
        if not args.confirm_real_models:
            raise RealModelConfirmationRequired(
                "model-smoke may call paid external APIs through LiteLLM. "
                "Re-run with --confirm-real-models after confirming the tiny smoke-test cost is acceptable."
            )
        if args.litellm_base_url is not None:
            _validated_litellm_base_url(args.litellm_base_url)
        smoke_client = litellm_client or LiteLlmSmokeClient(
            base_url=args.litellm_base_url,
            timeout_seconds=args.timeout,
        )
        _print_json(_model_smoke(args, smoke_client), stdout)
        return 0

    raise HarnessControlError(f"Unsupported command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_command(args)
    except RealModelConfirmationRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except HarnessControlError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _task_payload(args: argparse.Namespace) -> dict[str, Any]:
    inputs = json.loads(args.input_json)
    if not isinstance(inputs, dict):
        raise HarnessControlError("--input-json must be a JSON object.")
    return {
        "title": args.title,
        "goal": args.goal,
        "workflow_pack": args.workflow_pack,
        "inputs": inputs,
        "constraints": args.constraint,
        "acceptance_criteria": args.acceptance,
        "created_by": args.created_by,
    }


def _bounded_int(min_value: int, max_value: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"must be an integer from {min_value} to {max_value}") from exc
        if parsed < min_value or parsed > max_value:
            raise argparse.ArgumentTypeError(f"must be from {min_value} to {max_value}")
        return parsed

    return parse


def _bounded_float(min_value: float, max_value: float):
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"must be a number from {min_value:g} to {max_value:g}") from exc
        if not math.isfinite(parsed) or parsed < min_value or parsed > max_value:
            raise argparse.ArgumentTypeError(f"must be from {min_value:g} to {max_value:g}")
        return parsed

    return parse


def _validated_litellm_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HarnessControlError("LITELLM_BASE_URL must be an http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HarnessControlError("LITELLM_BASE_URL must not include credentials, query, or fragment.")
    if not _is_loopback_host(parsed.hostname or ""):
        if os.environ.get("TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY") != "1":
            raise HarnessControlError(
                "Remote LiteLLM Proxy URLs are disabled for model-smoke. "
                "Use a loopback URL or set TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY=1 for a trusted HTTPS proxy."
            )
        if parsed.scheme != "https":
            raise HarnessControlError("Remote LiteLLM Proxy URLs must use https.")
    return value.strip()


def _is_loopback_host(hostname: str) -> bool:
    return hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def _require_real_model_confirmation(api: Any, confirmed: bool) -> None:
    providers = api.get("/model-providers")
    enabled_real = [
        str(provider.get("name", "unknown"))
        for provider in providers
        if provider.get("enabled") is True and provider.get("real_calls") is True
    ]
    if enabled_real and not confirmed:
        names = ", ".join(enabled_real)
        raise RealModelConfirmationRequired(
            "Real model providers are enabled: "
            f"{names}. Re-run with --confirm-real-models after confirming this may call paid external APIs."
        )


def _approval_summary(run_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    jobs = detail.get("runtime_jobs", [])
    if not isinstance(jobs, list):
        jobs = []
    return {
        "run_id": run_id,
        "approval_required_jobs": [
            job
            for job in jobs
            if isinstance(job, dict) and job.get("status") == "approval_required"
        ],
    }


def _model_smoke(args: argparse.Namespace, litellm_client: Any) -> dict[str, Any]:
    results = []
    for model in args.model:
        started = perf_counter()
        try:
            response = litellm_client.chat_completion(
                model=model,
                prompt=SMOKE_TEST_PROMPT,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
            latency_ms = max(1, int((perf_counter() - started) * 1000))
            results.append(
                {
                    "model": model,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "content_preview": _chat_content_preview(response),
                    "usage": _response_usage(response),
                }
            )
        except Exception as exc:
            latency_ms = max(1, int((perf_counter() - started) * 1000))
            results.append(
                {
                    "model": model,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "error_type": exc.__class__.__name__,
                    "error": _redact_error_text(str(exc)),
                }
            )
    return {"ok": all(result["ok"] for result in results), "results": results}


def _chat_content_preview(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return str(content or "")[:120]


def _response_usage(response: object) -> object:
    if isinstance(response, dict):
        return response.get("usage", {})
    return {}


def _print_json(payload: Any, stdout: TextIO) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        stdout.write(text)
    except UnicodeEncodeError:
        stdout.write(json.dumps(payload, ensure_ascii=True, indent=2))
    stdout.write("\n")


def _safe_http_error_message(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
    except Exception:
        body = ""
    if len(body) > 400:
        body = body[:400] + "..."
    return f"HTTP {exc.code}: {_redact_error_text(body)}"


def _redact_error_text(text: str) -> str:
    redacted = text
    for env_name in ("LITELLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        secret = os.environ.get(env_name)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(api[_-]?key\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(token\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(secret\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", redacted)
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
