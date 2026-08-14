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


def test_listbox_navigation_index_supports_standard_keys_without_wrapping() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");
const navigate = HarnessRuntime.listboxNavigationIndex;

assert.equal(navigate("ArrowDown", 0, 3), 1);
assert.equal(navigate("ArrowDown", 2, 3), 2);
assert.equal(navigate("ArrowUp", 2, 3), 1);
assert.equal(navigate("ArrowUp", 0, 3), 0);
assert.equal(navigate("Home", 2, 3), 0);
assert.equal(navigate("End", 0, 3), 2);
assert.equal(navigate("PageDown", 1, 3), null);
assert.equal(navigate("ArrowDown", -1, 3), 1);
assert.equal(navigate("ArrowDown", 0, 0), null);
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


def test_runtime_text_selection_is_clamped_and_preserves_direction() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

assert.deepEqual(
  HarnessRuntime.normalizeRuntimeTextSelection(
    { selectionStart: 3, selectionEnd: 12, selectionDirection: "backward" },
    7,
  ),
  { selectionStart: 3, selectionEnd: 7, selectionDirection: "backward" },
);
assert.deepEqual(
  HarnessRuntime.normalizeRuntimeTextSelection(
    { selectionStart: -4, selectionEnd: 2, selectionDirection: "sideways" },
    5,
  ),
  { selectionStart: 0, selectionEnd: 2, selectionDirection: "none" },
);
assert.deepEqual(
  HarnessRuntime.normalizeRuntimeTextSelection(
    { selectionStart: 8, selectionEnd: 4, selectionDirection: "forward" },
    10,
  ),
  { selectionStart: 8, selectionEnd: 8, selectionDirection: "forward" },
);
assert.equal(HarnessRuntime.normalizeRuntimeTextSelection({ selectionStart: 1 }, 4), null);
assert.equal(HarnessRuntime.normalizeRuntimeTextSelection({}, undefined), null);
"""
    )


def test_refresh_coordinator_silent_join_recovers_failing_full_refresh() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

(async () => {
  const calls = [];
  let rejectInitial;
  const refresh = HarnessRuntime.createRefreshCoordinator((options) => {
    calls.push(options);
    if (calls.length === 1) {
      return new Promise((_resolve, reject) => {
        rejectInitial = reject;
      });
    }
    return Promise.resolve("silent recovery completed");
  });

  const initial = refresh({ feedback: true });
  await Promise.resolve();
  const persistedMutationRefresh = refresh({ silent: true });
  assert.strictEqual(initial, persistedMutationRefresh);
  rejectInitial(new Error("initial full refresh failed"));

  assert.equal(await initial, "silent recovery completed");
  assert.deepEqual(calls, [
    { feedback: true },
    { silent: true, runtimeOnly: false },
  ]);

  let attempts = 0;
  const alwaysFailingRefresh = HarnessRuntime.createRefreshCoordinator(async () => {
    attempts += 1;
    throw new Error(`refresh failed ${attempts}`);
  });
  const failingInitial = alwaysFailingRefresh({ feedback: true });
  const silentJoin = alwaysFailingRefresh({ silent: true });
  assert.strictEqual(failingInitial, silentJoin);
  assert.equal(await failingInitial, undefined);
  assert.equal(attempts, 2);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    )


def test_run_authorization_receipt_prompts_only_for_new_routes_and_tools() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

const scope = "research:team-a";
const primary = { kind: "pack", provider: "openai", model: "gpt5.5", slots: ["Planner"] };
const fallback = { kind: "pack_fallback", provider: "deepseek", model: "deepseek-v4-pro", slots: ["Planner"] };
const webTool = { name: "web_search", provider: "tavily" };
const prompts = [];
const confirmModels = (routes) => {
  prompts.push({ type: "models", targets: routes.map((route) => `${route.provider}/${route.model}`) });
  return true;
};
const confirmWeb = (tools) => {
  prompts.push({ type: "web", targets: tools.map((tool) => `${tool.name}/${tool.provider}`) });
  return true;
};

const disabled = HarnessRuntime.authorizeRunRoutes({
  scope,
  modelRoutes: [],
  webTools: [],
  confirmModels,
  confirmWeb,
});
assert.equal(disabled.authorized, true);
assert.equal(disabled.confirmRealModels, false);
assert.equal(disabled.confirmRealWeb, false);
assert.deepEqual(disabled.confirmedWebToolKeys, []);
assert.deepEqual(prompts, []);

const enabled = HarnessRuntime.authorizeRunRoutes({
  scope,
  receipt: disabled.receipt,
  modelRoutes: [primary, primary],
  webTools: [webTool, webTool],
  confirmModels,
  confirmWeb,
});
assert.equal(enabled.authorized, true);
assert.equal(enabled.confirmRealModels, true);
assert.equal(enabled.confirmRealWeb, true);
assert.deepEqual(enabled.confirmedWebToolKeys, ["web_search"]);
assert.equal(Object.isFrozen(enabled.confirmedWebToolKeys), true);
assert.deepEqual(enabled.confirmedWebToolRoutes, [{ name: "web_search", provider: "tavily" }]);
assert.equal(Object.isFrozen(enabled.confirmedWebToolRoutes), true);
assert.equal(Object.isFrozen(enabled.confirmedWebToolRoutes[0]), true);
assert.deepEqual(enabled.receipt.webToolKeys, [JSON.stringify(["web_search", "tavily"])]);
assert.deepEqual(prompts, [
  { type: "models", targets: ["openai/gpt5.5"] },
  { type: "web", targets: ["web_search/tavily"] },
]);

prompts.length = 0;
const unchanged = HarnessRuntime.authorizeRunRoutes({
  scope,
  receipt: enabled.receipt,
  modelRoutes: [primary],
  webTools: [webTool],
  confirmModels,
  confirmWeb,
});
assert.equal(unchanged.authorized, true);
assert.deepEqual(unchanged.confirmedWebToolKeys, ["web_search"]);
assert.deepEqual(prompts, []);

const newFallback = HarnessRuntime.authorizeRunRoutes({
  scope,
  receipt: unchanged.receipt,
  modelRoutes: [primary, fallback],
  webTools: [webTool],
  confirmModels,
  confirmWeb,
});
assert.equal(newFallback.authorized, true);
assert.deepEqual(prompts, [
  { type: "models", targets: ["deepseek/deepseek-v4-pro"] },
]);

const rejected = HarnessRuntime.authorizeRunRoutes({
  scope,
  receipt: newFallback.receipt,
  modelRoutes: [primary, fallback, { ...fallback, model: "deepseek-new" }],
  webTools: [webTool],
  confirmModels: () => false,
  confirmWeb,
});
assert.equal(rejected.authorized, false);
assert.equal(rejected.cancelled, "models");
"""
    )


