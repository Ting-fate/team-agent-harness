from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.artifacts import ArtifactStore
from app.core.benchmark import BenchmarkSuite, BenchmarkTrial, evaluate_benchmark
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate reproducible Team Agent Harness benchmark trials.")
    parser.add_argument("--db", default="data/harness.sqlite3", help="Harness SQLite database path.")
    parser.add_argument("--artifacts", default="data/artifacts", help="Harness artifact root.")
    parser.add_argument("--suite", required=True, help="Benchmark suite JSON file.")
    parser.add_argument("--trials", required=True, help="Benchmark trial mapping JSON file.")
    parser.add_argument("--output", help="Optional report JSON path. Prints to stdout when omitted.")
    args = parser.parse_args()

    suite = BenchmarkSuite.model_validate_json(Path(args.suite).read_text(encoding="utf-8"))
    raw_trials = json.loads(Path(args.trials).read_text(encoding="utf-8"))
    trials = [BenchmarkTrial.model_validate(item) for item in raw_trials]
    with SQLiteStorage(args.db) as storage:
        storage.connect()
        artifact_store = ArtifactStore(args.artifacts, storage, TraceLogger(storage))
        report = evaluate_benchmark(storage, artifact_store, suite, trials)
    rendered = report.model_dump_json(indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
