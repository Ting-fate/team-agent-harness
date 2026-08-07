from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
import math
from time import perf_counter
from typing import Callable

from app.core.model_runtime import (
    ModelGateway,
    ModelInteraction,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRoutePrice,
    ModelToolDefinition,
    reasoning_effort_trace_payload,
)
from app.core.models import AgentDefinition, Run, Task, TraceEventType
from app.core.sensitive_text import redact_secret_like_text
from app.core.tool_gateway import ToolContext, ToolGateway, ToolGatewayError
from app.core.trace import TraceLogger
from app.packs.base import WorkflowStep


class AgentLoopError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentLoopResult:
    text: str
    interactions: list[ModelInteraction]
    tool_call_count: int
    estimated_cost_usd: float | None
    stop_reason: str
    budget_exhausted: bool = False


class AgentLoopExecutor:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_gateway: ToolGateway,
        trace_logger: TraceLogger,
        on_request_started: Callable[[ModelRequest], None] | None = None,
    ) -> None:
        self.model_gateway = model_gateway
        self.tool_gateway = tool_gateway
        self.trace_logger = trace_logger
        self.on_request_started = on_request_started

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        request: ModelRequest,
    ) -> AgentLoopResult:
        policy = step.agent_loop
        if not policy.enabled:
            raise AgentLoopError("Agent loop is not enabled for this workflow step.")
        allowed_tools = frozenset(step.allowed_tools) & frozenset(agent.tool_permissions)
        if not allowed_tools:
            raise AgentLoopError("Agent loop has no tools allowed by both the step and agent.")
        tool_specs = [
            ModelToolDefinition(
                name=spec["name"],
                description=spec["description"],
                input_schema=spec["input_schema"],
            )
            for spec in self.tool_gateway.model_tool_specs(allowed_tools)
        ]
        configured_pricing = _model_pricing(agent)
        if request.input_usd_per_million is None and configured_pricing is not None:
            request = replace(
                request,
                input_usd_per_million=configured_pricing[0],
                output_usd_per_million=configured_pricing[1],
            )
        route_prices = self.model_gateway.route_prices(request)
        if policy.max_cost_usd is not None and any(price is None for price in route_prices):
            raise AgentLoopError(
                "Agent loop cost budget requires a known input/output price for every route candidate."
            )

        tool_context = ToolContext(
            run_id=run.id,
            agent_run_id=str(request.metadata.get("agent_run_id") or "unknown"),
            agent=agent,
            allowed_tools=frozenset(step.allowed_tools),
            real_web_access_confirmed=run.real_web_access_confirmed,
            enforce_side_effect_approval=True,
            approved_side_effect_tools=frozenset(run.approved_side_effect_tools),
        )
        deadline = perf_counter() + policy.timeout_seconds
        messages = list(request.messages)
        interactions: list[ModelInteraction] = []
        signature_counts: Counter[str] = Counter()
        tool_call_count = 0
        total_tokens = 0
        estimated_cost = 0.0 if all(price is not None for price in route_prices) else None
        best_text = ""
        force_final = False
        forced_stop_reason: str | None = None

        for loop_index in range(policy.max_steps):
            remaining_seconds = deadline - perf_counter()
            if remaining_seconds <= 0:
                return self._stopped(
                    run,
                    step,
                    interactions,
                    best_text,
                    tool_call_count,
                    estimated_cost,
                    "timeout_budget",
                )
            remaining_tokens = policy.max_total_tokens - total_tokens
            if remaining_tokens <= 0:
                return self._stopped(
                    run,
                    step,
                    interactions,
                    best_text,
                    tool_call_count,
                    estimated_cost,
                    "token_budget",
                )
            tools = tool_specs if not force_final and loop_index < policy.max_steps - 1 else []
            budget_request = replace(request, messages=messages, tools=tools)
            estimated_input_tokens = _estimated_request_input_tokens(budget_request)
            request_max_tokens = remaining_tokens - estimated_input_tokens
            if request_max_tokens <= 0:
                return self._stopped(
                    run,
                    step,
                    interactions,
                    best_text,
                    tool_call_count,
                    estimated_cost,
                    "token_budget",
                )
            if request.max_tokens is not None:
                request_max_tokens = min(request.max_tokens, request_max_tokens)
            if policy.max_cost_usd is not None and estimated_cost is not None:
                request_max_tokens = _cost_bounded_output_tokens(
                    request_max_tokens,
                    remaining_cost_usd=max(0.0, policy.max_cost_usd - estimated_cost),
                    route_prices=route_prices,
                    estimated_input_tokens=estimated_input_tokens,
                )
                if request_max_tokens <= 0:
                    return self._stopped(
                        run,
                        step,
                        interactions,
                        best_text,
                        tool_call_count,
                        estimated_cost,
                        "cost_budget",
                    )
            current_request = replace(
                budget_request,
                max_tokens=request_max_tokens,
                timeout_seconds=remaining_seconds,
                metadata={**request.metadata, "agent_loop_step": loop_index + 1},
            )
            if self.on_request_started is not None:
                self.on_request_started(current_request)
            response = self.model_gateway.complete(current_request)
            interactions.append(ModelInteraction(request=current_request, response=response))
            self._record_interaction(run, current_request, response)
            response_tokens = _response_tokens(
                response.usage,
                unknown_charge=remaining_tokens,
            )
            total_tokens += response_tokens
            if estimated_cost is not None:
                selected_pricing = _selected_response_pricing(response, route_prices)
                if selected_pricing is None:
                    raise AgentLoopError("Selected model route has no reliable price for cost accounting.")
                estimated_cost += _response_cost(
                    response.usage,
                    (
                        selected_pricing.input_usd_per_million,
                        selected_pricing.output_usd_per_million,
                    ),
                    token_charge=response_tokens,
                )
            if response.text.strip():
                best_text = response.text

            if perf_counter() >= deadline:
                return self._stopped(
                    run,
                    step,
                    interactions,
                    best_text,
                    tool_call_count,
                    estimated_cost,
                    "timeout_budget",
                )
            if total_tokens >= policy.max_total_tokens:
                return self._stopped(
                    run,
                    step,
                    interactions,
                    best_text,
                    tool_call_count,
                    estimated_cost,
                    "token_budget",
                )
            if (
                policy.max_cost_usd is not None
                and estimated_cost is not None
                and estimated_cost >= policy.max_cost_usd
            ):
                return self._stopped(
                    run,
                    step,
                    interactions,
                    best_text,
                    tool_call_count,
                    estimated_cost,
                    "cost_budget",
                )

            if not response.tool_calls:
                if not response.text.strip():
                    raise AgentLoopError("Agent loop returned neither text nor tool calls.")
                if forced_stop_reason is not None:
                    return self._stopped(
                        run,
                        step,
                        interactions,
                        response.text,
                        tool_call_count,
                        estimated_cost,
                        forced_stop_reason,
                    )
                return self._completed(
                    run,
                    step,
                    interactions,
                    response.text,
                    tool_call_count,
                    estimated_cost,
                )

            if not tools:
                raise AgentLoopError("Model requested tools after the loop entered finalization mode.")
            messages.append(
                ModelMessage(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            for tool_call in response.tool_calls:
                tool_call_count += 1
                signature = _tool_signature(tool_call.name, tool_call.arguments)
                signature_counts[signature] += 1
                if forced_stop_reason is not None:
                    observation = {"error": "agent_loop_budget_exhausted"}
                elif tool_call_count > policy.max_tool_calls:
                    observation = {"error": "tool_call_budget_exhausted"}
                    force_final = True
                    forced_stop_reason = "tool_call_budget"
                elif signature_counts[signature] > policy.max_repeated_tool_calls:
                    observation = {"error": "repeated_tool_call_blocked"}
                    force_final = True
                    forced_stop_reason = "repetition_budget"
                else:
                    try:
                        observation = self.tool_gateway.call_tool(
                            tool_context,
                            tool_call.name,
                            tool_call.arguments,
                        )
                    except ToolGatewayError as exc:
                        observation = {
                            "error": exc.__class__.__name__,
                            "message": redact_secret_like_text(str(exc)),
                        }
                messages.append(
                    ModelMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content=_bounded_observation(observation, policy.max_observation_chars),
                    )
                )
            if loop_index >= policy.max_steps - 2:
                force_final = True

        return self._stopped(
            run,
            step,
            interactions,
            best_text,
            tool_call_count,
            estimated_cost,
            "step_budget",
        )

    def _completed(
        self,
        run: Run,
        step: WorkflowStep,
        interactions: list[ModelInteraction],
        text: str,
        tool_call_count: int,
        estimated_cost_usd: float | None,
    ) -> AgentLoopResult:
        self._record_stop(run, step, "finished", len(interactions), tool_call_count, False)
        return AgentLoopResult(
            text=text,
            interactions=interactions,
            tool_call_count=tool_call_count,
            estimated_cost_usd=estimated_cost_usd,
            stop_reason="finished",
        )

    def _stopped(
        self,
        run: Run,
        step: WorkflowStep,
        interactions: list[ModelInteraction],
        best_text: str,
        tool_call_count: int,
        estimated_cost_usd: float | None,
        reason: str,
    ) -> AgentLoopResult:
        if not best_text.strip():
            raise AgentLoopError(
                f"Agent loop exhausted the {reason.replace('_', ' ')} without a usable result."
            )
        self._record_stop(run, step, reason, len(interactions), tool_call_count, True)
        return AgentLoopResult(
            text=best_text,
            interactions=interactions,
            tool_call_count=tool_call_count,
            estimated_cost_usd=estimated_cost_usd,
            stop_reason=reason,
            budget_exhausted=True,
        )

    def _record_stop(
        self,
        run: Run,
        step: WorkflowStep,
        reason: str,
        model_calls: int,
        tool_calls: int,
        budget_exhausted: bool,
    ) -> None:
        self.trace_logger.record(
            run_id=run.id,
            event_type=TraceEventType.WORKFLOW_EVENT,
            payload={
                "action": "agent_loop_stopped",
                "step_name": step.name,
                "reason": reason,
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "budget_exhausted": budget_exhausted,
            },
        )

    def _record_interaction(self, run: Run, request: ModelRequest, response: ModelResponse) -> None:
        metadata = request.metadata
        agent_run_id = str(metadata.get("agent_run_id")) if metadata.get("agent_run_id") else None
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=agent_run_id,
            event_type=TraceEventType.MODEL_ACTION,
            payload={
                "action": "model_request",
                "provider": request.provider,
                "model": request.model,
                "agent_id": metadata.get("agent_id"),
                "step_name": metadata.get("step_name"),
                "agent_loop_step": metadata.get("agent_loop_step"),
                **reasoning_effort_trace_payload(request),
                "tools_allowed": [tool.name for tool in request.tools],
                "context_keys": metadata.get("context_keys", []),
            },
        )
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=agent_run_id,
            event_type=TraceEventType.MODEL_ACTION,
            payload={
                "action": "model_response",
                "provider": response.selected_provider or response.raw_provider,
                "model": response.selected_model or request.model,
                "agent_id": metadata.get("agent_id"),
                "step_name": metadata.get("step_name"),
                "agent_loop_step": metadata.get("agent_loop_step"),
                "adapter": response.adapter,
                "mocked": response.mocked,
                "usage": response.usage,
                "latency_ms": response.latency_ms,
                "finish_reason": response.finish_reason,
                "output_length": len(response.text),
                "tool_call_count": len(response.tool_calls),
                "route_receipt": response.route_receipt,
            },
            duration_ms=response.latency_ms,
        )


