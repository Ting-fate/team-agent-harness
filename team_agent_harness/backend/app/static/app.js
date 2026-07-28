const state = {
  packs: [],
  selectedPackDetail: null,
  modelProviders: [],
  toolProviders: [],
  agents: [],
  roleCards: [],
  agentBindings: [],
  skills: [],
  skillBindings: [],
  skillAutoRoutes: [],
  selectedSkillId: null,
  selectedRoleCardId: null,
  tasks: [],
  runs: [],
  selectedTaskId: null,
  selectedRunId: null,
  selectedRunDetail: null,
  followLatestActiveRun: false,
  detailRequestToken: 0,
  lastRefreshAt: null,
  lastRefreshError: null,
  recordSearch: "",
  recordStatusFilter: "all",
  writebackPreviews: {},
  pendingRuntimeActions: new Set(),
  pendingWritebackActions: new Map(),
  activeTab: "chain",
  activeView: "dashboardView",
  autoRefreshTimer: null,
  isBusy: false,
};

const els = {
  viewEyebrow: document.querySelector("#viewEyebrow"),
  viewTitle: document.querySelector("#viewTitle"),
  viewSubtitle: document.querySelector("#viewSubtitle"),
  healthBadge: document.querySelector("#healthBadge"),
  refreshButton: document.querySelector("#refreshButton"),
  taskForm: document.querySelector("#taskForm"),
  workflowPack: document.querySelector("#workflowPack"),
  taskTitle: document.querySelector("#taskTitle"),
  taskGoal: document.querySelector("#taskGoal"),
  taskInputs: document.querySelector("#taskInputs"),
  taskConstraints: document.querySelector("#taskConstraints"),
  taskCriteria: document.querySelector("#taskCriteria"),
  codeExampleButton: document.querySelector("#codeExampleButton"),
  researchExampleButton: document.querySelector("#researchExampleButton"),
  createTaskButton: document.querySelector("#createTaskButton"),
  runTaskButton: document.querySelector("#runTaskButton"),
  globalRunBar: document.querySelector("#globalRunBar"),
  globalRunTitle: document.querySelector("#globalRunTitle"),
  globalRunSummary: document.querySelector("#globalRunSummary"),
  globalRunMeta: document.querySelector("#globalRunMeta"),
  globalRunTaskButton: document.querySelector("#globalRunTaskButton"),
  globalTraceButton: document.querySelector("#globalTraceButton"),
  followActiveRunButton: document.querySelector("#followActiveRunButton"),
  runConsoleFreshness: document.querySelector("#runConsoleFreshness"),
  needsAttentionPanel: document.querySelector("#needsAttentionPanel"),
  runConsoleList: document.querySelector("#runConsoleList"),
  workflowCurrentPanel: document.querySelector("#workflowCurrentPanel"),
  workflowCurrentBadge: document.querySelector("#workflowCurrentBadge"),
  workflowCurrentSummary: document.querySelector("#workflowCurrentSummary"),
  workflowRunCurrentButton: document.querySelector("#workflowRunCurrentButton"),
  workflowTraceCurrentButton: document.querySelector("#workflowTraceCurrentButton"),
  runSelectedButton: document.querySelector("#runSelectedButton"),
  summaryCount: document.querySelector("#summaryCount"),
  recordSearch: document.querySelector("#recordSearch"),
  recordStatusFilter: document.querySelector("#recordStatusFilter"),
  clearRecordFiltersButton: document.querySelector("#clearRecordFiltersButton"),
  recordFilterSummary: document.querySelector("#recordFilterSummary"),
  packCount: document.querySelector("#packCount"),
  agentCount: document.querySelector("#agentCount"),
  runCount: document.querySelector("#runCount"),
  taskList: document.querySelector("#taskList"),
  runList: document.querySelector("#runList"),
  selectedRunBadge: document.querySelector("#selectedRunBadge"),
  selectedRunMeta: document.querySelector("#selectedRunMeta"),
  runtimeStatus: document.querySelector("#runtimeStatus"),
  selectedPackBadge: document.querySelector("#selectedPackBadge"),
  providerOverview: document.querySelector("#providerOverview"),
  packOverview: document.querySelector("#packOverview"),
  roleCardCount: document.querySelector("#roleCardCount"),
  roleCardList: document.querySelector("#roleCardList"),
  roleCardForm: document.querySelector("#roleCardForm"),
  roleCardId: document.querySelector("#roleCardId"),
  roleCardName: document.querySelector("#roleCardName"),
  roleCardDescription: document.querySelector("#roleCardDescription"),
  roleCardColor: document.querySelector("#roleCardColor"),
  roleCardEmoji: document.querySelector("#roleCardEmoji"),
  roleCardVibe: document.querySelector("#roleCardVibe"),
  roleCardContent: document.querySelector("#roleCardContent"),
  roleCardSaveBadge: document.querySelector("#roleCardSaveBadge"),
  newRoleCardButton: document.querySelector("#newRoleCardButton"),
  deleteRoleCardButton: document.querySelector("#deleteRoleCardButton"),
  agentBindingForm: document.querySelector("#agentBindingForm"),
  bindingAgent: document.querySelector("#bindingAgent"),
  bindingProvider: document.querySelector("#bindingProvider"),
  bindingModel: document.querySelector("#bindingModel"),
  bindingRoleCard: document.querySelector("#bindingRoleCard"),
  bindingReasoningEffort: document.querySelector("#bindingReasoningEffort"),
  bindingTemperature: document.querySelector("#bindingTemperature"),
  bindingMaxTokens: document.querySelector("#bindingMaxTokens"),
  bindingAllowRealCalls: document.querySelector("#bindingAllowRealCalls"),
  deleteBindingButton: document.querySelector("#deleteBindingButton"),
  applyGptPresetButton: document.querySelector("#applyGptPresetButton"),
  applyDeepSeekPresetButton: document.querySelector("#applyDeepSeekPresetButton"),
  saveInstitutionalPresetButton: document.querySelector("#saveInstitutionalPresetButton"),
  agentBindingList: document.querySelector("#agentBindingList"),
  skillCount: document.querySelector("#skillCount"),
  skillList: document.querySelector("#skillList"),
  skillPreview: document.querySelector("#skillPreview"),
  skillPreviewBadge: document.querySelector("#skillPreviewBadge"),
  refreshSkillsButton: document.querySelector("#refreshSkillsButton"),
  skillBindingForm: document.querySelector("#skillBindingForm"),
  skillBindingAgent: document.querySelector("#skillBindingAgent"),
  skillBindingIds: document.querySelector("#skillBindingIds"),
  deleteSkillBindingButton: document.querySelector("#deleteSkillBindingButton"),
  skillBindingList: document.querySelector("#skillBindingList"),
  autoSkillRoutes: document.querySelector("#autoSkillRoutes"),
  failureSummary: document.querySelector("#failureSummary"),
  chainPanel: document.querySelector("#chainPanel"),
  tracePanel: document.querySelector("#tracePanel"),
  artifactPanel: document.querySelector("#artifactPanel"),
  evalPanel: document.querySelector("#evalPanel"),
  toast: document.querySelector("#toast"),
};

const viewMeta = {
  dashboardView: {
    eyebrow: "Run Console",
    title: "运行控制台",
    subtitle: "监督当前 task/run、人工动作、追踪产物和刷新状态。",
  },
  workflowView: {
    eyebrow: "工作流录入",
    title: "工作流",
    subtitle: "选择一个 workflow pack，填写任务目标，再交给多个 agent 协同处理。",
  },
  routingView: {
    eyebrow: "模型路由",
    title: "模型路由",
    subtitle: "查看模型渠道状态、每个智能体的模型配置，以及工作流执行步骤。",
  },
  roleCardsView: {
    eyebrow: "角色卡",
    title: "角色卡",
    subtitle: "管理本地角色模板，并把角色卡绑定到指定智能体。保存后需要重启服务生效。",
  },
  skillsView: {
    eyebrow: "Skill Library",
    title: "能力包",
    subtitle: "只读导入本地 SKILL.md，可手动绑定，也可按场景自动识别注入。",
  },
  recordsView: {
    eyebrow: "运行队列",
    title: "任务记录",
    subtitle: "选择任务、运行任务，并查看最近 run 的状态。",
  },
  traceView: {
    eyebrow: "执行控制台",
    title: "追踪产物",
    subtitle: "检查执行链路、追踪事件、产物、评估和本地写回风险。",
  },
};

const examples = {
  code_rd: {
    title: "为项目增加健康检查",
    goal: "完成一个小型代码研发协作任务：澄清需求、设计实现、准备 patch、测试、评审并交付总结。",
    inputs: { repository_path: "workspace/app", target: "health_check" },
    constraints: ["默认使用本地模拟路由；模型调用以当前服务端模型渠道路由为准。", "输出必须包含测试与评审状态。"],
    acceptance_criteria: ["生成最终报告。", "追踪事件中能看到移交记录与评估结果。"],
  },
  research: {
    title: "研究 multi-agent harness 架构",
    goal: "产出一份关于团队 multi-agent harness 的阶段性研究报告，覆盖 harness core、工作流包和可观察性。",
    inputs: { recency: "not_required", topic: "team multi-agent harness" },
    constraints: ["默认使用本地模拟搜索；如服务端已启用 Tavily 联网搜索，运行前需要确认。", "明确区分事实、假设和后续扩展。"],
    acceptance_criteria: ["生成最终报告。", "产物中包含来源摘要、研究笔记和验证报告。"],
  },
  code_rd_institutional: {
    title: "用制度化流程交付代码变更",
    goal: "通过 GPT 主线程和 DeepSeek 长上下文把控完成代码研发协作演示：阅读上下文、规划、审核、派发、执行分支、复核、汇总和最终审批。",
    inputs: { repository_path: "workspace/app", target: "institutional_code_rd" },
    constraints: ["默认使用本地模拟路由；模型调用以当前服务端模型渠道路由为准。", "GPT 负责编码、测试和最终审批；DeepSeek 负责长上下文阅读、质询和风险复核。"],
    acceptance_criteria: ["生成最终报告。", "运行详情能看到 GPT/DeepSeek 交叉讨论、执行分支和模型路由。"],
  },
};

const workflowPackDisplay = {
  code_rd: {
    label: "代码研发协作",
    description: "模拟代码研发流程：需求澄清、方案设计、补丁准备、测试、评审和交付总结",
  },
  code_rd_institutional: {
    label: "制度化代码研发协作",
    description: "GPT 主线程 + DeepSeek 长上下文把控：规划、审核、派发、执行、复核、汇总和最终审批",
  },
  research: {
    label: "知识研究协作",
    description: "模拟研究流程：研究计划、资料收集、阅读摘要、事实核验、报告撰写和评审",
  },
};

const agentRoleDisplay = {
  Clarifier: "需求澄清",
  Architect: "架构设计",
  Coder: "代码实现",
  Tester: "测试验证",
  Reviewer: "代码审查",
  Finalizer: "交付汇总",
  ContextReader: "上下文阅读",
  Planner: "规划主线程",
  ReviewGate: "计划审核",
  Dispatcher: "任务派发",
  ImplementationExecutor: "代码执行",
  TestExecutor: "测试执行",
  ContextReviewer: "上下文审查",
  Synthesizer: "结果汇总",
  FinalReviewer: "最终审查",
  FinalApprover: "最终审批",
  Searcher: "资料搜索",
  Reader: "资料阅读",
  Verifier: "事实核验",
  Writer: "报告撰写",
};

const providerDisplay = {
  mock: "本地模拟",
  openai: "OpenAI / GPT",
  deepseek: "DeepSeek",
  litellm_proxy: "LiteLLM 统一网关",
};

const routingPresets = {
  gptMainThread: {
    provider: "litellm_proxy",
    model: "gpt5.5",
    reasoning_effort: "xhigh",
    temperature: 0.2,
    max_tokens: "",
    allow_real_calls: true,
  },
  deepSeekLongContext: {
    provider: "litellm_proxy",
    model: "deepseek-v4-pro",
    reasoning_effort: "xhigh",
    temperature: 0.2,
    max_tokens: 4096,
    allow_real_calls: true,
  },
};

const institutionalDeepSeekRoles = new Set([
  "ContextReader",
  "ReviewGate",
  "ContextReviewer",
  "FinalReviewer",
]);

const api = window.HarnessRuntime.createApi();

function linesToArray(value) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseInputs() {
  const raw = els.taskInputs.value.trim();
  if (!raw) {
    return {};
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error("输入 JSON 格式不正确。");
  }
}

function statusLabel(status) {
  const labels = {
    queued: "排队中",
    running: "运行中",
    waiting: "等待中",
    failed: "失败",
    completed: "完成",
    approval_required: "待本地审批",
    approved: "已批准",
    rejected: "已拒绝",
    waiting_approval: "等待审批",
    recorded: "已记录",
    cancelled: "已取消",
  };
  return labels[status] || status;
}

function renderStatusPill(status, label = null) {
  return `<span class="status-pill ${escapeHtml(status || "muted")}">${escapeHtml(label || statusLabel(status || "muted"))}</span>`;
}

function renderEmptyState(message) {
  return `<div class="empty">${escapeHtml(message)}</div>`;
}

function workflowPackLabel(packName) {
  if (packName === "auto") {
    return "自动识别";
  }
  return workflowPackDisplay[packName]?.label || packName;
}

function workflowPackDescription(pack) {
  return workflowPackDisplay[pack.name]?.description || pack.description || "";
}

function workflowPackOptionText(pack) {
  const description = workflowPackDescription(pack);
  return description ? `${workflowPackLabel(pack.name)} - ${description}` : workflowPackLabel(pack.name);
}

