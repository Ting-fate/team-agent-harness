from pathlib import Path
import shutil
import subprocess

import pytest


RUNTIME_JS = Path(__file__).parents[1] / "static" / "runtime.js"


def _run_node(assertions: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for static runtime behavior tests.")
    source = RUNTIME_JS.read_text(encoding="utf-8")
    result = subprocess.run(
        ["node", "-"],
        input=f"{source}\n{assertions}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_api_timeout_and_caller_abort_cover_response_body_read() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

function stalledResponse(signal) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    text: () => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("body aborted")), { once: true });
    }),
  };
}

(async () => {
  const api = HarnessRuntime.createApi({
    fetchImpl: async (_path, options) => stalledResponse(options.signal),
  });
  await assert.rejects(api("/slow-body", { timeoutMs: 20 }), /请求超时或已取消/);

  const caller = new AbortController();
  const pending = api("/caller-abort", { timeoutMs: 1000, signal: caller.signal });
  setTimeout(() => caller.abort(), 10);
  await assert.rejects(pending, /请求超时或已取消/);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    )


def test_refresh_coordinator_runs_queued_full_refresh_after_runtime_failure() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

(async () => {
  const calls = [];
  const refresh = HarnessRuntime.createRefreshCoordinator(async (options) => {
    calls.push(options);
    if (calls.length === 1) {
      throw new Error("runtime poll failed");
    }
    return "full refresh completed";
  });

  const runtimePoll = refresh({ runtimeOnly: true });
  const requiredFullRefresh = refresh({ feedback: true });
  assert.strictEqual(runtimePoll, requiredFullRefresh);
  assert.equal(await requiredFullRefresh, "full refresh completed");
  assert.deepEqual(calls, [
    { runtimeOnly: true },
    { feedback: true, runtimeOnly: false },
  ]);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    )


def test_refresh_coordinator_coalesces_and_merges_full_refresh_options() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

(async () => {
  const calls = [];
  let releaseRuntimePoll;
  const refresh = HarnessRuntime.createRefreshCoordinator((options) => {
    calls.push(options);
    if (calls.length === 1) {
      return new Promise((resolve) => {
        releaseRuntimePoll = resolve;
      });
    }
    return Promise.resolve("full refresh completed");
  });

  const runtimePoll = refresh({ runtimeOnly: true, silent: true });
  const firstFull = refresh({ feedback: true, silent: true });
  const secondFull = refresh({ silent: false });
  assert.strictEqual(runtimePoll, firstFull);
  assert.strictEqual(firstFull, secondFull);
  await Promise.resolve();
  releaseRuntimePoll("runtime completed");
  assert.equal(await secondFull, "full refresh completed");
  assert.deepEqual(calls, [
    { runtimeOnly: true, silent: true },
    { feedback: true, silent: false, runtimeOnly: false },
  ]);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    )


def test_refresh_coordinator_preserves_undefined_rejection() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

(async () => {
  const refresh = HarnessRuntime.createRefreshCoordinator(() => Promise.reject(undefined));
  let rejected = false;
  try {
    await refresh();
  } catch (error) {
    rejected = true;
    assert.equal(error, undefined);
  }
  assert.equal(rejected, true);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    )