def _bounded_observation(value: object, max_chars: int) -> str:
    try:
        rendered = json.dumps(
            {"trust": "untrusted_tool_output", "observation": value},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AgentLoopError("Tool returned a non-serializable observation.") from exc
    rendered = redact_secret_like_text(rendered)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars] + "...[truncated]"


def _tool_signature(name: str, arguments: dict[str, object]) -> str:
    return f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"


def _response_tokens(usage: dict[str, int], *, unknown_charge: int) -> int:
    total = usage.get("total_tokens")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    has_split = type(input_tokens) is int and type(output_tokens) is int
    split_total = _counter(input_tokens) + _counter(output_tokens)
    if type(total) is int and total > 0:
        return max(total, split_total if has_split else 0)
    if has_split and split_total > 0:
        return split_total
    return unknown_charge


def _model_pricing(agent: AgentDefinition) -> tuple[float, float] | None:
    input_price = agent.model_settings.get("input_usd_per_million")
    output_price = agent.model_settings.get("output_usd_per_million")
    if input_price is None and output_price is None:
        return None
    if input_price is None or output_price is None:
        raise AgentLoopError("Model input/output prices must be configured together.")
    try:
        prices = (float(input_price), float(output_price))
    except (TypeError, ValueError) as exc:
        raise AgentLoopError("Model prices must be non-negative numbers.") from exc
    if prices[0] < 0 or prices[1] < 0 or not all(math.isfinite(value) for value in prices):
        raise AgentLoopError("Model prices must be non-negative numbers.")
    return prices