function agentRoleLabel(role) {
  return agentRoleDisplay[role] || role || "-";
}

function agentOptionLabel(agent) {
  const packLabel = workflowPackLabel(agent.pack_name);
  return `${packLabel} / ${agentRoleLabel(agent.role)} ｜ ${agent.id}`;
}

function providerLabel(providerName) {
  return providerDisplay[providerName] || providerName || "-";
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function timestampValue(value) {
  if (!value) {
    return 0;
  }
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function runTimestamp(run) {
  return Math.max(timestampValue(run.started_at), timestampValue(run.finished_at), timestampValue(run.created_at));
}

function isActiveRunStatus(status) {
  return ["queued", "running", "waiting", "approval_required", "waiting_approval", "recorded"].includes(status);
}

function isFailedRunStatus(status) {
  return ["failed", "cancelled", "rejected"].includes(status);
}

function isDoneRunStatus(status) {
  return ["completed", "approved"].includes(status);
}

function runToastMessage(run) {
  if (isFailedRunStatus(run.status)) {
    return { message: `运行已停止：${statusLabel(run.status)}。`, tone: "danger" };
  }
  if (isActiveRunStatus(run.status)) {
    return { message: `运行已提交，当前状态：${statusLabel(run.status)}。`, tone: "ok" };
  }
  return { message: `运行已${statusLabel(run.status)}。`, tone: "ok" };
}

function runStatusGroup(run) {
  if (isActiveRunStatus(run.status)) {
    return "active";
  }
  if (isFailedRunStatus(run.status)) {
    return "failed";
  }
  if (isDoneRunStatus(run.status)) {
    return "done";
  }
  return "other";
}

function taskById(taskId) {
  return state.tasks.find((task) => task.id === taskId) || null;
}

function latestRunForTask(taskId) {
  return state.runs
    .filter((run) => run.task_id === taskId)
    .sort((left, right) => runTimestamp(right) - runTimestamp(left))[0] || null;
}

function preferredRun(runs = state.runs) {
  const sorted = [...runs].sort((left, right) => runTimestamp(right) - runTimestamp(left));
  return sorted.find((run) => isActiveRunStatus(run.status)) || sorted[0] || null;
}

function selectedOrPreferredRun() {
  return currentRun() || preferredRun();
}

function taskSortValue(task) {
  const latestRun = latestRunForTask(task.id);
  return latestRun ? runTimestamp(latestRun) : timestampValue(task.created_at);
}

function currentTask() {
  return taskById(state.selectedTaskId);
}

function currentRun() {
  return state.runs.find((run) => run.id === state.selectedRunId) || null;
}

function selectTask(taskId, options = {}) {
  state.selectedTaskId = taskId;
  const latestRun = latestRunForTask(taskId);
  if (options.runId !== undefined) {
    state.selectedRunId = options.runId;
  } else if (options.selectLatestRun !== false) {
    state.selectedRunId = latestRun?.id || null;
  }
  if (options.followLatestActiveRun === false) {
    state.followLatestActiveRun = false;
  }
}

function selectRun(runId, options = {}) {
  state.selectedRunId = runId;
  const run = currentRun();
  if (run) {
    state.selectedTaskId = run.task_id;
  }
  if (options.followLatestActiveRun === false) {
    state.followLatestActiveRun = false;
  }
}

function taskActivityGroup(task, index) {
  if (task.id === state.selectedTaskId) {
    return "selected";
  }
  const latestRun = latestRunForTask(task.id);
  if (!latestRun) {
    return "active";
  }
  if (latestRun && isActiveRunStatus(latestRun.status)) {
    return "active";
  }
  if (index < 5) {
    return "recent";
  }
  return "archive";
}

function groupTasks(tasks) {
  const groupedTasks = {
    selected: [],
    active: [],
    recent: [],
    archive: [],
  };
  tasks.forEach((task, index) => {
    groupedTasks[taskActivityGroup(task, index)].push(task);
  });
  return groupedTasks;
}

function groupRuns(runs) {
  const groupedRuns = {
    active: [],
    failed: [],
    done: [],
    other: [],
  };
  runs.forEach((run) => {
    groupedRuns[runStatusGroup(run)].push(run);
  });
  return groupedRuns;
}

function searchableTaskText(task) {
  const latestRun = latestRunForTask(task.id);
  return [
    task.id,
    task.title,
    task.goal,
    task.workflow_pack,
    latestRun?.id,
    latestRun?.status,
    latestRun?.current_step,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function searchableRunText(run) {
  const task = taskById(run.task_id);
  return [
    run.id,
    run.task_id,
    run.status,
    run.current_step,
    task?.title,
    task?.goal,
    task?.workflow_pack,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function runNeedsAttention(run) {
  return isFailedRunStatus(run.status) || ["approval_required", "waiting_approval", "recorded"].includes(run.status);
}

function runNeedsOperatorAction(run) {
  return ["approval_required", "waiting_approval", "recorded"].includes(run.status);
}

function taskMatchesRecordFilter(task) {
  if (task.id === state.selectedTaskId) {
    return true;
  }
  const search = state.recordSearch.trim().toLowerCase();
  const latestRun = latestRunForTask(task.id);
  if (search && !searchableTaskText(task).includes(search)) {
    return false;
  }
  switch (state.recordStatusFilter) {
    case "attention":
      return latestRun ? runNeedsAttention(latestRun) : false;
    case "active":
      return latestRun ? isActiveRunStatus(latestRun.status) : true;
    case "failed":
      return latestRun ? isFailedRunStatus(latestRun.status) : false;
    case "done":
      return latestRun ? isDoneRunStatus(latestRun.status) : false;
    case "unrun":
      return !latestRun;
    default:
      return true;
  }
}

function runMatchesRecordFilter(run) {
  if (run.id === state.selectedRunId) {
    return true;
  }
  const search = state.recordSearch.trim().toLowerCase();
  if (search && !searchableRunText(run).includes(search)) {
    return false;
  }
  switch (state.recordStatusFilter) {
    case "attention":
      return runNeedsAttention(run);
    case "active":
      return isActiveRunStatus(run.status);
    case "failed":
      return isFailedRunStatus(run.status);
    case "done":
      return isDoneRunStatus(run.status);
    case "unrun":
      return false;
    default:
      return true;
  }
}

function filteredTasks() {
  return state.tasks.filter(taskMatchesRecordFilter);
}

function filteredRuns() {
  return state.runs.filter(runMatchesRecordFilter);
}

function nextActionForRun(run) {
  if (!run) {
    return "运行所选任务";
  }
  if (runNeedsAttention(run)) {
    return "查看详情并处理";
  }
  if (isActiveRunStatus(run.status)) {
    return "监督执行";
  }
  if (isDoneRunStatus(run.status)) {
    return "查看产物";
  }
  return "查看详情";
}

function deriveNeedsAttention() {
  return state.runs
    .filter(runNeedsOperatorAction)
    .sort((left, right) => runTimestamp(right) - runTimestamp(left))
    .slice(0, 8)
    .map((run) => ({
      run,
      task: taskById(run.task_id),
      reason: isFailedRunStatus(run.status) ? statusLabel(run.status) : "需要本地处理",
    }));
}

function deriveRunConsoleItems() {
  const sorted = [...state.runs].sort((left, right) => runTimestamp(right) - runTimestamp(left));
  return {
    attention: sorted.filter(runNeedsOperatorAction).slice(0, 8),
    active: sorted.filter((run) => isActiveRunStatus(run.status) && !runNeedsAttention(run)).slice(0, 8),
    failed: sorted.filter((run) => isFailedRunStatus(run.status)).slice(0, 6),
    done: sorted.filter((run) => isDoneRunStatus(run.status)).slice(0, 6),
  };
}

function formatRefreshFreshness() {
  if (state.lastRefreshError) {
    return `刷新失败：${state.lastRefreshError}`;
  }
  if (!state.lastRefreshAt) {
    return "尚未刷新";
  }
  return `刷新：${new Date(state.lastRefreshAt).toLocaleTimeString("zh-CN", { hour12: false })}`;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeSelector(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(value);
  }
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function showToast(message, tone = "ok") {
  els.toast.textContent = message;
  els.toast.className = `toast visible ${tone}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.className = "toast";
  }, 2800);
}

function setBusy(isBusy) {
  state.isBusy = isBusy;
  els.createTaskButton.disabled = isBusy;
  els.refreshButton.disabled = isBusy;
  els.runTaskButton.disabled = isBusy || !els.workflowPack.value;
  els.workflowRunCurrentButton.disabled = isBusy || !state.selectedTaskId;
  els.workflowTraceCurrentButton.disabled = isBusy || !state.selectedRunId;
  els.runSelectedButton.disabled = isBusy || !state.selectedTaskId;
  els.globalRunTaskButton.disabled = isBusy || !state.selectedTaskId;
  els.globalTraceButton.disabled = isBusy || !state.selectedRunId;
}

function setActiveView(viewId) {
  if (!viewMeta[viewId]) {
    return;
  }
  state.activeView = viewId;
  document.querySelectorAll(".view-page").forEach((page) => {
    const isActive = page.id === viewId;
    page.classList.toggle("active", isActive);
    page.toggleAttribute("hidden", !isActive);
  });
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    const isActive = button.dataset.viewTarget === viewId;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-current", isActive ? "page" : "false");
  });
  const meta = viewMeta[viewId];
  els.viewEyebrow.textContent = meta.eyebrow;
  els.viewTitle.textContent = meta.title;
  els.viewSubtitle.textContent = meta.subtitle;
  if (window.location.hash !== `#${viewId}`) {
    window.history.replaceState(null, "", `#${viewId}`);
  }
  document.querySelector(".app-main")?.scrollTo({ top: 0, behavior: "smooth" });
  updateAutoRefresh();
}

function viewFromHash() {
  const viewId = window.location.hash.replace("#", "");
  return viewMeta[viewId] ? viewId : "dashboardView";
}

function renderCatalog() {
  const currentValue = els.workflowPack.value || "auto";
  els.workflowPack.innerHTML = [
    `<option value="auto">自动识别 - 复杂/高风险任务推荐制度化 DAG 子任务流程</option>`,
    ...state.packs.map((pack) => `<option value="${escapeHtml(pack.name)}">${escapeHtml(workflowPackOptionText(pack))}</option>`),
  ].join("");
  if (currentValue === "auto" || state.packs.some((pack) => pack.name === currentValue)) {
    els.workflowPack.value = currentValue;
  }
  els.packCount.textContent = state.packs.length;
  els.agentCount.textContent = state.agents.length;
  els.runTaskButton.disabled = state.isBusy || !els.workflowPack.value;
}

function selectedPack() {
  if (els.workflowPack.value === "auto") {
    return null;
  }
  if (state.selectedPackDetail?.name === els.workflowPack.value) {
    return state.selectedPackDetail;
  }
  return state.packs.find((pack) => pack.name === els.workflowPack.value) || null;
}

function formatModelConfig(agent) {
  const config = agent.model_config || {};
  const provider = config.provider || "mock";
  const model = config.model || "mock-model";
  return `${providerLabel(provider)} / ${model}`;
}

function coordinationLabel(value) {
  const labels = {
    controller: "主控线程",
    subagent: "子智能体",
    gate: "审核关口",
    synthesizer: "汇总者",
  };
  return labels[value] || value || "-";
}

function formatReturnContract(contract) {
  if (!contract) {
    return "-";
  }
  const parts = [
    `必需产物：${(contract.required_artifact_types || []).join(", ") || "-"}`,
    `摘要：${contract.require_summary ? "必需" : "可选"}`,
  ];
  if (contract.require_open_questions) {
    parts.push("开放问题：必需");
  }
  if (contract.require_risk_notes) {
    parts.push("风险说明：必需");
  }
  return parts.join(" ｜ ");
}

function formatSessionPolicy(policy) {
  if (!policy) {
    return "-";
  }
  return [
    `长期会话：${policy.persistent ? "是" : "否"}`,
    `恢复方式：${policy.resume_strategy || "无"}`,
    `需要审批：${policy.requires_approval ? "是" : "否"}`,
  ].join(" ｜ ");
}

function formatOwnership(ownership) {
  const entries = Object.entries(ownership || {});
  if (!entries.length) {
    return "-";
  }
  return entries
    .map(([resourceType, resources]) => `${resourceType}:${Array.isArray(resources) && resources.length ? resources.join(", ") : "-"}`)
    .join(" ｜ ");
}

function formatGateContext(inputContext) {
  const requiresArtifact = inputContext?.requires_artifact || [];
  return [
    `审批：${inputContext?.session_policy?.requires_approval ? "需要" : "不需要"}`,
    `评估通过：${inputContext?.requires_eval_pass ? "需要" : "不需要"}`,
    `上游产物：${requiresArtifact.length ? requiresArtifact.join(", ") : "-"}`,
  ].join(" ｜ ");
}

function modelConfigForAgent(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  return agent ? formatModelConfig(agent) : "-";
}

async function loadSelectedPackDetail() {
  if (!els.workflowPack.value || els.workflowPack.value === "auto") {
    state.selectedPackDetail = null;
    return;
  }
  state.selectedPackDetail = await api(`/workflow-packs/${encodeURIComponent(els.workflowPack.value)}`);
}

function renderPackOverview() {
  const pack = selectedPack();
  if (!pack) {
    els.selectedPackBadge.className = "status-pill muted";
    els.selectedPackBadge.textContent = "未加载";
    els.packOverview.className = "pack-overview empty";
    els.packOverview.textContent = "加载工作流后显示步骤、智能体、模型配置与评估检查。";
    return;
  }

  els.selectedPackBadge.className = "status-pill ok";
  els.selectedPackBadge.textContent = workflowPackLabel(pack.name);
  els.packOverview.className = "pack-overview";
  els.packOverview.innerHTML = `
    <article class="observer-block">
      <h4>协作模型</h4>
      <p>
        Harness 主控线程编排工作流；子智能体按步骤和角色独立执行并回传摘要、产物和风险；
        每个智能体通过模型配置路由到本地模拟、OpenAI、DeepSeek 或 LiteLLM 统一网关；执行方式和会话规则用来表达长期子会话或工程执行器边界。
        当前仅记录本地会话、任务与审批意图；本地批准后恢复模拟或模型步骤，不启动外部工程执行器进程。
      </p>
    </article>
    <article class="observer-block">
      <h4>执行步骤</h4>
      <ol class="observer-list">
        ${pack.steps
          .map((step, index) => `
            <li>
              <strong>${index + 1}. ${escapeHtml(step.name)} ｜ ${escapeHtml(step.agent_role)}</strong>
              阶段：${escapeHtml(step.phase || "-")} ｜ 协调角色：${escapeHtml(coordinationLabel(step.coordination_role))} ｜ 产出：${escapeHtml(step.produces_artifact_type || "-")}<br />
              执行方式：${escapeHtml(step.runtime || "model")}<br />
              会话规则：${escapeHtml(formatSessionPolicy(step.session_policy))}<br />
              控制步骤：${escapeHtml(step.controller_step || "-")}<br />
              步骤依赖：${escapeHtml((step.depends_on || []).join(", ") || "-")}<br />
              输入：${escapeHtml((step.required_inputs || []).join(", ") || "-")}<br />
              依赖产物：${escapeHtml((step.required_artifacts || []).join(", ") || "-")}<br />
              Gate：${escapeHtml(formatGateContext(step))}<br />
              Ownership：${escapeHtml(formatOwnership(step.ownership))}<br />
              返回要求：${escapeHtml(formatReturnContract(step.return_contract))}<br />
              允许工具：${escapeHtml((step.allowed_tools || []).join(", ") || "-")}
            </li>
          `)
          .join("")}
      </ol>
    </article>
    <article class="observer-block">
      <h4>智能体</h4>
      <ul class="observer-list">
        ${pack.agents
          .map((agent) => `
            <li>
              <strong>${escapeHtml(agentRoleLabel(agent.role))}</strong>
              <span class="muted-text"> ｜ ${escapeHtml(agent.role)}</span><br />
              模型配置：${escapeHtml(formatModelConfig(agent))}<br />
              工具权限：${escapeHtml((agent.tool_permissions || []).join(", ") || "-")}
            </li>
          `)
          .join("")}
      </ul>
    </article>
    <article class="observer-block">
      <h4>评估检查</h4>
      <ul class="observer-list">
        ${(pack.eval_checks || [])
          .map((check) => `
            <li>
              <strong>${escapeHtml(check.name)} ｜ ${escapeHtml(check.severity)}</strong>
              ${escapeHtml(check.description)}<br />
              需要：${escapeHtml((check.required_artifact_types || []).join(", ") || "-")}
            </li>
          `)
          .join("")}
      </ul>
    </article>
  `;
}

function renderProviderOverview() {
  if (!state.modelProviders.length && !state.toolProviders.length) {
    els.providerOverview.className = "provider-overview empty";
    els.providerOverview.textContent = "加载模型渠道和联网搜索状态后显示启用状态。";
    return;
  }

  els.providerOverview.className = "provider-overview";
  els.providerOverview.innerHTML = `
    <article class="observer-block">
      <h4>模型渠道</h4>
      <div class="provider-grid">
        ${state.modelProviders
          .map((provider) => {
            const tone = provider.real_calls ? "real" : "mock";
            return `
              <section class="provider-card ${tone}">
                <header>
                  <strong>${escapeHtml(provider.name)}</strong>
                  <span class="model-chip">${escapeHtml(provider.adapter)}</span>
                </header>
                <p>状态：${provider.enabled ? "启用" : "未启用"} ｜ 真实调用能力：${provider.real_calls ? "是" : "否"}</p>
                <p>凭据已配置：${provider.real_calls_configured ? "是" : "否"} ｜ 凭据：${provider.requires_credentials ? "需要" : "不需要"}</p>
                <p>${escapeHtml(provider.description)}</p>
              </section>
            `;
          })
          .join("")}
      </div>
    </article>
    <article class="observer-block">
      <h4>联网工具</h4>
      <div class="provider-grid">
        ${state.toolProviders
          .map((provider) => {
            const tone = provider.real_calls ? "real" : "mock";
            return `
              <section class="provider-card ${tone}">
                <header>
                  <strong>${escapeHtml(provider.name)}</strong>
                  <span class="model-chip">${escapeHtml(provider.adapter)}</span>
                </header>
                <p>服务：${escapeHtml(provider.provider)} ｜ 状态：${provider.enabled ? "启用" : "未启用"}</p>
                <p>真实联网：${provider.real_calls ? "是" : "否"} ｜ 凭据已配置：${provider.real_calls_configured ? "是" : "否"}</p>
                <p>Chrome/CDP：${provider.provider === "chrome" ? "Google Chrome 桥接" : provider.adapter === "browser_cdp" ? "本地 CDP 桥接" : "不适用"} ｜ 仅 Research 的 browser_search/browser_fetch 使用。</p>
                <p>${escapeHtml(provider.description)}</p>
              </section>
            `;
          })
          .join("")}
      </div>
    </article>
  `;
}

function roleCardLabel(card) {
  return card?.frontmatter?.name || card?.id || "未命名角色卡";
}

function bindingForAgent(agentId) {
  return state.agentBindings.find((binding) => binding.agent_id === agentId) || null;
}

function defaultModelForProvider(provider) {
  const defaults = {
    mock: "mock-model",
    litellm_proxy: "gpt5.5",
    openai: "gpt-5.5",
    deepseek: "deepseek-v4-pro",
  };
  return defaults[provider] || "";
}

function defaultReasoningEffortForModel(provider, model) {
  if (["openai", "deepseek", "litellm_proxy"].includes(provider)) {
    return "xhigh";
  }
  return "";
}

function preferredRouteForAgent(agent) {
  if (!agent) {
    return routingPresets.gptMainThread;
  }
  if (agent.pack_name === "code_rd_institutional" && institutionalDeepSeekRoles.has(agent.role)) {
    return routingPresets.deepSeekLongContext;
  }
  if (agent.pack_name === "research" && ["Reader", "Verifier", "Reviewer"].includes(agent.role)) {
    return routingPresets.deepSeekLongContext;
  }
  if (agent.role === "Reviewer" || agent.role === "ContextReviewer" || agent.role === "FinalReviewer") {
    return routingPresets.deepSeekLongContext;
  }
  return routingPresets.gptMainThread;
}

function applyRoutePresetToForm(preset) {
  els.bindingProvider.value = preset.provider;
  els.bindingModel.value = preset.model;
  els.bindingReasoningEffort.value = preset.reasoning_effort || "";
  els.bindingTemperature.value = preset.temperature ?? "";
  els.bindingMaxTokens.value = preset.max_tokens ?? "";
  els.bindingAllowRealCalls.checked = Boolean(preset.allow_real_calls);
}

function resetRoleCardForm() {
  state.selectedRoleCardId = null;
  els.roleCardId.disabled = false;
  els.roleCardId.value = "";
  els.roleCardName.value = "";
  els.roleCardDescription.value = "";
  els.roleCardColor.value = "";
  els.roleCardEmoji.value = "";
  els.roleCardVibe.value = "";
  els.roleCardContent.value = "# 新角色\n\n你负责...";
  els.roleCardSaveBadge.className = "status-pill muted";
  els.roleCardSaveBadge.textContent = "新建";
  renderRoleCards();
}

async function loadRoleCardIntoForm(roleCardId) {
  const card = await api(`/role-cards/${encodeURIComponent(roleCardId)}`);
  state.selectedRoleCardId = card.id;
  els.roleCardId.disabled = true;
  els.roleCardId.value = card.id;
  els.roleCardName.value = card.frontmatter.name || card.id;
  els.roleCardDescription.value = card.frontmatter.description || "";
  els.roleCardColor.value = card.frontmatter.color || "";
  els.roleCardEmoji.value = card.frontmatter.emoji || "";
  els.roleCardVibe.value = card.frontmatter.vibe || "";
  els.roleCardContent.value = card.content || "";
  els.roleCardSaveBadge.className = "status-pill ok";
  els.roleCardSaveBadge.textContent = "编辑中";
  renderRoleCards();
}

function renderRoleCards() {
  els.roleCardCount.textContent = `${state.roleCards.length} 张`;
  if (!state.roleCards.length) {
    els.roleCardList.className = "item-list empty";
    els.roleCardList.textContent = "暂无角色卡";
  } else {
    els.roleCardList.className = "item-list";
    els.roleCardList.innerHTML = state.roleCards
      .map((card) => {
        const active = card.id === state.selectedRoleCardId ? " active" : "";
        return `
          <article class="item-card${active}" data-role-card-id="${escapeHtml(card.id)}" tabindex="0">
            <h4>${escapeHtml(roleCardLabel(card))}</h4>
            <p>ID：${escapeHtml(card.id)} ｜ 路径：${escapeHtml(card.path)}</p>
            <p>${escapeHtml(card.frontmatter.description || "无描述")}</p>
          </article>
        `;
      })
      .join("");
  }

  const currentAgent = els.bindingAgent.value;
  els.bindingAgent.innerHTML = state.agents
    .map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agentOptionLabel(agent))}</option>`)
    .join("");
  if (currentAgent && state.agents.some((agent) => agent.id === currentAgent)) {
    els.bindingAgent.value = currentAgent;
  }

  const providers = state.modelProviders.filter((provider) => ["mock", "openai", "deepseek", "litellm_proxy"].includes(provider.name));
  const currentProvider = els.bindingProvider.value;
  els.bindingProvider.innerHTML = providers
    .map((provider) => `<option value="${escapeHtml(provider.name)}">${escapeHtml(providerLabel(provider.name))} ｜ ${escapeHtml(provider.name)} ｜ ${provider.real_calls ? "真实调用" : "模拟"}</option>`)
    .join("");
  if (currentProvider && providers.some((provider) => provider.name === currentProvider)) {
    els.bindingProvider.value = currentProvider;
  }

  const currentRoleCard = els.bindingRoleCard.value;
  els.bindingRoleCard.innerHTML = [
    '<option value="">不绑定角色卡</option>',
    ...state.roleCards.map((card) => `<option value="${escapeHtml(card.id)}">${escapeHtml(roleCardLabel(card))}</option>`),
  ].join("");
  if (currentRoleCard && state.roleCards.some((card) => card.id === currentRoleCard)) {
    els.bindingRoleCard.value = currentRoleCard;
  }
  syncBindingFormFromSelectedAgent(false);
  renderAgentBindings();
}

function syncBindingFormFromSelectedAgent(overwrite = true) {
  const agentId = els.bindingAgent.value;
  if (!agentId) {
    return;
  }
  const binding = bindingForAgent(agentId);
  const agent = state.agents.find((item) => item.id === agentId);
  const modelConfig = agent?.model_config || {};
  if (!overwrite && (els.bindingModel.value || binding)) {
    return;
  }
  const provider = binding?.provider || modelConfig.provider || "mock";
  const model = binding?.model || modelConfig.model || defaultModelForProvider(provider);
  els.bindingProvider.value = provider;
  els.bindingModel.value = model;
  els.bindingRoleCard.value = binding?.role_card_id || "";
  els.bindingReasoningEffort.value =
    binding?.reasoning_effort || modelConfig.reasoning_effort || defaultReasoningEffortForModel(provider, model);
  els.bindingTemperature.value = binding?.temperature ?? "";
  els.bindingMaxTokens.value = binding?.max_tokens ?? "";
  els.bindingAllowRealCalls.checked = Boolean(binding?.allow_real_calls);
}

function renderAgentBindings() {
  if (!state.agentBindings.length) {
    els.agentBindingList.className = "provider-overview empty";
    els.agentBindingList.textContent = "暂无绑定配置。";
    return;
  }
  els.agentBindingList.className = "provider-overview";
  els.agentBindingList.innerHTML = `
    <article class="observer-block">
      <h4>当前本地绑定</h4>
      <ul class="observer-list">
        ${state.agentBindings
          .map((binding) => `
            <li>
              <strong>${escapeHtml(agentLabel(binding.agent_id))}</strong>
              模型渠道：${escapeHtml(providerLabel(binding.provider))} ｜ ${escapeHtml(binding.provider)} ｜ 模型：${escapeHtml(binding.model)}<br />
              角色卡：${escapeHtml(binding.role_card_id || "未绑定")} ｜ 角色文件：${escapeHtml(binding.role_file || "-")}<br />
              思考强度：${escapeHtml(binding.reasoning_effort || "-")} ｜ 真实调用：${binding.allow_real_calls ? "允许" : "否"} ｜ 保存后需要重启服务生效
            </li>
          `)
          .join("")}
      </ul>
    </article>
  `;
}

function skillLabel(skill) {
  return skill?.name || skill?.skill_id || "未命名能力包";
}

function skillBindingForAgent(agentId) {
  return state.skillBindings.find((binding) => binding.agent_id === agentId) || null;
}

function autoSkillRoutesForAgent(agentId) {
  return state.skillAutoRoutes.filter((route) => route.agent_id === agentId);
}

function renderSkills() {
  if (!els.skillList) {
    return;
  }
  els.skillCount.textContent = `${state.skills.length} 个`;
  if (!state.skills.length) {
    els.skillList.className = "item-list empty";
    els.skillList.textContent = "暂无能力包";
  } else {
    els.skillList.className = "item-list";
    els.skillList.innerHTML = state.skills
      .map((skill) => {
        const active = skill.skill_id === state.selectedSkillId ? " active" : "";
        const flags = (skill.risk_flags || []).join(", ") || "无";
        return `
          <article class="item-card${active}" data-skill-id="${escapeHtml(skill.skill_id)}" tabindex="0">
            <h4>${escapeHtml(skillLabel(skill))}</h4>
            <p>ID：${escapeHtml(skill.skill_id)} ｜ 大小：${escapeHtml(skill.size)}</p>
            <p>脚本存在但禁用：${skill.has_scripts ? "是" : "否"} ｜ 风险标记：${escapeHtml(flags)}</p>
            <p>${escapeHtml(skill.description || "无描述")}</p>
          </article>
        `;
      })
      .join("");
  }

  const currentAgent = els.skillBindingAgent.value;
  els.skillBindingAgent.innerHTML = state.agents
    .map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agentOptionLabel(agent))}</option>`)
    .join("");
  if (currentAgent && state.agents.some((agent) => agent.id === currentAgent)) {
    els.skillBindingAgent.value = currentAgent;
  }
  syncSkillBindingFormFromSelectedAgent(false);
  renderSkillBindings();
  renderAutoSkillRoutes();
}

async function loadSkillIntoPreview(skillId) {
  const skill = await api(`/skills/${encodeURIComponent(skillId)}`);
  state.selectedSkillId = skill.skill_id;
  els.skillPreviewBadge.className = "status-pill ok";
  els.skillPreviewBadge.textContent = "只读";
  els.skillPreview.className = "skill-preview";
  els.skillPreview.innerHTML = `
    <article class="observer-block">
      <h4>${escapeHtml(skillLabel(skill))}</h4>
      <p>ID：${escapeHtml(skill.skill_id)}</p>
      <p>路径：${escapeHtml(skill.path)}</p>
      <p>脚本存在但禁用：${skill.has_scripts ? "是" : "否"} ｜ 引用目录自动加载：否</p>
      <p>风险标记：${escapeHtml((skill.risk_flags || []).join(", ") || "无")}</p>
      <pre>${escapeHtml(skill.content || "")}</pre>
    </article>
  `;
  renderSkills();
}

function syncSkillBindingFormFromSelectedAgent(overwrite = true) {
  const agentId = els.skillBindingAgent.value;
  if (!agentId) {
    return;
  }
  if (!overwrite && els.skillBindingIds.value) {
    return;
  }
  const binding = skillBindingForAgent(agentId);
  const ids = binding?.skill_ids || [];
  els.skillBindingIds.value = ids.join(", ");
}

function skillIdsFromForm() {
  return els.skillBindingIds.value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

async function saveSkillBinding() {
  if (!els.skillBindingAgent.value) {
    throw new Error("请先选择智能体。");
  }
  await api(`/skill-bindings/${encodeURIComponent(els.skillBindingAgent.value)}`, {
    method: "PUT",
    body: JSON.stringify({ skill_ids: skillIdsFromForm() }),
  });
  showToast("能力包绑定已保存。保存后需要重启服务生效。");
  await refreshData();
}

async function deleteSelectedSkillBinding() {
  if (!els.skillBindingAgent.value) {
    throw new Error("请先选择智能体。");
  }
  await api(`/skill-bindings/${encodeURIComponent(els.skillBindingAgent.value)}`, { method: "DELETE" });
  els.skillBindingIds.value = "";
  showToast("能力包绑定已清除。保存后需要重启服务生效。");
  await refreshData();
}

async function refreshSkills() {
  await api("/skills/refresh", { method: "POST" });
  showToast("能力包已刷新。重启服务后绑定会生效。");
  await refreshData();
}

function renderSkillBindings() {
  if (!state.skillBindings.length) {
    els.skillBindingList.className = "provider-overview empty";
    els.skillBindingList.textContent = "暂无能力包绑定。";
    return;
  }
  els.skillBindingList.className = "provider-overview";
  els.skillBindingList.innerHTML = `
    <article class="observer-block">
      <h4>当前能力包绑定</h4>
      <ul class="observer-list">
        ${state.skillBindings
          .map((binding) => `
            <li>
              <strong>${escapeHtml(agentLabel(binding.agent_id))}</strong>
              能力包：${escapeHtml((binding.skill_ids || []).join(", ") || "-")}<br />
              保存后需要重启服务生效
            </li>
          `)
          .join("")}
      </ul>
    </article>
  `;
}

function renderAutoSkillRoutes() {
  if (!els.autoSkillRoutes) {
    return;
  }
  if (!state.skillAutoRoutes.length) {
    els.autoSkillRoutes.className = "provider-overview empty";
    els.autoSkillRoutes.textContent = "暂无自动识别能力包。";
    return;
  }
  const byAgent = new Map();
  for (const route of state.skillAutoRoutes) {
    if (!byAgent.has(route.agent_id)) {
      byAgent.set(route.agent_id, []);
    }
    byAgent.get(route.agent_id).push(route);
  }
  els.autoSkillRoutes.className = "provider-overview";
  els.autoSkillRoutes.innerHTML = `
    <article class="observer-block">
      <h4>自动识别结果</h4>
      <ul class="observer-list">
        ${Array.from(byAgent.entries())
          .map(([agentId, routes]) => `
            <li>
              <strong>${escapeHtml(agentLabel(agentId))}</strong><br />
              ${routes
                .map((route) => `${escapeHtml(route.skill_id)}：${escapeHtml(route.reason || "按场景匹配")}`)
                .join("<br />")}
            </li>
          `)
          .join("")}
      </ul>
    </article>
  `;
}

async function saveBindingForAgent(agent, preset) {
  const existing = bindingForAgent(agent.id);
  await api(`/agent-bindings/${encodeURIComponent(agent.id)}`, {
    method: "PUT",
    body: JSON.stringify({
      provider: preset.provider,
      model: preset.model,
      temperature: preset.temperature,
      max_tokens: preset.max_tokens || undefined,
      reasoning_effort: preset.reasoning_effort || undefined,
      role_card_id: existing?.role_card_id || null,
      allow_real_calls: preset.allow_real_calls,
    }),
  });
}

async function saveInstitutionalRecommendedRoutes() {
  const agents = state.agents.filter((agent) => agent.pack_name === "code_rd_institutional");
  if (!agents.length) {
    throw new Error("没有找到制度化代码研发工作流的智能体。");
  }
  const summary = agents
    .map((agent) => {
      const route = preferredRouteForAgent(agent);
      return `${agentRoleLabel(agent.role)}：${providerLabel(route.provider)} / ${route.model}`;
    })
    .join("\n");
  const confirmed = window.confirm(
    `确认保存制度化代码研发的推荐模型分工？\n\n${summary}\n\n这只会保存本地模型路由配置，不会立即发起模型调用。`
  );
  if (!confirmed) {
    return;
  }
  for (const agent of agents) {
    await saveBindingForAgent(agent, preferredRouteForAgent(agent));
  }
  showToast("推荐分工已保存。重启服务后生效。");
  await refreshData();
}

function renderTaskCard(task) {
  const latestRun = latestRunForTask(task.id);
  const active = task.id === state.selectedTaskId ? " active" : "";
  const isSelected = task.id === state.selectedTaskId ? "true" : "false";
  return `
    <article
      class="item-card${active}"
      data-task-id="${escapeHtml(task.id)}"
      role="option"
      aria-selected="${isSelected}"
      aria-label="选择任务：${escapeHtml(task.title)}"
      tabindex="0"
    >
      <h4>${escapeHtml(task.title)}</h4>
      <p>工作流：${escapeHtml(workflowPackLabel(task.workflow_pack))}</p>
      <p>${escapeHtml(task.goal)}</p>
      <p>最近运行：${
        latestRun
          ? `<span class="status-pill ${escapeHtml(latestRun.status)}">${statusLabel(latestRun.status)}</span>`
          : '<span class="status-pill muted">暂无运行</span>'
      }</p>
    </article>
  `;
}

function renderRecordGroup(groupKey, label, items, renderItem, options = {}) {
  if (!items.length) {
    return "";
  }
  const open = options.open || items.some((item) => item.id === options.selectedId);
  return `
    <details class="record-group ${groupKey}" ${open ? "open" : ""}>
      <summary>${label}<span>${items.length}</span></summary>
      <div class="record-group-body">
        ${items.map(renderItem).join("")}
      </div>
    </details>
  `;
}

function renderGlobalRunBar() {
  const task = currentTask();
  const run = currentRun();
  const displayTask = task || taskById(run?.task_id);
  els.globalRunTaskButton.disabled = state.isBusy || !displayTask;
  els.globalTraceButton.disabled = state.isBusy || !run;
  els.followActiveRunButton.textContent = state.followLatestActiveRun ? "取消跟随" : "跟随活跃运行";
  els.followActiveRunButton.setAttribute("aria-pressed", state.followLatestActiveRun ? "true" : "false");

  if (!displayTask && !run) {
    els.globalRunTitle.textContent = "未选择任务";
    els.globalRunSummary.textContent = "创建或选择一个任务后，这里会固定显示当前 task/run、状态、步骤和刷新时间。";
    els.globalRunMeta.innerHTML = `
      <div><dt>状态</dt><dd>${renderStatusPill("muted", "未选择")}</dd></div>
      <div><dt>当前步骤</dt><dd>-</dd></div>
      <div><dt>刷新</dt><dd>${escapeHtml(formatRefreshFreshness())}</dd></div>
    `;
    return;
  }

  els.globalRunTitle.textContent = displayTask?.title || run.id;
  els.globalRunSummary.textContent = run
    ? `Run ${run.id} ｜ ${workflowPackLabel(displayTask?.workflow_pack || "-")} ｜ 下一步：${nextActionForRun(run)}`
    : `${workflowPackLabel(displayTask?.workflow_pack || "-")} ｜ 尚未运行，下一步：运行所选任务`;
  els.globalRunMeta.innerHTML = `
    <div><dt>状态</dt><dd>${run ? renderStatusPill(run.status) : renderStatusPill("muted", "待运行")}</dd></div>
    <div><dt>当前步骤</dt><dd>${escapeHtml(run?.current_step || "-")}</dd></div>
    <div><dt>刷新</dt><dd>${escapeHtml(formatRefreshFreshness())}</dd></div>
  `;
}

function renderNeedsAttention() {
  const items = deriveNeedsAttention();
  if (!items.length) {
    els.needsAttentionPanel.className = "attention-strip empty";
    els.needsAttentionPanel.textContent = "暂无需要人工处理的运行。";
    return;
  }
  els.needsAttentionPanel.className = "attention-strip";
  els.needsAttentionPanel.innerHTML = `
    <strong>需要处理</strong>
    ${items
      .map(({ run, task }) => `
        <button type="button" data-console-run-id="${escapeHtml(run.id)}">
          ${escapeHtml(task?.title || run.task_id)} ｜ ${escapeHtml(statusLabel(run.status))}
        </button>
      `)
      .join("")}
  `;
}

function renderRunConsoleCard(run) {
  const task = taskById(run.task_id);
  const active = run.id === state.selectedRunId ? " active" : "";
  return `
    <article class="run-console-card${active}" data-console-run-id="${escapeHtml(run.id)}" tabindex="0">
      <header>
        <div>
          <h4>${escapeHtml(task?.title || run.task_id)}</h4>
          <p>${escapeHtml(workflowPackLabel(task?.workflow_pack || "-"))} ｜ Run ${escapeHtml(run.id)}</p>
        </div>
        ${renderStatusPill(run.status)}
      </header>
      <dl class="run-console-meta">
        <div><dt>当前步骤</dt><dd>${escapeHtml(run.current_step || "-")}</dd></div>
        <div><dt>下一步</dt><dd>${escapeHtml(nextActionForRun(run))}</dd></div>
        <div><dt>更新时间</dt><dd>${formatDate(run.finished_at || run.started_at || run.created_at)}</dd></div>
      </dl>
    </article>
  `;
}

function renderRunConsoleGroup(label, runs, groupClass) {
  if (!runs.length) {
    return "";
  }
  return `
    <section class="run-console-group ${groupClass}" aria-label="${escapeHtml(label)}">
      <h4>${escapeHtml(label)} <span>${runs.length}</span></h4>
      <div>${runs.map(renderRunConsoleCard).join("")}</div>
    </section>
  `;
}

function renderRunConsole() {
  els.runConsoleFreshness.className = state.lastRefreshError ? "status-pill danger" : "status-pill muted";
  els.runConsoleFreshness.textContent = formatRefreshFreshness();
  renderNeedsAttention();

  if (!state.runs.length) {
    els.runConsoleList.className = "run-console-list empty";
    els.runConsoleList.textContent = "暂无运行记录；创建并运行任务后会显示在这里。";
    return;
  }

  const groups = deriveRunConsoleItems();
  els.runConsoleList.className = "run-console-list";
  els.runConsoleList.innerHTML = [
    renderRunConsoleGroup("需要处理", groups.attention, "attention"),
    renderRunConsoleGroup("正在运行", groups.active, "active"),
    renderRunConsoleGroup("失败 / 阻塞", groups.failed, "failed"),
    renderRunConsoleGroup("最近完成", groups.done, "done"),
  ].join("") || renderEmptyState("暂无匹配的运行记录。");
}

function renderTasks() {
  const visibleTasks = filteredTasks();
  const visibleRuns = filteredRuns();
  const hasFilter = state.recordSearch.trim() || state.recordStatusFilter !== "all";
  els.summaryCount.textContent = `${state.tasks.length} 个任务`;
  els.recordFilterSummary.textContent = hasFilter
    ? `命中 ${visibleTasks.length} 个任务 / ${visibleRuns.length} 条运行`
    : "未筛选";
  els.runSelectedButton.disabled = state.isBusy || !state.selectedTaskId;

  if (!visibleTasks.length) {
    els.taskList.className = "item-list empty";
    els.taskList.removeAttribute("role");
    els.taskList.removeAttribute("aria-label");
    els.taskList.textContent = state.tasks.length ? "没有符合筛选条件的任务" : "暂无任务";
    return;
  }

  els.taskList.className = "item-list";
  els.taskList.setAttribute("role", "listbox");
  els.taskList.setAttribute("aria-label", "任务列表");
  const sortedTasks = [...visibleTasks].sort((left, right) => {
    if (left.id === state.selectedTaskId) {
      return -1;
    }
    if (right.id === state.selectedTaskId) {
      return 1;
    }
    return taskSortValue(right) - taskSortValue(left);
  });
  const groupedTasks = groupTasks(sortedTasks);
  els.taskList.innerHTML = [
    renderRecordGroup("selected", "当前所选", groupedTasks.selected, renderTaskCard, {
      open: true,
      selectedId: state.selectedTaskId,
    }),
    renderRecordGroup("active", "正在进行 / 待处理任务", groupedTasks.active, renderTaskCard, {
      open: true,
      selectedId: state.selectedTaskId,
    }),
    renderRecordGroup("recent", "最近任务", groupedTasks.recent, renderTaskCard, {
      open: groupedTasks.selected.length === 0 && groupedTasks.active.length === 0,
      selectedId: state.selectedTaskId,
    }),
    renderRecordGroup("archive", "更早任务", groupedTasks.archive, renderTaskCard, {
      selectedId: state.selectedTaskId,
    }),
  ].join("");
}

function renderRunCard(run) {
  const task = state.tasks.find((item) => item.id === run.task_id);
  const related = run.task_id === state.selectedTaskId;
  const active = run.id === state.selectedRunId ? " active" : "";
  const isSelected = run.id === state.selectedRunId ? "true" : "false";
  const title = task?.title || run.task_id;
  return `
    <article
      class="item-card${active}${related ? " related" : ""}"
      data-run-id="${escapeHtml(run.id)}"
      role="option"
      aria-selected="${isSelected}"
      aria-label="选择运行记录：${escapeHtml(title)}，状态 ${escapeHtml(statusLabel(run.status))}"
      tabindex="0"
    >
      <h4>${escapeHtml(title)}</h4>
      <p><span class="status-pill ${escapeHtml(run.status)}">${statusLabel(run.status)}</span></p>
      <p>当前步骤：${escapeHtml(run.current_step || "-")}</p>
      ${related ? '<p><span class="status-pill muted">当前任务相关</span></p>' : ""}
      <p>开始：${formatDate(run.started_at)} ｜ 结束：${formatDate(run.finished_at)}</p>
    </article>
  `;
}

function renderRuns() {
  els.runCount.textContent = state.runs.length;
  const visibleRuns = filteredRuns();

  if (!visibleRuns.length) {
    els.runList.className = "item-list empty";
    els.runList.removeAttribute("role");
    els.runList.removeAttribute("aria-label");
    els.runList.textContent = state.runs.length ? "没有符合筛选条件的运行记录" : "暂无运行记录";
    return;
  }

  els.runList.className = "item-list";
  els.runList.setAttribute("role", "listbox");
  els.runList.setAttribute("aria-label", "运行记录列表");
  const sortedRuns = [...visibleRuns]
    .sort((left, right) => {
      const leftRelated = left.task_id === state.selectedTaskId ? 1 : 0;
      const rightRelated = right.task_id === state.selectedTaskId ? 1 : 0;
      if (leftRelated !== rightRelated) {
        return rightRelated - leftRelated;
      }
      return runTimestamp(right) - runTimestamp(left);
    });
  const groupedRuns = groupRuns(sortedRuns);
  const groups = [
    ["active", "正在进行 / 需要处理"],
    ["failed", "失败 / 取消"],
    ["done", "已完成"],
    ["other", "其他状态"],
  ];
  els.runList.innerHTML = groups
    .filter(([key]) => groupedRuns[key].length)
    .map(([key, label]) =>
      renderRecordGroup(key, label, groupedRuns[key], renderRunCard, {
        open: key === "active",
        selectedId: state.selectedRunId,
      })
    )
    .join("");
}

function renderWorkflowCurrent() {
  const task = currentTask();
  const run = task ? latestRunForTask(task.id) : currentRun() || preferredRun();
  els.workflowRunCurrentButton.disabled = state.isBusy || !task;
  els.workflowTraceCurrentButton.disabled = state.isBusy || !run;

  if (!task && !run) {
    els.workflowCurrentBadge.className = "status-pill muted";
    els.workflowCurrentBadge.textContent = "未选择";
    els.workflowCurrentSummary.className = "workflow-current-summary empty";
    els.workflowCurrentSummary.textContent = "创建或选择一个任务后，这里会显示当前任务、最近运行和下一步动作。";
    return;
  }

  const displayTask = task || state.tasks.find((item) => item.id === run.task_id);
  const status = run?.status || "muted";
  els.workflowCurrentBadge.className = `status-pill ${escapeHtml(status)}`;
  els.workflowCurrentBadge.textContent = run ? statusLabel(run.status) : "可运行";
  els.workflowCurrentSummary.className = "workflow-current-summary";
  els.workflowCurrentSummary.innerHTML = `
    <dl class="current-run-grid">
      <div>
        <dt>当前任务</dt>
        <dd>${escapeHtml(displayTask?.title || "未命名任务")}</dd>
      </div>
      <div>
        <dt>工作流</dt>
        <dd>${escapeHtml(workflowPackLabel(displayTask?.workflow_pack || "-"))}</dd>
      </div>
      <div>
        <dt>最近运行</dt>
        <dd>${run ? escapeHtml(run.id) : "暂无运行"}</dd>
      </div>
      <div>
        <dt>当前步骤</dt>
        <dd>${escapeHtml(run?.current_step || "-")}</dd>
      </div>
    </dl>
    <p>${escapeHtml(run ? `追踪产物会默认跟随这条 ${statusLabel(run.status)} 的运行。` : "这个任务还没有运行，可以直接在这里启动。")}</p>
  `;
}

function renderSelectedRunMeta(run) {
  if (!run) {
    els.selectedRunBadge.className = "status-pill muted";
    els.selectedRunBadge.textContent = "未选择";
    els.selectedRunMeta.className = "run-meta empty";
    els.selectedRunMeta.textContent = "选择一个运行记录查看执行过程。";
    els.runtimeStatus.className = "runtime-status empty";
    els.runtimeStatus.textContent = "选择一个运行记录查看本地会话、任务、审批、队列和锁状态。";
    els.failureSummary.className = "failure-summary hidden";
    els.failureSummary.innerHTML = "";
    return;
  }

  const task = state.tasks.find((item) => item.id === run.task_id);
  els.selectedRunBadge.className = `status-pill ${run.status}`;
  els.selectedRunBadge.textContent = statusLabel(run.status);
  els.selectedRunMeta.className = "run-meta";
  els.selectedRunMeta.innerHTML = `
    <p><strong>${escapeHtml(task?.title || "未命名任务")}</strong></p>
    <p>运行编号：${escapeHtml(run.id)}</p>
    <p>工作流：${escapeHtml(workflowPackLabel(task?.workflow_pack || "-"))}</p>
    <p>当前步骤：${escapeHtml(run.current_step || "-")}</p>
    <p>最终产物：${escapeHtml(run.final_artifact_id || "-")}</p>
  `;
}

function statusSummary(items) {
  if (!items.length) {
    return "-";
  }
  const counts = {};
  items.forEach((item) => {
    counts[item.status] = (counts[item.status] || 0) + 1;
  });
  return Object.entries(counts)
    .map(([status, count]) => `${status}:${count}`)
    .join(", ");
}

function workflowTraceEvents(trace, action) {
  return trace.filter((event) => event.event_type === "workflow_event" && event.payload?.action === action);
}

function formatReadyBatchSummary(trace) {
  const readyEvent = workflowTraceEvents(trace, "ready_batches_planned")[0];
  if (!readyEvent) {
    return "无 ready batch 计划记录";
  }
  const batches = readyEvent.payload?.batches || [];
  const branchBatches = batches.filter((batch) => (batch.steps || []).length > 1);
  if (!branchBatches.length) {
    return "串行或单分支执行";
  }
  return branchBatches
    .map((batch) => `${(batch.steps || []).join(" + ")} ｜ 并行候选：${batch.parallel_candidate ? "是" : "否"}`)
    .join("；");
}

function formatParallelExecutionSummary(trace) {
  const parallelEvents = workflowTraceEvents(trace, "parallel_step_batch_executed");
  const abortedEvents = workflowTraceEvents(trace, "parallel_step_batch_aborted");
  if (!parallelEvents.length && !abortedEvents.length) {
    return "未发生真实线程并发；可能仍存在并行审批等待或 DAG 分支。";
  }
  return [
    ...parallelEvents.map((event) => `${(event.payload?.steps || []).join(" + ")} ｜ ownership 已检查`),
    ...abortedEvents.map(
      (event) =>
        `parallel_step_batch_aborted: ${event.payload?.failed_step || "-"} ｜ 已取消：${(event.payload?.cancelled_uncommitted_steps || []).join(", ") || "-"}`
    ),
  ].join("；");
}

function formatTaskSkillRouteSummary(trace) {
  const routeEvents = workflowTraceEvents(trace, "task_skill_routes_applied");
  if (!routeEvents.length) {
    return "无 task-time skill 注入";
  }
  return routeEvents
    .map((event) => {
      const skillIds = event.payload?.skill_ids || [];
      const injectedBytes = event.payload?.injected_bytes || 0;
      return `${event.payload?.step_name || "-"}: ${skillIds.join(", ") || "-"} ｜ ${injectedBytes} bytes`;
    })
    .join("；");
}

function renderRuntimeStatus(runtimeSessions, runtimeJobs, queueState = [], lockState = [], trace = []) {
  const approvalJobs = runtimeJobs.filter((job) => job.approval_required);
  const pendingApprovalJobs = approvalJobs.filter((job) => job.status === "approval_required");
  const hasRuntimeState = runtimeSessions.length || runtimeJobs.length || queueState.length || lockState.length;
  els.runtimeStatus.className = hasRuntimeState ? "runtime-status" : "runtime-status empty";
  els.runtimeStatus.innerHTML = `
    <h3>本地运行状态</h3>
    <p>会话：${escapeHtml(statusSummary(runtimeSessions))}</p>
    <p>任务：${escapeHtml(statusSummary(runtimeJobs))}</p>
    <p>审批：${escapeHtml(pendingApprovalJobs.length ? `${pendingApprovalJobs.length} 个任务等待本地审批` : "无待审批任务")}</p>
    <p>本地队列：${escapeHtml(statusSummary(queueState))}</p>
    <p>运行锁：${escapeHtml(statusSummary(lockState))}</p>
    <p>Ready batch：${escapeHtml(formatReadyBatchSummary(trace))}</p>
    <p>真实并发：${escapeHtml(formatParallelExecutionSummary(trace))}</p>
    <p>Task-time skill：${escapeHtml(formatTaskSkillRouteSummary(trace))}</p>
      <p>队列 / 锁：本地持久化状态由后台 worker 消费；不代表命令行、子进程或远程执行器。</p>
    <p>外部执行器：未启动。批准、拒绝、取消只记录本地审批意图。</p>
  `;
}

function renderFailureSummary(run, trace, artifacts, evalResults) {
  const errorEvent = lastEventOfType(trace, "error");
  const failedEvals = evalResults.filter((result) => result.status === "fail");
  if (run.status !== "failed" && !errorEvent && !failedEvals.length) {
    els.failureSummary.className = "failure-summary hidden";
    els.failureSummary.innerHTML = "";
    return;
  }

  const agent = state.agents.find((item) => item.id === errorEvent?.payload?.agent_id);
  els.failureSummary.className = "failure-summary";
  els.failureSummary.innerHTML = `
    <h3>失败摘要</h3>
    <p>运行状态：${escapeHtml(statusLabel(run.status))}</p>
    <p>失败步骤：${escapeHtml(run.current_step || errorEvent?.payload?.step_name || "-")}</p>
    <p>智能体：${escapeHtml(agent ? `${agentRoleLabel(agent.role)} (${agent.id})` : errorEvent?.payload?.agent_id || "-")}</p>
    <p>类型：${escapeHtml(errorEvent?.payload?.error_type || (failedEvals.length ? "EvalFail" : "-"))}</p>
    <p>信息：${escapeHtml(errorEvent?.payload?.message || failedEvals.map((result) => result.check_name).join(", ") || "-")}</p>
    <p>失败评估：${escapeHtml(failedEvals.map((result) => `${result.check_name}:${result.message || result.status}`).join(", ") || "-")}</p>
    <p>已产出产物：${escapeHtml(artifacts.map((artifact) => `${artifact.type}:${artifact.id}`).join(", ") || "-")}</p>
    <p>最终产物：${escapeHtml(run.final_artifact_id || "未产出最终交付")}</p>
  `;
}

function agentLabel(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent) {
    return agentId || "-";
  }
  return `${agentRoleLabel(agent.role)} (${agent.id})`;
}

function artifactsForAgentRun(artifacts, agentRunId) {
  return artifacts.filter((artifact) => artifact.agent_run_id === agentRunId);
}

function evalsForAgentRun(evalResults, artifacts, agentRunId) {
  const artifactIds = new Set(artifactsForAgentRun(artifacts, agentRunId).map((artifact) => artifact.id));
  return evalResults.filter((result) => result.artifact_id && artifactIds.has(result.artifact_id));
}

function handoffFromAgentRun(handoffs, agentRunId) {
  return handoffs.find((handoff) => handoff.from_agent_run_id === agentRunId) || null;
}

function lastEventOfType(events, eventType) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].event_type === eventType) {
      return events[index];
    }
  }
  return null;
}

