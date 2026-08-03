import re

from fastapi.testclient import TestClient

from app.main import create_app


def test_static_ui_index_and_assets_are_served(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        index_response = client.get("/")
        css_response = client.get("/static/styles.css")
        runtime_response = client.get("/static/runtime.js")
        js_response = client.get("/static/app.js")

    assert index_response.status_code == 200
    assert "团队 Multi-Agent Harness" in index_response.text
    assert 'class="sidebar"' in index_response.text
    assert 'class="side-nav"' in index_response.text
    assert 'href="#dashboardView" data-view-target="dashboardView"' in index_response.text
    assert 'href="#workflowView" data-view-target="workflowView"' in index_response.text
    assert 'href="#routingView" data-view-target="routingView"' in index_response.text
    assert 'href="#roleCardsView" data-view-target="roleCardsView"' in index_response.text
    assert 'href="#skillsView" data-view-target="skillsView"' in index_response.text
    assert 'href="#recordsView" data-view-target="recordsView"' in index_response.text
    assert 'href="#traceView" data-view-target="traceView"' in index_response.text
    assert 'class="view-page active"' in index_response.text
    assert 'id="workflowView"' in index_response.text
    assert 'id="routingView"' in index_response.text
    assert 'id="roleCardsView"' in index_response.text
    assert 'id="skillsView"' in index_response.text
    assert 'id="recordsView"' in index_response.text
    assert 'id="traceView"' in index_response.text
    assert 'class="metric-grid"' in index_response.text
    assert 'class="metric-card accent-green"' in index_response.text
    assert "多模型协作控制台" in index_response.text
    assert "模型路由" in index_response.text
    assert "角色卡" in index_response.text
    assert "能力包" in index_response.text
    assert "Skill Library" in index_response.text
    assert "角色卡列表" in index_response.text
    assert "绑定到智能体" in index_response.text
    assert "选择要配置的岗位" in index_response.text
    assert "推荐 GPT 与 DeepSeek 均经 LiteLLM，分别使用 gpt5.5 与 deepseek-v4-pro" in index_response.text
    assert "思考强度" in index_response.text
    assert "推荐模型方案" in index_response.text
    assert "GPT 主线程" in index_response.text
    assert "DeepSeek 长上下文" in index_response.text
    assert "一键保存推荐分工" in index_response.text
    assert "保存后需要重启服务生效" in index_response.text
    assert "脚本存在但禁用" in index_response.text
    assert "追踪产物" in index_response.text
    assert "快捷操作" in index_response.text
    assert "平台拆分" in index_response.text
    assert "Harness 负责主控线程和子智能体的长期编排" in index_response.text
    assert "运行方式和会话策略决定长期子会话或工程执行器边界" in index_response.text
    assert "本地后台 worker 执行持久化运行队列，外部工程执行器仍未启动" in index_response.text
    assert 'lang="zh-CN"' in index_response.text
    assert "代码研发示例" in index_response.text
    assert "知识研究示例" in index_response.text
    assert "工作流观察" in index_response.text
    assert "任务录入" in index_response.text
    assert "运行队列" in index_response.text
    assert "执行控制台" in index_response.text
    assert "当前工作台" in index_response.text
    assert "workflowCurrentPanel" in index_response.text
    assert "workflowCurrentBadge" in index_response.text
    assert "workflowRunCurrentButton" in index_response.text
    assert "workflowTraceCurrentButton" in index_response.text
    assert "20260702-run-console" in index_response.text
    assert "globalRunBar" in index_response.text
    assert "globalRunTaskButton" in index_response.text
    assert "globalTraceButton" in index_response.text
    assert "followActiveRunButton" in index_response.text
    assert "runConsoleList" in index_response.text
    assert "needsAttentionPanel" in index_response.text
    assert "recordSearch" in index_response.text
    assert "recordStatusFilter" in index_response.text
    assert "recordFilterSummary" in index_response.text
    assert "任务归纳" in index_response.text
    assert "运行归纳" in index_response.text
    assert "完成或失败的历史运行会折叠归档" in index_response.text
    assert "运行规则" in index_response.text
    assert "普通本地批准只记录审批意图" in index_response.text
    assert "模型配置" in index_response.text
    assert "模型渠道目录" in index_response.text
    assert "默认使用本地模拟路由" in index_response.text
    assert "执行链路" in index_response.text
    assert "执行追踪与产物" in index_response.text
    assert "chainPanel" in index_response.text
    assert "evalPanel" in index_response.text
    assert "failureSummary" in index_response.text
    assert 'role="tablist"' in index_response.text
    assert 'role="tab"' in index_response.text
    assert 'aria-selected="true"' in index_response.text
    assert 'aria-controls="chainPanel"' in index_response.text
    assert 'role="tabpanel"' in index_response.text
    assert css_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]
    assert "--sidebar-width" in css_response.text
    assert ".metric-card" in css_response.text
    assert ".side-nav" in css_response.text
    assert ".role-card-icon" in css_response.text
    assert ".role-editor" in css_response.text
    assert ".field-help" in css_response.text
    assert ".view-page.active" in css_response.text
    assert ".view-page[hidden]" in css_response.text
    assert ".view-page:target" not in css_response.text
    assert ":has(.view-page:target)" not in css_response.text
    assert "@keyframes view-in" in css_response.text
    assert ".workflow-current" in css_response.text
    assert ".global-run-bar" in css_response.text
    assert ".run-console-panel" in css_response.text
    assert ".run-console-card" in css_response.text
    assert ".record-toolbar" in css_response.text
    assert ".record-group" in css_response.text
    assert ".record-group.selected" in css_response.text
    assert ".record-group.archive" in css_response.text
    assert ".item-card.related" in css_response.text
    assert ".current-run-grid" in css_response.text
    assert "#refreshButton.refreshing::before" in css_response.text
    assert "@keyframes button-spin" in css_response.text
    assert "@media (max-width: 820px)" in css_response.text
    assert runtime_response.status_code == 200
    assert "createRefreshCoordinator" in runtime_response.text
    assert "const text = await response.text()" in runtime_response.text
    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]
    assert "renderPackOverview" in js_response.text
    assert "selectedPackDetail" in js_response.text
    assert "formatModelConfig" in js_response.text
    assert "renderProviderOverview" in js_response.text
    assert "renderRoleCards" in js_response.text
    assert "renderSkills" in js_response.text
    assert "/skills" in js_response.text
    assert "/skill-bindings" in js_response.text
    assert "/skill-auto-routes" in js_response.text
    assert "自动识别能力包" in index_response.text
    assert "自动注入" in index_response.text
    assert "renderAutoSkillRoutes" in js_response.text
    assert "/tool-providers" in js_response.text
    assert "confirmRealWebSearchRun" in js_response.text
    assert "saveRoleCard" in js_response.text
    assert "saveAgentBinding" in js_response.text
    assert "/role-cards" in js_response.text
    assert "/agent-bindings" in js_response.text
    assert "activeView" in js_response.text
    assert "autoRefreshTimer" in js_response.text
    assert "background: true" in js_response.text
    assert "viewMeta" in js_response.text
    assert "setActiveView" in js_response.text
    assert "viewFromHash" in js_response.text
    assert "hashchange" in js_response.text
    assert "data-view-target" in js_response.text
    assert "data-view-shortcut" in js_response.text
    assert "workflowPackLabel" in js_response.text
    assert "workflowPackDescription" in js_response.text
    assert "代码研发协作" in js_response.text
    assert "制度化代码研发协作" in js_response.text
    assert "知识研究协作" in js_response.text
    assert "GPT 主线程 + DeepSeek 长上下文把控" in js_response.text
    assert "agentRoleDisplay" in js_response.text
    assert "agentRoleLabel" in js_response.text
    assert "agentOptionLabel" in js_response.text
    assert "providerLabel" in js_response.text
    assert "LiteLLM 统一网关" in js_response.text
    assert "本地模拟" in js_response.text
    assert "代码审查" in js_response.text
    assert "上下文阅读" in js_response.text
    assert "最终审批" in js_response.text
    assert "工作流：" in js_response.text
    assert 'setAttribute("role", "listbox")' in js_response.text
    assert 'role="option"' in js_response.text
    assert "aria-selected" in js_response.text
    assert "item.setAttribute(\"aria-selected\"" in js_response.text
    assert 'removeAttribute("aria-label")' in js_response.text
    assert "coordinationLabel" in js_response.text
    assert "formatReturnContract" in js_response.text
    assert "formatSessionPolicy" in js_response.text
    assert "协作模型" in js_response.text
    assert "执行步骤" in js_response.text
    assert "智能体" in js_response.text
    assert "评估检查" in js_response.text
    assert "模型渠道" in js_response.text
    assert "主控线程" in js_response.text
    assert "子智能体" in js_response.text
    assert "移交 / 子智能体派发" in js_response.text
    assert "返回要求" in js_response.text
    assert "执行方式" in js_response.text
    assert "会话规则" in js_response.text
    assert "session_policy" in js_response.text
    assert "Gate" in js_response.text
    assert "Ownership" in js_response.text
    assert "Ready batch" in js_response.text
    assert "真实并发" in js_response.text
    assert "Task-time skill" in js_response.text
    assert "parallel_step_batch_executed" in js_response.text
    assert "parallel_step_batch_aborted" in js_response.text
    assert "runtimeStatus" in js_response.text
    assert "renderRuntimeStatus" in js_response.text
    assert "runtime_sessions" in js_response.text
    assert "runtime_jobs" in js_response.text
    assert "queue_state" in js_response.text
    assert "lock_state" in js_response.text
    assert "本地队列" in js_response.text
    assert "运行锁" in js_response.text
    assert "本地持久化状态由后台 worker 消费" in js_response.text
    assert "本地持久化状态由后台 worker 消费；不代表命令行、子进程或远程执行器" in js_response.text
    assert "approval_required" in js_response.text
    assert "requires_approval" in js_response.text
    assert "external_runtime_started" in js_response.text
    assert "不启动外部工程执行器进程" in js_response.text
    assert "外部执行器：未启动" in js_response.text
    assert "批准意图" in js_response.text
    assert "拒绝意图" in js_response.text
    assert "取消任务" in js_response.text
    assert "只记录本地审批意图" in js_response.text
    assert "不会启动外部 ACP 执行器" in js_response.text
    assert "submitRuntimeAction" in js_response.text
    assert 'action === "approve" ? "?background=true" : ""' in js_response.text
    assert "预览写回" in js_response.text
    assert "确认写回" in js_response.text
    assert "普通本地批准不会写回" in js_response.text
    assert "submitWritebackAction" in js_response.text
    assert "writebackApprovalTimeoutMs" in js_response.text
    assert "timeoutMs: writebackApprovalTimeoutMs()" in js_response.text
    assert "/writeback/preview" in js_response.text
    assert "/writeback/approve" in js_response.text
    assert "expected_base_hashes" in js_response.text
    assert "confirm_patch_hash" in js_response.text
    assert "pytest 将以当前 Windows 用户权限执行模型生成补丁后的代码" in js_response.text
    assert "该进程不是系统安全沙箱" in js_response.text
    assert "此操作会修改你的原项目文件" in js_response.text
    assert "startAcp" not in js_response.text
    assert "Start ACP" not in js_response.text
    assert "Launch executor" not in js_response.text
    assert "startWorker" not in js_response.text
    assert "Launch worker" not in js_response.text
    assert "lease_token" not in js_response.text
    assert "owner_token" not in js_response.text
    assert "external_ref" not in js_response.text
    assert "/approvals" not in js_response.text
    assert "/jobs/start" not in js_response.text
    assert "模型配置" in js_response.text
    assert 'api("/model-providers", requestOptions)' in js_response.text
    assert "真实调用能力" in js_response.text
    assert "联网搜索" in js_response.text
    assert "联网工具" in js_response.text
    assert "Tavily" in js_response.text
    assert "browser_search" in js_response.text
    assert "browser_fetch" in js_response.text
    assert "Google Chrome 桥接" in js_response.text
    assert "仅 Research 的 browser_search/browser_fetch 使用" in js_response.text
    assert "确认运行真实联网工具" in js_response.text
    assert "本机浏览器桥接" in js_response.text
    assert "凭据已配置" in js_response.text
    assert "confirmRealProviderRun" in js_response.text
    assert "confirmRealProviderRunForPack" in js_response.text
    assert "window.confirm" in js_response.text
    assert "确认运行真实模型调用" in js_response.text
    assert "真实模型调用" in js_response.text
    assert "real_calls" in js_response.text
    assert "enabled" in js_response.text
    assert "model_config" in js_response.text
    assert "skipConfirm" in js_response.text
    assert 'api("/workflow-packs", requestOptions)' in js_response.text
    assert "/workflow-packs/${encodeURIComponent(els.workflowPack.value)}" in js_response.text
    assert "async function loadSelectedPackDetail" in js_response.text
    assert 'els.workflowPack.addEventListener("change"' in js_response.text
    assert "renderFailureSummary" in js_response.text
    assert "renderExecutionChain" in js_response.text
    assert "renderEvalResults" in js_response.text
    assert "renderWorkflowCurrent" in js_response.text
    assert "followLatestActiveRun" in js_response.text
    assert "detailRequestToken" in js_response.text
    assert "lastRefreshAt" in js_response.text
    assert "lastRefreshError" in js_response.text
    assert "renderGlobalRunBar" in js_response.text
    assert "renderRunConsole" in js_response.text
    assert "deriveRunConsoleItems" in js_response.text
    assert "deriveNeedsAttention" in js_response.text
    assert "groupTasks" in js_response.text
    assert "groupRuns" in js_response.text
    assert "renderStatusPill" in js_response.text
    assert "renderEmptyState" in js_response.text
    assert "requestToken !== state.detailRequestToken" in js_response.text
    assert "detail.run?.id !== run.id" in js_response.text
    assert "cache: \"no-store\"" in runtime_response.text
    assert "renderTaskCard" in js_response.text
    assert "renderRecordGroup" in js_response.text
    assert "taskActivityGroup" in js_response.text
    assert "preferredRun" in js_response.text
    assert "isActiveRunStatus" in js_response.text
    assert js_response.text.count(
        '["waiting", "approval_required", "waiting_approval", "recorded"].includes(run.status)'
    ) == 2
    assert "updateAutoRefresh" in js_response.text
    assert "record-group" in js_response.text
    assert "当前所选" in js_response.text
    assert "正在进行 / 待处理任务" in js_response.text
    assert "最近任务" in js_response.text
    assert "更早任务" in js_response.text
    assert "正在进行 / 需要处理" in js_response.text
    assert "失败 / 取消" in js_response.text
    assert "已完成" in js_response.text
    assert "当前任务相关" in js_response.text
    assert "刷新中" in js_response.text
    assert "数据已刷新" in js_response.text
    assert "尚未刷新" in index_response.text
    assert "跟随活跃运行" in index_response.text
    assert "取消跟随" in js_response.text
    assert "没有符合筛选条件的任务" in js_response.text
    assert "没有符合筛选条件的运行记录" in js_response.text
    assert "正在加载运行详情：" in js_response.text
    assert "aria-busy" in js_response.text
    assert "运行已提交，当前状态" in js_response.text
    assert "运行已完成。\" : \"运行已完成" not in js_response.text
    assert 'activeTab: "chain"' in js_response.text
    assert '/runs/${run.id}/detail' in js_response.text
    assert '/runs/${run.id}/trace' not in js_response.text
    assert '/runs/${run.id}/agent-runs' not in js_response.text
    assert '/runs/${run.id}/handoffs' not in js_response.text
    assert '/runs/${run.id}/eval-results' not in js_response.text
    assert "errorEvent?.payload?.agent_id" in js_response.text
    assert "applyExample" in js_response.text
    assert js_response.text.count("loadSelectedPackDetail") >= 4
    assert "Access-Control" not in js_response.text
    assert "routingPresets" in js_response.text
    assert "gpt5.5" in js_response.text
    assert "reasoning_effort" in js_response.text
    assert "bindingReasoningEffort" in js_response.text
    assert "saveInstitutionalRecommendedRoutes" in js_response.text
    assert re.search(
        r'deepSeekLongContext:\s*\{\s*provider: "litellm_proxy",\s*model: "deepseek-v4-pro"[\s\S]*?max_tokens: 4096',
        js_response.text,
    )
    assert "applyGptPresetButton" in js_response.text
    assert "applyDeepSeekPresetButton" in js_response.text
    assert "saveInstitutionalPresetButton" in js_response.text