def test_run_authorization_reprompts_when_same_tool_changes_provider_identity() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

const prompts = [];
const authorize = (receipt, provider) => HarnessRuntime.authorizeRunRoutes({
  scope: "research:default",
  receipt,
  modelRoutes: [],
  webTools: [{ name: "web_search", provider }],
  confirmModels: () => true,
  confirmWeb: (tools) => {
    prompts.push(tools.map((tool) => `${tool.name}/${tool.provider}`));
    return true;
  },
});

const tavily = authorize(null, "tavily");
const edge = authorize(tavily.receipt, "edge");

assert.deepEqual(prompts, [["web_search/tavily"], ["web_search/edge"]]);
assert.deepEqual(edge.confirmedWebToolKeys, ["web_search"]);
assert.deepEqual(edge.confirmedWebToolRoutes, [{ name: "web_search", provider: "edge" }]);
assert.deepEqual(edge.receipt.webToolKeys, [
  JSON.stringify(["web_search", "tavily"]),
  JSON.stringify(["web_search", "edge"]),
]);
"""
    )


def test_vision_preprocess_real_routes_use_single_primary_route_contract() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

const providers = [
  { name: "openai", enabled: true, real_calls: true },
  { name: "deepseek", enabled: false, real_calls: true },
  { name: "local", enabled: true, real_calls: false },
];
const inputs = {
  vision_preprocess: {
    provider: "openai",
    model: "gpt-5.5",
    allow_real_calls: true,
  },
};

assert.deepEqual(HarnessRuntime.collectVisionPreprocessRealModelRoutes(inputs, providers), [
  {
    kind: "vision_preprocess",
    provider: "openai",
    model: "gpt-5.5",
    slots: ["vision_preprocess"],
  },
]);
assert.throws(
  () => HarnessRuntime.collectVisionPreprocessRealModelRoutes(
    { vision_preprocess: { provider: "openai" } },
    providers,
  ),
  /vision_preprocess/,
);
assert.deepEqual(
  HarnessRuntime.collectVisionPreprocessRealModelRoutes(
    { vision_preprocess: { provider: "local", model: "local-vision" } },
    providers,
  ),
  [],
);
"""
    )