function modelRequestForAgentRun(trace, agentRunId) {
  return trace.find(
    (event) =>
      event.agent_run_id === agentRunId &&
      event.event_type === "model_action" &&
      event.payload?.action === "model_request"
  );
}

function modelResponseForAgentRun(trace, agentRunId) {
  return trace.find(
    (event) =>
      event.agent_run_id === agentRunId &&
      event.event_type === "model_action" &&
      event.payload?.action === "model_response"
  );
}

function sessionForAgentRun(runtimeSessions, agentRunId) {
  return runtimeSessions.find((session) => session.agent_run_id === agentRunId) || null;
}

function jobForAgentRun(runtimeJobs, agentRunId) {
  return runtimeJobs.find((job) => job.agent_run_id === agentRunId) || null;
}

function formatRuntimeJob(job) {
  if (!job) {
    return "任务：无 ｜ 审批：无 ｜ 外部执行器：未启动";
  }
  return [
    `任务：${statusLabel(job.status)}`,
    `审批：${job.approval_required ? "需要" : "不需要"}`,
    `外部执行器：${job.metadata?.external_runtime_started ? "已启动" : "未启动"}`,
  ].join(" ｜ ");
}

function renderRuntimeJobActions(job) {
  if (!job || job.runtime !== "acp") {
    return "";
  }
  const canApprove = job.status === "approval_required" && job.metadata?.external_runtime_started !== true;
  if (!canApprove) {
    return `<div class="runtime-actions muted">本地审批动作：${escapeHtml(statusLabel(job.status))} ｜ 外部执行器：未启动</div>`;
  }
  const isPending = state.pendingRuntimeActions.has(job.id);
  const disabled = isPending ? "disabled" : "";
  const pendingLabel = isPending ? "处理中..." : "本地审批动作：只记录本地审批意图，不启动外部执行器。";
  return `
    <div class="runtime-actions" data-runtime-job-actions="${escapeHtml(job.id)}">
      <span>${escapeHtml(pendingLabel)}</span>
      <button type="button" aria-label="批准意图" data-runtime-action="approve" data-run-id="${escapeHtml(job.run_id)}" data-job-id="${escapeHtml(job.id)}" ${disabled}>批准意图</button>
      <button type="button" aria-label="拒绝意图" data-runtime-action="reject" data-run-id="${escapeHtml(job.run_id)}" data-job-id="${escapeHtml(job.id)}" ${disabled}>拒绝意图</button>
      <button type="button" aria-label="取消任务" data-runtime-action="cancel" data-run-id="${escapeHtml(job.run_id)}" data-job-id="${escapeHtml(job.id)}" ${disabled}>取消任务</button>
    </div>
  `;
}