def test_static_ui_guards_long_polling_and_duplicate_mutations(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        js = client.get("/static/app.js").text
        runtime_js = client.get("/static/runtime.js").text

    assert "createRefreshCoordinator(refreshDataOnce)" in js
    assert "queuedFullOptions" in runtime_js
    assert "completeOperation" in runtime_js
    assert "refreshDataOnce" in js
    assert "Promise.all([" in js
    assert "refreshData({ silent: true, runtimeOnly: true })" in js
    assert "if (options.runtimeOnly)" in js
    assert "timeoutMs: 10000" in js
    assert 'healthBadge.textContent = "连接异常"' in js
    assert "pendingWritebackActions" in js
    assert "if (state.isBusy)" in js


def test_static_ui_dom_ids_match_javascript_selectors(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        index_text = client.get("/").text
        js_text = client.get("/static/app.js").text

    html_ids = set(re.findall(r'id="([^"]+)"', index_text))
    queried_ids = set(re.findall(r'document\.querySelector\("#([^"]+)"\)', js_text))

    assert queried_ids
    assert queried_ids <= html_ids


def test_static_ui_navigation_and_tabs_target_real_panels(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        index_text = client.get("/").text

    html_ids = set(re.findall(r'id="([^"]+)"', index_text))
    view_targets = set(re.findall(r'data-view-target="([^"]+)"', index_text))
    shortcut_targets = set(re.findall(r'data-view-shortcut="([^"]+)"', index_text))
    tab_targets = set(re.findall(r'aria-controls="([^"]+)"', index_text))

    assert view_targets <= html_ids
    assert shortcut_targets <= html_ids
    assert tab_targets <= html_ids
