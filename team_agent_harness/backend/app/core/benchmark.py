from __future__ import annotations

from collections import defaultdict
from statistics import fmean

from pydantic import Field, model_validator

from app.core.artifacts import ArtifactStore
from app.core.models import HarnessModel
from app.core.quality import (
    MODEL_RESPONSE_TRACE_ACTIONS,
    RunQualityCriteria,
    RunQualityReport,
    count_additional_unmetered_model_calls,
    evaluate_run_quality,
)
from app.core.storage import SQLiteStorage


class ModelPrice(HarnessModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_usd_per_million: float = Field(default=0, ge=0)
    output_usd_per_million: float = Field(default=0, ge=0)


class BenchmarkValueGate(HarnessModel):
    min_quality_gain_percentage_points: float = 10.0
    max_quality_regression_percentage_points: float = 0.0
    min_rework_reduction_percent: float = 25.0
    max_cost_ratio: float = Field(default=1.5, gt=0)
    max_duration_ratio: float = Field(default=1.5, gt=0)


class BenchmarkCase(HarnessModel):
    id: str = Field(min_length=1)
    description: str = ""
    quality_criteria: RunQualityCriteria


class BenchmarkSuite(HarnessModel):
    schema_version: str = Field(default="benchmark-v1", pattern=r"^benchmark-v1$")
    name: str = Field(min_length=1)
    baseline_variant: str = Field(min_length=1)
    variants: list[str] = Field(min_length=2)
    cases: list[BenchmarkCase] = Field(min_length=1)
    prices: list[ModelPrice] = Field(default_factory=list)
    value_gate: BenchmarkValueGate = Field(default_factory=BenchmarkValueGate)

    @model_validator(mode="after")
    def validate_suite(self) -> "BenchmarkSuite":
        if len(self.variants) != len(set(self.variants)):
            raise ValueError("Benchmark variants must be unique.")
        if self.baseline_variant not in self.variants:
            raise ValueError("baseline_variant must be listed in variants.")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Benchmark case ids must be unique.")
        price_keys = [(price.provider, price.model) for price in self.prices]
        if len(price_keys) != len(set(price_keys)):
            raise ValueError("Benchmark model prices must be unique by provider and model.")
        return self


class BenchmarkTrial(HarnessModel):
    case_id: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    replicate: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1)
    manual_rework_count: int = Field(default=0, ge=0)
    contradiction_count: int = Field(default=0, ge=0)
    indispensable_contributions: list[str] = Field(default_factory=list)


class BenchmarkTrialResult(HarnessModel):
    trial: BenchmarkTrial
    quality: RunQualityReport
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class BenchmarkVariantSummary(HarnessModel):
    variant: str
    trials: int = Field(ge=1)
    quality_pass_rate: float = Field(ge=0, le=1)
    average_total_tokens: float | None = Field(default=None, ge=0)
    average_duration_seconds: float | None = Field(default=None, ge=0)
    average_cost_usd: float | None = Field(default=None, ge=0)
    average_manual_rework_count: float = Field(ge=0)
    average_contradiction_count: float = Field(ge=0)
    average_indispensable_contributions: float = Field(ge=0)
    quality_gain_percentage_points: float | None = None
    cost_ratio: float | None = Field(default=None, ge=0)
    duration_ratio: float | None = Field(default=None, ge=0)
    rework_reduction_percent: float | None = None
    meets_value_gate: bool | None = None


class BenchmarkReport(HarnessModel):
    schema_version: str = "benchmark-report-v1"
    suite_name: str
    baseline_variant: str
    trial_results: list[BenchmarkTrialResult]
    variants: list[BenchmarkVariantSummary]