function renderWritebackControls(artifact) {
  if (artifact.type !== "patch") {
    return "";
  }
  const preview = state.writebackPreviews[artifact.id];
  const task = state.selectedRunDetail?.task;
  const repositoryPath = task?.inputs?.repository_path || "";
  const pendingAction = state.pendingWritebackActions.get(artifact.id);
  const previewPending = pendingAction === "preview";
  const approvePending = pendingAction === "approve";
  const previewHtml = preview
    ? `
      <div class="writeback-preview">
        <p>写回预览：${escapeHtml(preview.dry_run_status)} ｜ 文件数：${escapeHtml(preview.files_changed.length)}</p>
        <p>目标仓库：${escapeHtml(preview.repository_path)}</p>
        <p>补丁校验值：${escapeHtml(preview.patch_hash)}</p>
        <ul>
          ${preview.files_changed
            .map((file) => `<li>${escapeHtml(file.action)} ｜ ${escapeHtml(file.path)} ｜ ${escapeHtml(file.base_hash.slice(0, 12))} -> ${escapeHtml(file.new_hash.slice(0, 12))}</li>`)
            .join("")}
        </ul>
      </div>
    `
    : "";
  return `
    <div class="writeback-actions">
      <span>写回原仓库：必须先预览，再显式确认。普通本地批准不会写回。</span>
      <button type="button" data-writeback-action="preview" data-artifact-id="${escapeHtml(artifact.id)}" ${previewPending || approvePending ? "disabled" : ""}>${previewPending ? "预览中..." : "预览写回"}</button>
      <button
        type="button"
        class="danger-button"
        data-writeback-action="approve"
        data-artifact-id="${escapeHtml(artifact.id)}"
        ${preview && repositoryPath && !previewPending && !approvePending ? "" : "disabled"}
      >${approvePending ? "写回中..." : "确认写回"}</button>
    </div>
    ${previewHtml}
  `;
}