def _cost_bounded_output_tokens(
    requested_max_tokens: int,
    *,
    remaining_cost_usd: float,
    route_prices: list[ModelRoutePrice | None],
    estimated_input_tokens: int,
) -> int:
    output_prices = [
        float(price.output_usd_per_million)
        for price in route_prices
        if price is not None
    ]
    input_prices = [
        float(price.input_usd_per_million)
        for price in route_prices
        if price is not None
    ]
    estimated_request_input_cost = (
        max(input_prices, default=0.0) * estimated_input_tokens / 1_000_000
    )
    remaining_output_cost = remaining_cost_usd - estimated_request_input_cost
    if remaining_output_cost <= 0:
        return 0
    maximum_output_price = max(output_prices, default=0.0)
    if maximum_output_price <= 0:
        return requested_max_tokens
    affordable_tokens = math.floor((remaining_output_cost * 1_000_000) / maximum_output_price)
    return min(requested_max_tokens, max(0, affordable_tokens))


def _estimated_request_input_tokens(request: ModelRequest) -> int:
    messages = []
    for message in request.messages:
        payload: dict[str, object] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            ]
        messages.append(payload)
    tools = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in request.tools
    ]
    try:
        encoded_bytes = len(
            json.dumps(
                {
                    "system_prompt": request.system_prompt,
                    "messages": messages,
                    "tools": tools,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise AgentLoopError("Model request could not be estimated for budgeting.") from exc
    framing_allowance = 64 + (16 * len(messages)) + (16 * len(tools))
    return max(1, encoded_bytes + framing_allowance)


def _selected_response_pricing(
    response: ModelResponse,
    route_prices: list[ModelRoutePrice | None],
) -> ModelRoutePrice | None:
    selected_provider = response.selected_provider or response.raw_provider
    selected_model = response.selected_model
    matching = [
        price
        for price in route_prices
        if price is not None
        and price.provider == selected_provider
        and (selected_model is None or price.model == selected_model)
    ]
    if len(matching) != 1:
        return None
    return matching[0]


def _response_cost(
    usage: dict[str, int],
    pricing: tuple[float, float],
    *,
    token_charge: int,
) -> float:
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if type(input_tokens) is int and type(output_tokens) is int:
        split_total = input_tokens + output_tokens
        unclassified_tokens = max(0, token_charge - split_total)
        cost = (
            input_tokens * pricing[0]
            + output_tokens * pricing[1]
            + unclassified_tokens * max(pricing)
        )
    else:
        cost = token_charge * max(pricing)
    return cost / 1_000_000


def _counter(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0