def evaluate_benchmark(
    storage: SQLiteStorage,
    artifact_store: ArtifactStore,
    suite: BenchmarkSuite,
    trials: list[BenchmarkTrial],
) -> BenchmarkReport:
    _validate_trials(suite, trials)
    cases = {case.id: case for case in suite.cases}
    prices = {(price.provider, price.model): price for price in suite.prices}
    trial_results = [
        BenchmarkTrialResult(
            trial=trial,
            quality=evaluate_run_quality(
                storage,
                artifact_store,
                trial.run_id,
                cases[trial.case_id].quality_criteria,
            ),
            estimated_cost_usd=_estimated_run_cost(storage, trial.run_id, prices),
        )
        for trial in sorted(
            trials,
            key=lambda item: (item.case_id, item.replicate, item.variant, item.run_id),
        )
    ]

    grouped: dict[str, list[BenchmarkTrialResult]] = defaultdict(list)
    for result in trial_results:
        grouped[result.trial.variant].append(result)
    raw_summaries = {
        variant: _summarize_variant(variant, grouped[variant])
        for variant in suite.variants
    }
    baseline = raw_summaries[suite.baseline_variant]
    baseline_results = {
        _trial_pair_key(result.trial): result
        for result in grouped[suite.baseline_variant]
    }
    summaries = [
        _compare_with_baseline(
            raw_summaries[variant],
            baseline,
            suite.value_gate,
            grouped[variant],
            baseline_results,
        )
        for variant in suite.variants
    ]
    return BenchmarkReport(
        suite_name=suite.name,
        baseline_variant=suite.baseline_variant,
        trial_results=trial_results,
        variants=summaries,
    )


def _validate_trials(suite: BenchmarkSuite, trials: list[BenchmarkTrial]) -> None:
    known_cases = {case.id for case in suite.cases}
    known_variants = set(suite.variants)
    keys: set[tuple[str, str, int]] = set()
    run_ids: set[str] = set()
    replicates: dict[tuple[str, str], set[int]] = defaultdict(set)
    for trial in trials:
        if trial.case_id not in known_cases:
            raise ValueError(f"Unknown benchmark case: {trial.case_id}")
        if trial.variant not in known_variants:
            raise ValueError(f"Unknown benchmark variant: {trial.variant}")
        key = (trial.case_id, trial.variant, trial.replicate)
        if key in keys:
            raise ValueError("Duplicate benchmark trial.")
        if trial.run_id in run_ids:
            raise ValueError(f"Benchmark run_id is reused across trials: {trial.run_id}")
        keys.add(key)
        run_ids.add(trial.run_id)
        replicates[(trial.case_id, trial.variant)].add(trial.replicate)
    missing: list[str] = []
    for case in suite.cases:
        case_replicates = set().union(
            *(replicates[(case.id, variant)] for variant in suite.variants)
        )
        if case_replicates:
            expected_replicates = set(range(1, max(case_replicates) + 1))
            if case_replicates != expected_replicates:
                missing.append(f"{case.id}:replicate-grid")
        for variant in suite.variants:
            if replicates[(case.id, variant)] != case_replicates or not case_replicates:
                missing.append(f"{case.id}:{variant}")
    if missing:
        raise ValueError(f"Benchmark trial coverage is incomplete: {', '.join(missing)}")


def _summarize_variant(
    variant: str,
    results: list[BenchmarkTrialResult],
) -> BenchmarkVariantSummary:
    durations = [
        result.quality.metrics.duration_seconds
        for result in results
        if result.quality.metrics.duration_seconds is not None
    ]
    costs = [result.estimated_cost_usd for result in results if result.estimated_cost_usd is not None]
    complete_token_usage = all(result.quality.metrics.usage_complete for result in results)
    return BenchmarkVariantSummary(
        variant=variant,
        trials=len(results),
        quality_pass_rate=sum(result.quality.passed for result in results) / len(results),
        average_total_tokens=(
            fmean(result.quality.metrics.total_tokens for result in results)
            if complete_token_usage
            else None
        ),
        average_duration_seconds=fmean(durations) if durations else None,
        average_cost_usd=fmean(costs) if len(costs) == len(results) else None,
        average_manual_rework_count=fmean(result.trial.manual_rework_count for result in results),
        average_contradiction_count=fmean(result.trial.contradiction_count for result in results),
        average_indispensable_contributions=fmean(
            len(result.trial.indispensable_contributions) for result in results
        ),
    )