function renderExecutionChain(agentRuns, handoffs, artifacts, evalResults, trace, runtimeSessions = [], runtimeJobs = [], run = null) {
  if (!agentRuns.length) {
    els.chainPanel.innerHTML = run && isActiveRunStatus(run.status)
      ? '<div class="empty">运行已提交，正在等待执行链路和追踪事件。</div>'
      : '<div class="empty">暂无智能体执行记录</div>';
    return;
  }

  els.chainPanel.innerHTML = agentRuns
    .map((agentRun, index) => {
      const stepArtifacts = artifactsForAgentRun(artifacts, agentRun.id);
      const stepEvals = evalsForAgentRun(evalResults, artifacts, agentRun.id);
      const handoff = handoffFromAgentRun(handoffs, agentRun.id);
      const modelRequest = modelRequestForAgentRun(trace, agentRun.id);
      const modelResponse = modelResponseForAgentRun(trace, agentRun.id);
      const configuredModel = modelConfigForAgent(agentRun.agent_id);
      const coordinationRole = agentRun.input_context?.coordination_role;
      const controllerStep = agentRun.input_context?.controller_step;
      const runtime = agentRun.input_context?.runtime || "model";
      const sessionPolicy = agentRun.input_context?.session_policy;
      const runtimeSession = sessionForAgentRun(runtimeSessions, agentRun.id);
      const runtimeJob = jobForAgentRun(runtimeJobs, agentRun.id);
      const callMode = modelResponse ? (modelResponse.payload.mocked ? "mock" : "real") : "-";
      const actualRoute = modelRequest ? `${modelRequest.payload.provider}/${modelRequest.payload.model}` : "-";
      return `
        <article class="chain-step ${escapeHtml(agentRun.status)}">
          <header>
            <strong>${index + 1}. ${escapeHtml(agentRun.step_name)}</strong>
            <span class="status-pill ${escapeHtml(agentRun.status)}">${statusLabel(agentRun.status)}</span>
          </header>
          <div class="chain-metadata">
            <p><strong>智能体</strong>${escapeHtml(agentLabel(agentRun.agent_id))}</p>
            <p><strong>协作身份</strong>${escapeHtml(coordinationLabel(coordinationRole))} ｜ 控制步骤：${escapeHtml(controllerStep || "-")}</p>
            <p><strong>运行方式</strong>${escapeHtml(runtime)} ｜ 会话策略：${escapeHtml(formatSessionPolicy(sessionPolicy))}</p>
            <p><strong>本地会话</strong>${escapeHtml(runtimeSession ? `${runtimeSession.status} / ${runtimeSession.resume_strategy}` : "-")} ｜ ${escapeHtml(formatRuntimeJob(runtimeJob))}</p>
            <p><strong>Gate</strong>${escapeHtml(formatGateContext(agentRun.input_context || {}))}</p>
            <p><strong>Ownership</strong>${escapeHtml(formatOwnership(agentRun.input_context?.ownership))}</p>
            <p><strong>模型配置</strong>${escapeHtml(configuredModel)}</p>
            <p><strong>本次调用</strong>${escapeHtml(actualRoute)} ｜ ${escapeHtml(callMode)}</p>
            <p><strong>延迟 / 用量</strong>${escapeHtml(modelResponse?.payload?.latency_ms ?? "-")} ms ｜ ${escapeHtml(JSON.stringify(modelResponse?.payload?.usage || {}))}</p>
            <p><strong>时间</strong>开始：${formatDate(agentRun.started_at)} ｜ 结束：${formatDate(agentRun.finished_at)}</p>
          </div>
          ${renderRuntimeJobActions(runtimeJob)}
          <p>输出摘要：${escapeHtml(agentRun.output_summary || "-")}</p>
          <p>产物：${escapeHtml(stepArtifacts.map((artifact) => `${artifact.type}:${artifact.id}`).join(", ") || "-")}</p>
          <p>评估：${escapeHtml(stepEvals.map((result) => `${result.check_name}:${result.status}`).join(", ") || "-")}</p>
          <p>移交 / 子智能体派发：${escapeHtml(handoff ? `${handoff.next_objective} -> ${agentLabel(handoff.to_agent_id)}` : "-")}</p>
        </article>
      `;
    })
    .join("");
}

