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
    let queuedFullOptions = null;

    function refresh(options = {}) {
      if (operationPromise) {
        if (!options.runtimeOnly && operationRuntimeOnly) {
          queuedFullOptions = {
            ...(queuedFullOptions || {}),
            ...options,
            runtimeOnly: false,
          };
        }
        return operationPromise;
      }

      operationRuntimeOnly = Boolean(options.runtimeOnly);
      const operation = Promise.resolve().then(() => runRefresh(options));
      operationPromise = operation.then(
        (value) => completeOperation({ rejected: false, value }),
        (error) => completeOperation({ rejected: true, error }),
      );
      return operationPromise;
    }

    function completeOperation({ rejected, value, error }) {
      const requiredFullRefresh = queuedFullOptions;
      queuedFullOptions = null;
      operationPromise = null;
      operationRuntimeOnly = false;
      if (requiredFullRefresh) {
        return refresh(requiredFullRefresh);
      }
      if (rejected) {
        throw error;
      }
      return value;
    }

    return refresh;
  }

  global.HarnessRuntime = Object.freeze({
    createApi,
    createRefreshCoordinator,
  });
})(typeof window === "undefined" ? globalThis : window);