def test_pack_real_web_tools_intersect_step_agent_permissions_and_catalog() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

const pack = {
  agents: [
    { role: "Reader", tool_permissions: ["web_search", "browser_fetch"] },
    { role: "Writer", tool_permissions: ["fetch_page"] },
  ],
  steps: [
    {
      name: "read",
      agent_role: "Reader",
      allowed_tools: ["web_search", "browser_search", "browser_fetch"],
    },
    { name: "write", agent_role: "Writer", allowed_tools: ["fetch_page"] },
  ],
};
const providers = [
  { name: "web_search", provider: "tavily", enabled: true, real_calls: true },
  { name: "fetch_page", provider: "tavily", enabled: true, real_calls: true },
  { name: "browser_search", provider: "edge", enabled: true, real_calls: true },
  { name: "browser_fetch", provider: "edge", enabled: false, real_calls: true },
  { name: "other_tool", provider: "remote", enabled: true, real_calls: true },
];

assert.deepEqual(HarnessRuntime.collectPackRealWebTools(pack, providers), [
  { name: "fetch_page", provider: "tavily", enabled: true, real_calls: true },
  { name: "web_search", provider: "tavily", enabled: true, real_calls: true },
]);
"""
    )


def test_pack_real_model_routes_include_disabled_fallbacks_and_deduplicate_targets() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

const providers = [
  { name: "openai", enabled: true, real_calls: true },
  { name: "deepseek", enabled: false, real_calls: true },
  { name: "mock", enabled: true, real_calls: false },
];
const pack = {
  agents: [
    {
      id: "planner-a",
      role: "Planner A",
      model_config: {
        provider: "openai",
        model: "gpt5.5",
        fallbacks: [
          { provider: "deepseek", model: "deepseek-v4-pro" },
          { provider: "mock", model: "mock-planner" },
        ],
      },
    },
    {
      id: "planner-b",
      role: "Planner B",
      model_config: {
        provider: "openai",
        model: "gpt5.5",
        fallbacks: [{ provider: "deepseek", model: "deepseek-v4-pro" }],
      },
    },
  ],
};

assert.deepEqual(HarnessRuntime.collectPackRealModelRoutes(pack, providers), [
  {
    kind: "pack",
    provider: "openai",
    model: "gpt5.5",
    slots: ["Planner A", "Planner B"],
  },
  {
    kind: "pack_fallback",
    provider: "deepseek",
    model: "deepseek-v4-pro",
    slots: ["Planner A", "Planner B"],
  },
]);
"""
    )


def test_create_task_after_run_authorization_never_mutates_when_cancelled() -> None:
    _run_node(
        r"""
const assert = require("node:assert/strict");

(async () => {
  const calls = [];
  const cancelled = await HarnessRuntime.createTaskAfterRunAuthorization({
    authorization: { authorized: false, cancelled: "web" },
    createTask: async () => calls.push("create"),
    submitRun: async () => calls.push("run"),
  });
  assert.equal(cancelled.created, false);
  assert.deepEqual(calls, []);

  const receipt = { version: "run-route-authorization-v1", scope: "research", modelRouteKeys: [], webToolKeys: [] };
  const accepted = await HarnessRuntime.createTaskAfterRunAuthorization({
    authorization: {
      authorized: true,
      receipt,
      confirmedWebToolKeys: Object.freeze(["web_search"]),
    },
    createTask: async () => {
      calls.push("create");
      return { id: "task-1" };
    },
    submitRun: async (task, receivedAuthorization) => {
      calls.push(`run:${task.id}`);
      assert.equal(receivedAuthorization.authorized, true);
      assert.strictEqual(receivedAuthorization.receipt, receipt);
      assert.deepEqual(
        receivedAuthorization.confirmedWebToolKeys,
        ["web_search"],
      );
    },
  });
  assert.equal(accepted.created, true);
  assert.deepEqual(calls, ["create", "run:task-1"]);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    )