def _compare_with_baseline(
    summary: BenchmarkVariantSummary,
    baseline: BenchmarkVariantSummary,
    gate: BenchmarkValueGate,
    results: list[BenchmarkTrialResult],
    baseline_results: dict[tuple[str, int], BenchmarkTrialResult],
) -> BenchmarkVariantSummary:
    if summary.variant == baseline.variant:
        return summary.model_copy(
            update={
                "quality_gain_percentage_points": 0.0,
                "cost_ratio": 1.0 if baseline.average_cost_usd is not None else None,
                "duration_ratio": 1.0 if baseline.average_duration_seconds is not None else None,
                "rework_reduction_percent": 0.0,
                "meets_value_gate": None,
            }
        )

    paired = [
        (result, baseline_results[_trial_pair_key(result.trial)])
        for result in results
    ]
    quality_gain = fmean(
        (float(result.quality.passed) - float(baseline_result.quality.passed)) * 100
        for result, baseline_result in paired
    )
    cost_ratio = _mean_optional(
        [_ratio(result.estimated_cost_usd, baseline_result.estimated_cost_usd) for result, baseline_result in paired]
    )
    duration_ratio = _mean_optional(
        [
            _ratio(
                result.quality.metrics.duration_seconds,
                baseline_result.quality.metrics.duration_seconds,
            )
            for result, baseline_result in paired
        ]
    )
    rework_reduction = _mean_optional(
        [
            _reduction_percent(
                float(result.trial.manual_rework_count),
                float(baseline_result.trial.manual_rework_count),
            )
            for result, baseline_result in paired
        ]
    )

    quality_wins = quality_gain >= gate.min_quality_gain_percentage_points
    rework_wins = (
        quality_gain >= -gate.max_quality_regression_percentage_points
        and rework_reduction is not None
        and rework_reduction >= gate.min_rework_reduction_percent
    )
    cost_ok = cost_ratio is not None and cost_ratio <= gate.max_cost_ratio
    duration_ok = duration_ratio is not None and duration_ratio <= gate.max_duration_ratio
    return summary.model_copy(
        update={
            "quality_gain_percentage_points": quality_gain,
            "cost_ratio": cost_ratio,
            "duration_ratio": duration_ratio,
            "rework_reduction_percent": rework_reduction,
            "meets_value_gate": bool((quality_wins or rework_wins) and cost_ok and duration_ok),
        }
    )


def _estimated_run_cost(
    storage: SQLiteStorage,
    run_id: str,
    prices: dict[tuple[str, str], ModelPrice],
) -> float | None:
    trace_events = storage.list_trace_events_for_run(run_id)
    if count_additional_unmetered_model_calls(trace_events):
        return None
    total = 0.0
    seen_model_response = False
    for event in trace_events:
        if event.payload.get("action") not in MODEL_RESPONSE_TRACE_ACTIONS:
            continue
        seen_model_response = True
        key = (str(event.payload.get("provider", "")), str(event.payload.get("model", "")))
        price = prices.get(key)
        if price is None:
            return None
        usage = event.payload.get("usage")
        if not isinstance(usage, dict):
            return None
        raw_input_tokens = usage.get("input_tokens")
        raw_output_tokens = usage.get("output_tokens")
        if not _is_counter(raw_input_tokens) or not _is_counter(raw_output_tokens):
            return None
        input_tokens = _counter(raw_input_tokens)
        output_tokens = _counter(raw_output_tokens)
        total += (
            input_tokens * price.input_usd_per_million
            + output_tokens * price.output_usd_per_million
        ) / 1_000_000
    return total if seen_model_response else 0.0


def _counter(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _is_counter(value: object) -> bool:
    return type(value) is int and value >= 0


def _trial_pair_key(trial: BenchmarkTrial) -> tuple[str, int]:
    return trial.case_id, trial.replicate


def _mean_optional(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return fmean(value for value in values if value is not None)


def _ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None:
        return None
    if baseline == 0:
        return 1.0 if value == 0 else None
    return value / baseline


def _reduction_percent(value: float, baseline: float) -> float | None:
    if baseline == 0:
        return 0.0 if value == 0 else None
    return (baseline - value) / baseline * 100