function renderEvalResults(evalResults) {
  if (!evalResults.length) {
    els.evalPanel.innerHTML = '<div class="empty">暂无评估结果</div>';
    return;
  }

  els.evalPanel.innerHTML = evalResults
    .map((result) => `
      <article class="eval-card ${escapeHtml(result.status)}">
        <header>
          <strong>${escapeHtml(result.check_name)}</strong>
          <span class="status-pill ${escapeHtml(result.status)}">${escapeHtml(result.status)}</span>
        </header>
        <p>信息：${escapeHtml(result.message || "-")}</p>
        <p>产物：${escapeHtml(result.artifact_id || "-")}</p>
        <p>创建时间：${formatDate(result.created_at)}</p>
      </article>
    `)
    .join("");
}

async function renderRunDetails() {
  const run = state.runs.find((item) => item.id === state.selectedRunId);
  const requestToken = ++state.detailRequestToken;
  renderSelectedRunMeta(run);

  if (!run) {
    state.selectedRunDetail = null;
    els.chainPanel.innerHTML = state.runs.length
      ? renderEmptyState("没有可用的运行详情。请选择一条运行记录。")
      : renderEmptyState("暂无运行；从工作流创建并运行后会自动显示这里。");
    els.tracePanel.innerHTML = renderEmptyState("暂无追踪事件");
    els.artifactPanel.innerHTML = renderEmptyState("暂无产物");
    els.evalPanel.innerHTML = renderEmptyState("暂无评估结果");
    return;
  }

  els.chainPanel.innerHTML = renderEmptyState(`正在加载运行详情：${run.id}`);
  els.tracePanel.innerHTML = renderEmptyState(`正在加载追踪事件：${run.id}`);
  els.artifactPanel.innerHTML = renderEmptyState(`正在加载产物：${run.id}`);
  els.evalPanel.innerHTML = renderEmptyState(`正在加载评估结果：${run.id}`);

  const detail = await api(`/runs/${run.id}/detail`);
  if (requestToken !== state.detailRequestToken || state.selectedRunId !== run.id || detail.run?.id !== run.id) {
    return;
  }
  state.selectedRunDetail = detail;
  const trace = detail.trace;
  const agentRuns = detail.agent_runs;
  const handoffs = detail.handoffs;
  const artifacts = detail.artifacts;
  const evalResults = detail.eval_results;
  const runtimeSessions = detail.runtime_sessions || [];
  const runtimeJobs = detail.runtime_jobs || [];
  const queueState = detail.queue_state || [];
  const lockState = detail.lock_state || [];
  renderRuntimeStatus(runtimeSessions, runtimeJobs, queueState, lockState, trace);
  renderSelectedRunMeta(detail.run);
  renderFailureSummary(detail.run, trace, artifacts, evalResults);
  renderExecutionChain(agentRuns, handoffs, artifacts, evalResults, trace, runtimeSessions, runtimeJobs, detail.run);
  renderEvalResults(evalResults);

  els.tracePanel.innerHTML = trace.length
    ? trace
        .map((event) => `
          <details class="trace-event">
            <summary>
              <strong>${escapeHtml(event.event_type)}</strong>
              <span>${formatDate(event.created_at)}</span>
            </summary>
            <pre>${escapeHtml(JSON.stringify(event.payload, null, 2))}</pre>
          </details>
        `)
        .join("")
    : renderEmptyState("暂无追踪事件");

  els.artifactPanel.innerHTML = artifacts.length
    ? artifacts
        .map((artifact) => `
          <article class="artifact-card">
            <header>
              <strong>${escapeHtml(artifact.type)}</strong>
              <button type="button" data-artifact-id="${escapeHtml(artifact.id)}">查看内容</button>
            </header>
            <p>产物 ID：${escapeHtml(artifact.id)}</p>
            <p>路径：${escapeHtml(artifact.path)}</p>
            ${renderWritebackControls(artifact)}
            <div id="artifact-content-${escapeHtml(artifact.id)}"></div>
          </article>
        `)
        .join("")
    : renderEmptyState("暂无产物");
}

function renderAll() {
  renderCatalog();
  renderRoleCards();
  renderGlobalRunBar();
  renderRunConsole();
  renderTasks();
  renderRuns();
  renderWorkflowCurrent();
}

function renderRunSelectionViews() {
  renderGlobalRunBar();
  renderRunConsole();
  renderTasks();
  renderRuns();
  renderWorkflowCurrent();
}

