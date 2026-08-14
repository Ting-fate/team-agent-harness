(function initializeHarnessRuntime(global) {
  "use strict";

  function createApi(overrides = {}) {
    const fetchImpl = overrides.fetchImpl || global.fetch.bind(global);
    const setTimeoutImpl = overrides.setTimeoutImpl || global.setTimeout.bind(global);
    const clearTimeoutImpl = overrides.clearTimeoutImpl || global.clearTimeout.bind(global);

    return async function api(path, options = {}) {
      const { headers = {}, timeoutMs = 90000, signal, ...requestOptions } = options;
      const controller = new AbortController();
      const abortFromCaller = () => controller.abort();
      if (signal?.aborted) {
        controller.abort();
      } else {
        signal?.addEventListener("abort", abortFromCaller, { once: true });
      }
      const timeoutId = setTimeoutImpl(() => controller.abort(), timeoutMs);

      try {
        const response = await fetchImpl(path, {
          ...requestOptions,
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            ...headers,
          },
          signal: controller.signal,
        });
        const text = await response.text();
        const contentType = response.headers.get("content-type") || "";
        let payload = text;
        if (text && contentType.includes("application/json")) {
          payload = JSON.parse(text);
        }
        if (!response.ok) {
          const message = payload?.detail || `请求失败：${response.status}`;
          throw new Error(Array.isArray(message) ? message.map((item) => item.msg).join("; ") : String(message));
        }
        return payload;
      } catch (error) {
        if (controller.signal.aborted) {
          throw new Error(`请求超时或已取消：${path}`);
        }
        throw error;
      } finally {
        clearTimeoutImpl(timeoutId);
        signal?.removeEventListener("abort", abortFromCaller);
      }
    };
  }

  function createRefreshCoordinator(runRefresh) {
    let operationPromise = null;
    let operationRuntimeOnly = false;
    let operationSilent = false;
    let queuedFullOptions = null;
    let queuedSilentRecoveryOptions = null;

    function refresh(options = {}) {
      if (operationPromise) {
        if (!options.runtimeOnly && operationRuntimeOnly) {
          queuedFullOptions = {
            ...(queuedFullOptions || {}),
            ...options,
            runtimeOnly: false,
          };
        } else if (!options.runtimeOnly && options.silent && !operationSilent) {
          queuedSilentRecoveryOptions = {
            ...(queuedSilentRecoveryOptions || {}),
            ...options,
            runtimeOnly: false,
            silent: true,
          };
        }
        return operationPromise;
      }

      operationRuntimeOnly = Boolean(options.runtimeOnly);
      operationSilent = Boolean(options.silent);
      const operation = Promise.resolve().then(() => runRefresh(options));
      operationPromise = operation.then(
        (value) => completeOperation({ rejected: false, value }),
        (error) => completeOperation({ rejected: true, error }),
      );
      return operationPromise;
    }

    function completeOperation({ rejected, value, error }) {
      const requiredFullRefresh = queuedFullOptions;
      const silentRecovery = queuedSilentRecoveryOptions;
      const completedOperationWasSilent = operationSilent;
      queuedFullOptions = null;
      queuedSilentRecoveryOptions = null;
      operationPromise = null;
      operationRuntimeOnly = false;
      operationSilent = false;
      if (requiredFullRefresh) {
        return refresh(requiredFullRefresh);
      }
      if (rejected && silentRecovery) {
        return refresh(silentRecovery);
      }
      if (rejected && completedOperationWasSilent) {
        return undefined;
      }
      if (rejected) {
        throw error;
      }
      return value;
    }

    return refresh;
  }

  function listboxNavigationIndex(key, currentIndex, itemCount) {
    if (!Number.isInteger(itemCount) || itemCount <= 0) {
      return null;
    }
    const index = Number.isInteger(currentIndex) && currentIndex >= 0 && currentIndex < itemCount
      ? currentIndex
      : 0;
    if (key === "ArrowDown") {
      return Math.min(itemCount - 1, index + 1);
    }
    if (key === "ArrowUp") {
      return Math.max(0, index - 1);
    }
    if (key === "Home") {
      return 0;
    }
    if (key === "End") {
      return itemCount - 1;
    }
    return null;
  }

  function normalizeRuntimeTextSelection(snapshot, valueLength) {
    if (
      !snapshot ||
      !Number.isFinite(snapshot.selectionStart) ||
      !Number.isFinite(snapshot.selectionEnd) ||
      !Number.isFinite(valueLength)
    ) {
      return null;
    }
    const length = Math.max(0, Math.trunc(valueLength));
    const clamp = (value) => Math.min(length, Math.max(0, Math.trunc(value)));
    const selectionStart = clamp(snapshot.selectionStart);
    const selectionEnd = Math.max(selectionStart, clamp(snapshot.selectionEnd));
    const selectionDirection = ["forward", "backward", "none"].includes(snapshot.selectionDirection)
      ? snapshot.selectionDirection
      : "none";
    return { selectionStart, selectionEnd, selectionDirection };
  }

  const RUN_ROUTE_AUTHORIZATION_VERSION = "run-route-authorization-v1";
  const REAL_WEB_TOOL_NAMES = new Set([
    "web_search",
    "fetch_page",
    "browser_search",
    "browser_fetch",
  ]);

  function uniqueTargets(items, keyForItem) {
    if (!Array.isArray(items)) {
      throw new Error("运行授权目标必须是数组。");
    }
    const seen = new Set();
    return items.filter((item) => {
      const key = keyForItem(item);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
  }

  function modelRouteAuthorizationKey(route) {
    if (
      !route ||
      typeof route !== "object" ||
      typeof route.provider !== "string" ||
      !route.provider ||
      typeof route.model !== "string" ||
      !route.model
    ) {
      throw new Error("模型授权路由不完整。");
    }
    const slots = Array.isArray(route.slots)
      ? [...new Set(route.slots.filter((slot) => typeof slot === "string" && slot))].sort()
      : [];
    return JSON.stringify([
      typeof route.kind === "string" ? route.kind : "",
      typeof route.family === "string" ? route.family : "",
      route.provider,
      route.model,
      slots,
    ]);
  }

  function webToolAuthorizationKey(tool) {
    if (
      !tool ||
      typeof tool !== "object" ||
      typeof tool.name !== "string" ||
      !tool.name ||
      typeof tool.provider !== "string" ||
      !tool.provider
    ) {
      throw new Error("联网工具授权目标不完整。");
    }
    return JSON.stringify([tool.name, tool.provider]);
  }

  function compareWebToolRoutes(left, right) {
    if (left.name !== right.name) {
      return left.name < right.name ? -1 : 1;
    }
    if (left.provider === right.provider) {
      return 0;
    }
    return left.provider < right.provider ? -1 : 1;
  }

  function frozenConfirmedWebToolRoutes(tools) {
    const routes = uniqueTargets(tools, webToolAuthorizationKey)
      .map((tool) => ({ name: tool.name, provider: tool.provider }))
      .sort(compareWebToolRoutes)
      .map((route) => Object.freeze(route));
    return Object.freeze(routes);
  }

  function createRunAuthorizationReceipt(scope, modelRouteKeys = [], webToolKeys = []) {
    return Object.freeze({
      version: RUN_ROUTE_AUTHORIZATION_VERSION,
      scope,
      modelRouteKeys: Object.freeze([...new Set(modelRouteKeys)]),
      webToolKeys: Object.freeze([...new Set(webToolKeys)]),
    });
  }

  function normalizedRunAuthorizationReceipt(receipt, scope) {
    if (
      receipt?.version !== RUN_ROUTE_AUTHORIZATION_VERSION ||
      receipt?.scope !== scope ||
      !Array.isArray(receipt?.modelRouteKeys) ||
      !receipt.modelRouteKeys.every((key) => typeof key === "string") ||
      !Array.isArray(receipt?.webToolKeys) ||
      !receipt.webToolKeys.every((key) => typeof key === "string")
    ) {
      return createRunAuthorizationReceipt(scope);
    }
    return createRunAuthorizationReceipt(scope, receipt.modelRouteKeys, receipt.webToolKeys);
  }

  function authorizeRunRoutes({
    scope,
    receipt = null,
    modelRoutes,
    webTools,
    confirmModels,
    confirmWeb,
  }) {
    if (typeof scope !== "string" || !scope) {
      throw new Error("运行授权范围不完整。");
    }
    const currentModelRoutes = uniqueTargets(modelRoutes, modelRouteAuthorizationKey);
    const currentWebTools = uniqueTargets(webTools, webToolAuthorizationKey);
    const currentReceipt = normalizedRunAuthorizationReceipt(receipt, scope);
    const coveredModelRoutes = new Set(currentReceipt.modelRouteKeys);
    const newModelRoutes = currentModelRoutes.filter(
      (route) => !coveredModelRoutes.has(modelRouteAuthorizationKey(route)),
    );
    if (newModelRoutes.length && (typeof confirmModels !== "function" || confirmModels(newModelRoutes) !== true)) {
      return { authorized: false, cancelled: "models", receipt: currentReceipt };
    }

    const modelRouteKeys = [
      ...currentReceipt.modelRouteKeys,
      ...newModelRoutes.map(modelRouteAuthorizationKey),
    ];
    const modelAuthorizedReceipt = createRunAuthorizationReceipt(
      scope,
      modelRouteKeys,
      currentReceipt.webToolKeys,
    );
    const coveredWebTools = new Set(modelAuthorizedReceipt.webToolKeys);
    const newWebTools = currentWebTools.filter(
      (tool) => !coveredWebTools.has(webToolAuthorizationKey(tool)),
    );
    if (newWebTools.length && (typeof confirmWeb !== "function" || confirmWeb(newWebTools) !== true)) {
      return { authorized: false, cancelled: "web", receipt: modelAuthorizedReceipt };
    }

    const confirmedWebToolRoutes = frozenConfirmedWebToolRoutes(currentWebTools);
    return {
      authorized: true,
      cancelled: null,
      receipt: createRunAuthorizationReceipt(
        scope,
        modelAuthorizedReceipt.modelRouteKeys,
        [...modelAuthorizedReceipt.webToolKeys, ...newWebTools.map(webToolAuthorizationKey)],
      ),
      confirmRealModels: currentModelRoutes.length > 0,
      confirmRealWeb: currentWebTools.length > 0,
      confirmedWebToolKeys: Object.freeze(
        [...new Set(confirmedWebToolRoutes.map((route) => route.name))].sort(),
      ),
      confirmedWebToolRoutes,
    };
  }

  function collectPackRealModelRoutes(pack, providers) {
    if (!pack || !Array.isArray(pack.agents) || !Array.isArray(providers)) {
      return [];
    }
    const realProviders = new Set(
      providers
        .filter((provider) => provider?.real_calls === true)
        .map((provider) => provider.name),
    );
    const grouped = new Map();

    pack.agents.forEach((agent) => {
      const config = agent?.model_config;
      const slot = typeof agent?.role === "string" && agent.role
        ? agent.role
        : typeof agent?.id === "string" && agent.id
          ? agent.id
          : null;
      if (!config || typeof config !== "object" || !slot) {
        return;
      }
      const candidates = [
        { kind: "pack", provider: config.provider, model: config.model },
        ...(Array.isArray(config.fallbacks)
          ? config.fallbacks.map((fallback) => ({
              kind: "pack_fallback",
              provider: fallback?.provider,
              model: fallback?.model,
            }))
          : []),
      ];
      candidates.forEach((candidate) => {
        if (
          typeof candidate.provider !== "string" ||
          typeof candidate.model !== "string" ||
          !candidate.model ||
          !realProviders.has(candidate.provider)
        ) {
          return;
        }
        const key = JSON.stringify([candidate.provider, candidate.model]);
        const existing = grouped.get(key);
        if (existing) {
          existing.slots.add(slot);
          if (candidate.kind === "pack") {
            existing.kind = "pack";
          }
          return;
        }
        grouped.set(key, {
          ...candidate,
          slots: new Set([slot]),
        });
      });
    });

    return [...grouped.values()].map((route) => ({
      kind: route.kind,
      provider: route.provider,
      model: route.model,
      slots: [...route.slots].sort(),
    }));
  }

  function collectVisionPreprocessRealModelRoutes(inputs, providers) {
    if (!inputs || typeof inputs !== "object" || Array.isArray(inputs)) {
      throw new Error("任务 inputs 不完整，无法验证 vision_preprocess 授权目标。");
    }
    const sidecar = inputs.vision_preprocess;
    if (sidecar === undefined || sidecar === null) {
      return [];
    }
    if (!sidecar || typeof sidecar !== "object" || Array.isArray(sidecar) || !Array.isArray(providers)) {
      throw new Error("vision_preprocess 配置不完整。");
    }
    const providerCatalog = new Map(providers.map((provider) => [provider?.name, provider]));
    const candidate = {
      kind: "vision_preprocess",
      provider: sidecar.provider,
      model: sidecar.model,
      allow_real_calls: sidecar.allow_real_calls,
    };
    if (
      typeof candidate.provider !== "string" ||
      !candidate.provider ||
      typeof candidate.model !== "string" ||
      !candidate.model
    ) {
      throw new Error("vision_preprocess 路由不完整。");
    }
    const provider = providerCatalog.get(candidate.provider);
    if (!provider) {
      throw new Error(`vision_preprocess 渠道 ${candidate.provider} 不在当前模型目录中。`);
    }
    if (candidate.provider === "mock") {
      throw new Error("vision_preprocess 不能使用 mock 渠道。");
    }
    if (provider.real_calls !== true) {
      return [];
    }
    if (candidate.allow_real_calls !== true) {
      throw new Error("vision_preprocess 真实路由必须显式 allow_real_calls=true。");
    }
    return [{
      kind: candidate.kind,
      provider: candidate.provider,
      model: candidate.model,
      slots: ["vision_preprocess"],
    }];
  }

  function collectPackRealWebTools(pack, providers) {
    if (
      !pack ||
      !Array.isArray(pack.agents) ||
      !Array.isArray(pack.steps) ||
      !Array.isArray(providers)
    ) {
      throw new Error("工作流或联网工具目录不完整。");
    }
    const permissionsByRole = new Map();
    pack.agents.forEach((agent) => {
      if (
        typeof agent?.role !== "string" ||
        !agent.role ||
        !Array.isArray(agent.tool_permissions) ||
        permissionsByRole.has(agent.role)
      ) {
        throw new Error("工作流 Agent 工具权限不完整。");
      }
      permissionsByRole.set(agent.role, new Set(agent.tool_permissions));
    });
    const permittedToolNames = new Set();
    pack.steps.forEach((step) => {
      const agentPermissions = permissionsByRole.get(step?.agent_role);
      if (!agentPermissions || !Array.isArray(step.allowed_tools)) {
        throw new Error("工作流步骤工具权限不完整。");
      }
      step.allowed_tools.forEach((toolName) => {
        if (REAL_WEB_TOOL_NAMES.has(toolName) && agentPermissions.has(toolName)) {
          permittedToolNames.add(toolName);
        }
      });
    });
    const selected = providers.filter((provider) => (
      provider?.enabled === true &&
      provider?.real_calls === true &&
      permittedToolNames.has(provider.name)
    ));
    return uniqueTargets(selected, webToolAuthorizationKey).sort(compareWebToolRoutes);
  }

  async function createTaskAfterRunAuthorization({ authorization, createTask, submitRun }) {
    if (authorization?.authorized !== true) {
      return { created: false, authorization };
    }
    if (typeof createTask !== "function" || typeof submitRun !== "function") {
      throw new Error("任务创建与运行提交回调不完整。");
    }
    const task = await createTask();
    const submission = await submitRun(task, authorization);
    return { created: true, task, submission, authorization };
  }

  global.HarnessRuntime = Object.freeze({
    authorizeRunRoutes,
    collectPackRealModelRoutes,
    collectPackRealWebTools,
    collectVisionPreprocessRealModelRoutes,
    createApi,
    createRefreshCoordinator,
    createTaskAfterRunAuthorization,
    listboxNavigationIndex,
    normalizeRuntimeTextSelection,
  });
})(typeof window === "undefined" ? globalThis : window);