function updateAutoRefresh() {
  const runRelatedView = ["dashboardView", "workflowView", "recordsView", "traceView"].includes(state.activeView);
  const shouldRefresh = runRelatedView && state.runs.some((run) => isActiveRunStatus(run.status));
  if (shouldRefresh && !state.autoRefreshTimer) {
    state.autoRefreshTimer = window.setInterval(() => {
      if (!state.isBusy) {
        refreshData({ silent: true, runtimeOnly: true }).catch(() => {});
      }
    }, 5000);
  }
  if (!shouldRefresh && state.autoRefreshTimer) {
    window.clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
}

function syncSelectionAfterRefresh() {
  if (state.selectedTaskId && !state.tasks.some((task) => task.id === state.selectedTaskId)) {
    state.selectedTaskId = null;
  }
  if (state.selectedRunId && !state.runs.some((run) => run.id === state.selectedRunId)) {
    state.selectedRunId = null;
  }

  const selectedTask = currentTask();
  const activePreferred = preferredRun();
  if (state.followLatestActiveRun && activePreferred) {
    state.selectedRunId = activePreferred.id;
    state.selectedTaskId = activePreferred.task_id;
  } else if (!state.selectedRunId && selectedTask) {
    state.selectedRunId = latestRunForTask(selectedTask.id)?.id || null;
  } else if (!state.selectedRunId && !selectedTask) {
    state.selectedRunId = activePreferred?.id || null;
  }

  const updatedSelectedRun = currentRun();
  if (updatedSelectedRun && !state.selectedTaskId) {
    state.selectedTaskId = updatedSelectedRun.task_id;
  }
  if (!state.selectedTaskId && state.tasks.length) {
    const sortedTasks = [...state.tasks].sort((left, right) => taskSortValue(right) - taskSortValue(left));
    state.selectedTaskId = sortedTasks[0].id;
  }
}

const refreshData = window.HarnessRuntime.createRefreshCoordinator(refreshDataOnce);

async function refreshDataOnce(options = {}) {
  const originalRefreshText = els.refreshButton.textContent;
  if (options.feedback) {
    els.refreshButton.textContent = "刷新中";
    els.refreshButton.classList.add("refreshing");
    els.refreshButton.setAttribute("aria-busy", "true");
  }
  try {
    const requestOptions = { timeoutMs: 10000 };
    let health;
    let tasks;
    let runs;
    if (options.runtimeOnly) {
      [health, tasks, runs] = await Promise.all([
        api("/health", requestOptions),
        api("/tasks?limit=500", requestOptions),
        api("/runs?limit=500", requestOptions),
      ]);
    } else {
      const [
        loadedHealth,
        packs,
        modelProviders,
        toolProviders,
        agents,
        roleCards,
        agentBindings,
        skills,
        skillBindings,
        skillAutoRoutes,
        loadedTasks,
        loadedRuns,
      ] = await Promise.all([
        api("/health", requestOptions),
        api("/workflow-packs", requestOptions),
        api("/model-providers", requestOptions),
        api("/tool-providers", requestOptions),
        api("/agents", requestOptions),
        api("/role-cards", requestOptions),
        api("/agent-bindings", requestOptions),
        api("/skills", requestOptions),
        api("/skill-bindings", requestOptions),
        api("/skill-auto-routes", requestOptions),
        api("/tasks?limit=500", requestOptions),
        api("/runs?limit=500", requestOptions),
      ]);
      health = loadedHealth;
      tasks = loadedTasks;
      runs = loadedRuns;
      state.packs = packs;
      state.modelProviders = modelProviders;
      state.toolProviders = toolProviders;
      state.agents = agents;
      state.roleCards = roleCards;
      state.agentBindings = agentBindings;
      state.skills = skills;
      state.skillBindings = skillBindings;
      state.skillAutoRoutes = skillAutoRoutes;
    }

    state.tasks = tasks;
    state.runs = runs;
    state.lastRefreshAt = Date.now();
    state.lastRefreshError = null;

    els.healthBadge.className = "status-pill ok";
    els.healthBadge.textContent = health.status === "ok" ? "已连接" : "未知状态";

    syncSelectionAfterRefresh();

    if (!options.runtimeOnly) {
      renderCatalog();
      await loadSelectedPackDetail();
    }
    renderGlobalRunBar();
    renderRunConsole();
    renderTasks();
    renderRuns();
    renderWorkflowCurrent();
    if (!options.runtimeOnly) {
      renderProviderOverview();
      renderPackOverview();
      renderRoleCards();
      renderSkills();
    }
    await renderRunDetails();
    updateAutoRefresh();

    if (options.feedback) {
      showToast(`数据已刷新：${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      els.refreshButton.textContent = "已刷新";
    }
  } catch (error) {
    state.lastRefreshError = error.message;
    els.healthBadge.className = "status-pill danger";
    els.healthBadge.textContent = "连接异常";
    renderGlobalRunBar();
    renderRunConsole();
    if (options.silent) {
      return;
    }
    throw error;
  } finally {
    if (options.feedback && els.refreshButton.getAttribute("aria-busy") === "true") {
      window.setTimeout(() => {
        els.refreshButton.textContent = originalRefreshText;
        els.refreshButton.classList.remove("refreshing");
        els.refreshButton.removeAttribute("aria-busy");
      }, 700);
    }
  }
}

function buildTaskPayload() {
  return {
    title: els.taskTitle.value.trim(),
    goal: els.taskGoal.value.trim(),
    workflow_pack: els.workflowPack.value,
    inputs: parseInputs(),
    constraints: linesToArray(els.taskConstraints.value),
    acceptance_criteria: linesToArray(els.taskCriteria.value),
    created_by: "thin-ui",
  };
}

async function resolveWorkflowPackForPayload(payload) {
  if (payload.workflow_pack !== "auto") {
    return payload.workflow_pack;
  }
  const intake = await api("/task-intake/analyze", {
    method: "POST",
    body: JSON.stringify({
      title: payload.title,
      goal: payload.goal,
      inputs: payload.inputs,
      constraints: payload.constraints,
      acceptance_criteria: payload.acceptance_criteria,
    }),
  });
  return intake.recommended_pack;
}

function providerByName(name) {
  return state.modelProviders.find((provider) => provider.name === name) || null;
}

function realEnabledRoutesForPack(pack) {
  if (!pack) {
    return [];
  }
  return pack.agents
    .map((agent) => {
      const modelConfig = agent.model_config || {};
      const providerName = modelConfig.provider || "mock";
      const provider = providerByName(providerName);
      if (providerName === "mock" || !provider?.real_calls || !provider?.enabled) {
        return null;
      }
      return {
        agentId: agent.id,
        provider: providerName,
        model: modelConfig.model || "-",
      };
    })
    .filter(Boolean);
}

function confirmRealProviderRunForPack(packName) {
  const pack = state.packs.find((item) => item.name === packName);
  const realRoutes = realEnabledRoutesForPack(pack);
  if (realRoutes.length === 0) {
    return true;
  }
  const routeList = realRoutes
    .map((route) => `- ${route.agentId}: ${route.provider}/${route.model}`)
    .join("\n");
  return window.confirm(
    `确认运行真实模型调用？\n\n工作流：${workflowPackLabel(packName)}\n${routeList}\n\n本次运行会调用外部模型渠道，可能产生费用并发送任务上下文。`
  );
}

function confirmRealProviderRun(task) {
  return confirmRealProviderRunForPack(task.workflow_pack);
}

function realWebSearchEnabled() {
  return state.toolProviders.some((provider) => provider.real_calls && provider.enabled);
}

function packUsesWebSearch(packName) {
  const pack = state.packs.find((item) => item.name === packName);
  return Boolean(pack?.steps?.some((step) =>
    (step.allowed_tools || []).some((tool) => ["web_search", "fetch_page", "browser_search", "browser_fetch"].includes(tool))
  ));
}

function confirmRealWebSearchRunForPack(packName) {
  if (!realWebSearchEnabled() || !packUsesWebSearch(packName)) {
    return true;
  }
  const providers = state.toolProviders
    .filter((provider) => provider.real_calls && provider.enabled)
    .map((provider) => `- ${provider.name}: ${provider.provider}`)
    .join("\n");
  return window.confirm(
    `确认运行真实联网工具？\n\n工作流：${workflowPackLabel(packName)}\n${providers}\n\n本次运行会访问外部搜索/网页服务或本机浏览器桥接，可能产生费用并发送搜索查询。`
  );
}

function confirmRealWebSearchRun(task) {
  return confirmRealWebSearchRunForPack(task.workflow_pack);
}

function applyExample(packName) {
  const example = examples[packName];
  if (!example) {
    return;
  }
  els.workflowPack.value = packName;
  els.taskTitle.value = example.title;
  els.taskGoal.value = example.goal;
  els.taskInputs.value = JSON.stringify(example.inputs, null, 2);
  els.taskConstraints.value = example.constraints.join("\n");
  els.taskCriteria.value = example.acceptance_criteria.join("\n");
  runAction(async () => {
    await loadSelectedPackDetail();
    renderPackOverview();
    setBusy(state.isBusy);
  });
  showToast(`${packName} 示例已填充。`);
}

async function createTask(payload = null, options = {}) {
  const task = await api("/tasks", {
    method: "POST",
    body: JSON.stringify(payload || buildTaskPayload()),
  });
  selectTask(task.id, { runId: null, followLatestActiveRun: false });
  showToast("任务已创建，可直接运行。");
  await refreshData();
  if (!options.stayOnCurrentView) {
    setActiveView("workflowView");
  }
  return task;
}

async function runTask(taskId, options = {}) {
  const task = state.tasks.find((item) => item.id === taskId) || (await api(`/tasks/${encodeURIComponent(taskId)}`));
  let confirmedRealModels = Boolean(options.confirmRealModels);
  let confirmedRealWeb = Boolean(options.confirmRealWeb);
  if (!options.skipConfirm) {
    confirmedRealModels = confirmRealProviderRun(task);
  }
  if (!confirmedRealModels) {
    showToast("已取消真实模型调用。");
    return;
  }
  if (!options.skipWebConfirm) {
    confirmedRealWeb = confirmRealWebSearchRun(task);
  }
  if (!confirmedRealWeb) {
    showToast("已取消真实联网搜索。");
    return;
  }
  const run = await api("/runs", {
    method: "POST",
    body: JSON.stringify({
      task_id: taskId,
      confirm_real_models: confirmedRealModels,
      confirm_real_web: confirmedRealWeb,
      background: true,
    }),
  });
  selectRun(run.id, { followLatestActiveRun: false });
  const toast = runToastMessage(run);
  showToast(toast.message, toast.tone);
  setActiveView("traceView");
  await refreshData();
}

function confirmRuntimeIntent(action) {
  const labels = {
    approve: "批准",
    reject: "拒绝",
    cancel: "取消",
  };
  return window.confirm(
    `确认${labels[action] || action}这个本地任务？\n\n这只会记录本地审批意图，不会启动外部工程执行器，不会执行命令行，也不会把代码发送给外部工程执行器。`
  );
}

function writebackApprovalTimeoutMs() {
  const configured = Number(state.selectedRunDetail?.task?.inputs?.test_timeout_seconds);
  const testTimeoutSeconds = Number.isFinite(configured) && configured > 0
    ? Math.min(Math.trunc(configured), 900)
    : 120;
  return (testTimeoutSeconds + 30) * 1000;
}

async function submitRuntimeAction(runId, jobId, action) {
  if (state.pendingRuntimeActions.has(jobId)) {
    return;
  }
  if (!confirmRuntimeIntent(action)) {
    showToast("已取消本地审批动作。");
    return;
  }
  state.pendingRuntimeActions.add(jobId);
  try {
    await renderRunDetails();
    const backgroundQuery = action === "approve" ? "?background=true" : "";
    const payload = await api(`/runs/${encodeURIComponent(runId)}/runtime-jobs/${encodeURIComponent(jobId)}/${action}${backgroundQuery}`, {
      method: "POST",
    });
    if (payload.run?.id) {
      selectRun(payload.run.id);
    }
    showToast(
      action === "approve"
        ? "本地审批意图已记录，后续步骤已进入后台队列。"
        : "本地审批意图已记录；外部工程执行器仍未启动。"
    );
    await refreshData();
  } finally {
    state.pendingRuntimeActions.delete(jobId);
    await renderRunDetails().catch(() => {});
  }
}

async function submitWritebackAction(artifactId, action) {
  if (!state.selectedRunId) {
    throw new Error("请先选择一个运行记录。");
  }
  if (state.pendingWritebackActions.has(artifactId)) {
    return;
  }
  state.pendingWritebackActions.set(artifactId, action);
  try {
    if (action === "preview") {
      const preview = await api(`/runs/${encodeURIComponent(state.selectedRunId)}/writeback/preview`, {
        method: "POST",
        body: JSON.stringify({ patch_artifact_id: artifactId }),
      });
      state.writebackPreviews[artifactId] = preview;
      showToast("写回预览已生成。");
      await renderRunDetails();
      return;
    }

    if (action !== "approve") {
      throw new Error("未知写回动作。");
    }
    const preview = state.writebackPreviews[artifactId];
    if (!preview) {
      throw new Error("请先预览写回。");
    }
    const repositoryPath = state.selectedRunDetail?.task?.inputs?.repository_path;
    if (!repositoryPath) {
      throw new Error("当前任务没有 repository_path，不能写回。");
    }
    const fileList = preview.files_changed.map((file) => `- ${file.path}`).join("\n");
    const confirmed = window.confirm(
      `确认写回原仓库？\n\n目标：${repositoryPath}\n文件：\n${fileList}\n\n系统会先在隔离副本应用 patch 并运行 test_command；通过后才写回。此操作会修改你的原项目文件。`
    );
    if (!confirmed) {
      showToast("已取消写回。");
      return;
    }
    const result = await api(`/runs/${encodeURIComponent(state.selectedRunId)}/writeback/approve`, {
      method: "POST",
      timeoutMs: writebackApprovalTimeoutMs(),
      body: JSON.stringify({
        patch_artifact_id: artifactId,
        writeback_id: preview.writeback_id,
        confirm_repository_path: repositoryPath,
        confirm_patch_hash: preview.patch_hash,
        expected_base_hashes: preview.base_hashes,
      }),
    });
    delete state.writebackPreviews[artifactId];
    showToast(`已写回 ${result.applied_files.length} 个文件。`);
    await refreshData();
  } finally {
    state.pendingWritebackActions.delete(artifactId);
    await renderRunDetails().catch(() => {});
  }
}

function roleCardPayloadFromForm() {
  return {
    name: els.roleCardName.value.trim(),
    description: els.roleCardDescription.value.trim(),
    color: els.roleCardColor.value.trim(),
    emoji: els.roleCardEmoji.value.trim(),
    vibe: els.roleCardVibe.value.trim(),
    content: els.roleCardContent.value.trim(),
  };
}

async function saveRoleCard() {
  const roleCardId = els.roleCardId.value.trim();
  if (!/^[A-Za-z0-9_-]+$/.test(roleCardId)) {
    throw new Error("角色卡 ID 只能包含字母、数字、下划线和短横线。");
  }
  const card = await api(`/role-cards/${encodeURIComponent(roleCardId)}`, {
    method: "PUT",
    body: JSON.stringify(roleCardPayloadFromForm()),
  });
  state.selectedRoleCardId = card.id;
  showToast("角色卡已保存。保存后需要重启服务生效。");
  await refreshData();
  await loadRoleCardIntoForm(card.id);
}

async function deleteSelectedRoleCard() {
  const roleCardId = state.selectedRoleCardId || els.roleCardId.value.trim();
  if (!roleCardId) {
    throw new Error("请先选择一个角色卡。");
  }
  if (!window.confirm(`确认删除角色卡 ${roleCardId}？引用它的本地绑定会被清除，重启后生效。`)) {
    return;
  }
  await api(`/role-cards/${encodeURIComponent(roleCardId)}`, { method: "DELETE" });
  resetRoleCardForm();
  showToast("角色卡已删除，相关本地绑定已清除。");
  await refreshData();
}

function bindingPayloadFromForm() {
  const payload = {
    provider: els.bindingProvider.value,
    model: els.bindingModel.value.trim(),
    role_card_id: els.bindingRoleCard.value || null,
    allow_real_calls: els.bindingAllowRealCalls.checked,
  };
  const temperature = els.bindingTemperature.value.trim();
  const maxTokens = els.bindingMaxTokens.value.trim();
  const reasoningEffort = els.bindingReasoningEffort.value.trim();
  if (temperature) {
    payload.temperature = Number(temperature);
  }
  if (maxTokens) {
    payload.max_tokens = Number(maxTokens);
  }
  if (reasoningEffort) {
    payload.reasoning_effort = reasoningEffort;
  }
  return payload;
}

async function saveAgentBinding() {
  if (!els.bindingAgent.value) {
    throw new Error("请先选择智能体。");
  }
  await api(`/agent-bindings/${encodeURIComponent(els.bindingAgent.value)}`, {
    method: "PUT",
    body: JSON.stringify(bindingPayloadFromForm()),
  });
  showToast("智能体绑定已保存。保存后需要重启服务生效。");
  await refreshData();
}

async function deleteSelectedAgentBinding() {
  if (!els.bindingAgent.value) {
    throw new Error("请先选择智能体。");
  }
  await api(`/agent-bindings/${encodeURIComponent(els.bindingAgent.value)}`, { method: "DELETE" });
  showToast("智能体绑定已清除。保存后需要重启服务生效。");
  await refreshData();
  syncBindingFormFromSelectedAgent(true);
}

async function runAction(callback) {
  if (state.isBusy) {
    return;
  }
  setBusy(true);
  try {
    await callback();
  } catch (error) {
    showToast(error.message, "danger");
  } finally {
    setBusy(false);
    renderRunSelectionViews();
  }
}

els.refreshButton.addEventListener("click", () => runAction(() => refreshData({ feedback: true })));

els.globalRunTaskButton.addEventListener("click", () => {
  if (!state.selectedTaskId) {
    return;
  }
  runAction(() => runTask(state.selectedTaskId));
});

els.globalTraceButton.addEventListener("click", () => {
  if (!state.selectedRunId) {
    return;
  }
  setActiveView("traceView");
  runAction(renderRunDetails);
});

els.followActiveRunButton.addEventListener("click", () => {
  state.followLatestActiveRun = !state.followLatestActiveRun;
  if (state.followLatestActiveRun) {
    const activeRun = preferredRun();
    if (activeRun) {
      selectRun(activeRun.id);
    }
  }
  renderRunSelectionViews();
  showToast(state.followLatestActiveRun ? "已跟随最新活跃运行。" : "已取消自动跟随。");
});

document.querySelectorAll("[data-view-target]").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    setActiveView(button.dataset.viewTarget);
  });
});

document.querySelectorAll("[data-view-shortcut]").forEach((button) => {
  button.addEventListener("click", () => setActiveView(button.dataset.viewShortcut));
});

window.addEventListener("hashchange", () => setActiveView(viewFromHash()));

els.workflowPack.addEventListener("change", () => {
  runAction(async () => {
    await loadSelectedPackDetail();
    renderPackOverview();
    setBusy(state.isBusy);
  });
});

els.newRoleCardButton.addEventListener("click", resetRoleCardForm);

els.roleCardList.addEventListener("click", (event) => {
  const card = event.target.closest("[data-role-card-id]");
  if (!card) {
    return;
  }
  runAction(() => loadRoleCardIntoForm(card.dataset.roleCardId));
});

els.roleCardList.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const card = event.target.closest("[data-role-card-id]");
  if (card) {
    runAction(() => loadRoleCardIntoForm(card.dataset.roleCardId));
  }
});

els.roleCardForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(saveRoleCard);
});

els.deleteRoleCardButton.addEventListener("click", () => runAction(deleteSelectedRoleCard));

els.bindingAgent.addEventListener("change", () => syncBindingFormFromSelectedAgent(true));

els.bindingProvider.addEventListener("change", () => {
  if (!bindingForAgent(els.bindingAgent.value)) {
    const model = defaultModelForProvider(els.bindingProvider.value);
    els.bindingModel.value = model;
    els.bindingReasoningEffort.value = defaultReasoningEffortForModel(els.bindingProvider.value, model);
  }
});

els.agentBindingForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(saveAgentBinding);
});

els.deleteBindingButton.addEventListener("click", () => runAction(deleteSelectedAgentBinding));

els.skillList.addEventListener("click", (event) => {
  const card = event.target.closest("[data-skill-id]");
  if (!card) {
    return;
  }
  runAction(() => loadSkillIntoPreview(card.dataset.skillId));
});

els.skillList.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const card = event.target.closest("[data-skill-id]");
  if (card) {
    runAction(() => loadSkillIntoPreview(card.dataset.skillId));
  }
});

els.refreshSkillsButton.addEventListener("click", () => runAction(refreshSkills));

els.skillBindingAgent.addEventListener("change", () => syncSkillBindingFormFromSelectedAgent(true));

els.skillBindingForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(saveSkillBinding);
});

els.deleteSkillBindingButton.addEventListener("click", () => runAction(deleteSelectedSkillBinding));

els.applyGptPresetButton.addEventListener("click", () => {
  applyRoutePresetToForm(routingPresets.gptMainThread);
  showToast("已填入 GPT 主线程推荐配置。");
});

els.applyDeepSeekPresetButton.addEventListener("click", () => {
  applyRoutePresetToForm(routingPresets.deepSeekLongContext);
  showToast("已填入 DeepSeek 长上下文推荐配置。");
});

els.saveInstitutionalPresetButton.addEventListener("click", () => runAction(saveInstitutionalRecommendedRoutes));

els.codeExampleButton.addEventListener("click", () => {
  applyExample(els.workflowPack.value === "code_rd_institutional" ? "code_rd_institutional" : "code_rd");
});

els.researchExampleButton.addEventListener("click", () => applyExample("research"));

els.taskForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(createTask);
});

els.runTaskButton.addEventListener("click", () => {
  runAction(async () => {
    const payload = buildTaskPayload();
    const resolvedPack = await resolveWorkflowPackForPayload(payload);
    if (!confirmRealProviderRunForPack(resolvedPack)) {
      showToast("已取消真实模型调用。");
      return;
    }
    if (!confirmRealWebSearchRunForPack(resolvedPack)) {
      showToast("已取消真实联网搜索。");
      return;
    }
    const task = await createTask({ ...payload, workflow_pack: resolvedPack }, { stayOnCurrentView: true });
    await runTask(task.id, { skipConfirm: true, skipWebConfirm: true, confirmRealModels: true, confirmRealWeb: true });
  });
});

els.runSelectedButton.addEventListener("click", () => {
  if (!state.selectedTaskId) {
    return;
  }
  runAction(() => runTask(state.selectedTaskId));
});

els.recordSearch.addEventListener("input", () => {
  state.recordSearch = els.recordSearch.value;
  renderRunSelectionViews();
});

els.recordStatusFilter.addEventListener("change", () => {
  state.recordStatusFilter = els.recordStatusFilter.value;
  renderRunSelectionViews();
});

els.clearRecordFiltersButton.addEventListener("click", () => {
  state.recordSearch = "";
  state.recordStatusFilter = "all";
  els.recordSearch.value = "";
  els.recordStatusFilter.value = "all";
  renderRunSelectionViews();
});

els.workflowRunCurrentButton.addEventListener("click", () => {
  if (!state.selectedTaskId) {
    return;
  }
  runAction(() => runTask(state.selectedTaskId));
});

els.workflowTraceCurrentButton.addEventListener("click", () => {
  if (!state.selectedRunId) {
    return;
  }
  setActiveView("traceView");
  runAction(renderRunDetails);
});

els.taskList.addEventListener("click", (event) => {
  const card = event.target.closest("[data-task-id]");
  if (!card) {
    return;
  }
  selectTask(card.dataset.taskId, { followLatestActiveRun: false });
  renderRunSelectionViews();
});

els.taskList.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const card = event.target.closest("[data-task-id]");
  if (card) {
    selectTask(card.dataset.taskId, { followLatestActiveRun: false });
    renderRunSelectionViews();
  }
});

els.runList.addEventListener("click", (event) => {
  const card = event.target.closest("[data-run-id]");
  if (!card) {
    return;
  }
  selectRun(card.dataset.runId, { followLatestActiveRun: false });
  renderRunSelectionViews();
  setActiveView("traceView");
  runAction(renderRunDetails);
});

els.runList.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const card = event.target.closest("[data-run-id]");
  if (card) {
    selectRun(card.dataset.runId, { followLatestActiveRun: false });
    renderRunSelectionViews();
    setActiveView("traceView");
    runAction(renderRunDetails);
  }
});

function openConsoleRun(runId) {
  selectRun(runId, { followLatestActiveRun: false });
  renderRunSelectionViews();
  setActiveView("traceView");
  runAction(renderRunDetails);
}

els.needsAttentionPanel.addEventListener("click", (event) => {
  const button = event.target.closest("[data-console-run-id]");
  if (!button) {
    return;
  }
  openConsoleRun(button.dataset.consoleRunId);
});

els.runConsoleList.addEventListener("click", (event) => {
  const card = event.target.closest("[data-console-run-id]");
  if (!card) {
    return;
  }
  openConsoleRun(card.dataset.consoleRunId);
});

els.runConsoleList.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") {
    return;
  }
  const card = event.target.closest("[data-console-run-id]");
  if (card) {
    openConsoleRun(card.dataset.consoleRunId);
  }
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.activeTab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((item) => {
      const isActive = item === tab;
      item.classList.toggle("active", isActive);
      item.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    els.chainPanel.classList.toggle("hidden", state.activeTab !== "chain");
    els.tracePanel.classList.toggle("hidden", state.activeTab !== "trace");
    els.artifactPanel.classList.toggle("hidden", state.activeTab !== "artifacts");
    els.evalPanel.classList.toggle("hidden", state.activeTab !== "evals");
  });
});

els.artifactPanel.addEventListener("click", async (event) => {
  const writebackButton = event.target.closest("[data-writeback-action]");
  if (writebackButton) {
    await runAction(() => submitWritebackAction(writebackButton.dataset.artifactId, writebackButton.dataset.writebackAction));
    return;
  }
  const button = event.target.closest("[data-artifact-id]");
  if (!button) {
    return;
  }
  await runAction(async () => {
    const payload = await api(`/artifacts/${button.dataset.artifactId}`);
    const container = document.querySelector(`#artifact-content-${escapeSelector(button.dataset.artifactId)}`);
    container.innerHTML = `<pre>${escapeHtml(payload.content)}</pre>`;
  });
});

els.chainPanel.addEventListener("click", (event) => {
  const button = event.target.closest("[data-runtime-action]");
  if (!button || button.disabled) {
    return;
  }
  runAction(() => submitRuntimeAction(button.dataset.runId, button.dataset.jobId, button.dataset.runtimeAction));
});

setActiveView(viewFromHash());

refreshData().catch((error) => {
  els.healthBadge.className = "status-pill danger";
  els.healthBadge.textContent = "连接失败";
  showToast(error.message, "danger");
});
