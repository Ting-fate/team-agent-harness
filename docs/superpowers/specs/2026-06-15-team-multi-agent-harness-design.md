# 团队版 Multi-Agent Harness MVP 设计

> Historical baseline from 2026-06-15. The core/pack boundaries remain useful, but the synchronous-run, no-worker, and metadata-only context sections are superseded by `2026-07-10-durable-run-worker-context-design.md`.

## 1. 目标

构建一个面向团队协作的可扩展 multi-agent harness。

平台分为两层：

- 通用 `Harness Core`：统一处理 agent 注册、任务编排、工具权限、上下文管理、trace、审计和评估。
- 首批 workflow pack：`Code R&D Pack` 和 `Research Pack`。

第一阶段不做单一业务流程自动化，也不试图覆盖所有行业场景。业务流程自动化只作为后续扩展方向，通过新增 workflow pack 接入。

## 2. 第一阶段架构

采用 **Service Core + Thin UI**。

后端服务负责 orchestration、状态持久化、权限校验、trace capture 和 evaluation。UI 只作为轻量控制台，用来提交任务、查看进度、检查 trace、下载 artifact。

推荐技术栈：

- Backend：Python + FastAPI。
- Runner：单进程 `asyncio` step runner。
- Storage：MVP 使用 SQLite，schema 保持可迁移到 Postgres。
- UI：轻量 React 或 Next.js console。
- Artifact storage：MVP 使用本地文件，后续可替换为对象存储。

这个选择避免第一阶段过早引入分布式 worker、队列和复杂部署，同时保留团队化使用和后续部署的服务边界。

## 3. Core 边界

`Harness Core` 只拥有通用执行基础设施，不包含具体场景的业务逻辑。

Core 模块：

- `Task API`：创建任务、选择 workflow pack、暴露 run 状态和结果。
- `Workflow Runner`：执行 pack 定义的步骤，管理 run 生命周期。
- `Agent Registry`：保存 agent role、prompt、tool scope、model config 和 runtime constraints。
- `Tool Gateway`：校验工具调用、执行权限策略、记录输入输出。
- `Context Store`：持久化 task context、handoff、artifact 和 state summary。
- `Trace Log`：记录 step 级事件、工具调用、模型动作、错误、耗时和输出。
- `Evaluation Engine`：执行 pack 定义的 readiness、quality、citation、test、regression check。
- `Audit Layer`：回答谁在何时因为什么执行了什么操作。

Workflow pack 拥有场景定义：

- Agent 角色。
- Step 顺序。
- 允许使用的工具。
- 必须产出的 artifact。
- Handoff 规则。
- Evaluation 规则。
- 最终输出格式。

## 4. MVP 范围

### 包含

- 创建 task：包含目标、输入材料、约束、workflow pack、验收标准。
- 通过多个 agent 执行一个 pack-defined workflow。
- 注册 agent：包含 role、system prompt、model config、tool permissions。
- 所有工具调用通过 `Tool Gateway`。
- 持久化 artifact、handoff、run state 和 trace event。
- 在轻量 UI 中查看 run progress 和 trace。
- 在最终输出前执行简单 evaluation check。
- 导出最终 artifact。

### 不包含

- 复杂多租户权限模型。
- 分布式队列和弹性 worker pool。
- 插件市场。
- Billing / quota 系统。
- Business automation workflow pack。
- 生产环境部署平台。
- 自动执行危险操作。
- 大型知识库 RAG 平台。

## 5. Task 生命周期

一次 task 的生命周期：

1. 用户通过 UI 或 API 创建 task。
2. 用户选择 `Code R&D Pack` 或 `Research Pack`。
3. `Task API` 创建 `Run`。
4. `Workflow Runner` 加载 pack definition。
5. Runner 创建第一个 `AgentRun`。
6. Agent 接收 task context 和 allowed tools。
7. 工具调用通过 `Tool Gateway` 执行。
8. Agent 产出一个或多个 artifact。
9. Agent 发出结构化 handoff。
10. Runner 启动下一个 agent step。
11. 在 pack 定义的 checkpoint 执行 evaluation check。
12. Finalizer 产出最终 artifact 和 run summary。
13. UI 展示结果、trace、artifact 和 evaluation status。

## 6. 协作协议

Agent 不使用自由群聊作为主要协作方式。

Agent 通过结构化记录协作：

- `Task`：整体目标、输入、约束和验收标准。
- `AgentRun`：某个 agent 的一次执行步骤。
- `Handoff`：从一个 agent 到另一个 agent 的结构化交接。
- `Artifact`：agent 产出的持久化结果。
- `TraceEvent`：可观测执行事件。
- `EvalResult`：挂在 run 或 artifact 上的验证结果。

### Handoff 字段

每个 handoff 包含：

- `from_agent`
- `to_agent`
- `summary`
- `artifact_refs`
- `open_questions`
- `next_objective`
- `constraints_to_preserve`
- `risk_notes`

这个协议让后续 review、replay、debug 和 evaluation 都有可追踪依据。

## 7. 数据模型

### Task

- `id`
- `title`
- `goal`
- `workflow_pack`
- `inputs`
- `constraints`
- `acceptance_criteria`
- `created_by`
- `created_at`

### Run

- `id`
- `task_id`
- `status`：`queued`、`running`、`waiting`、`failed`、`completed`、`cancelled`
- `current_step`
- `started_at`
- `finished_at`
- `final_artifact_id`

### AgentDefinition

- `id`
- `pack_name`
- `role`
- `system_prompt`
- `model_config`
- `tool_permissions`
- `runtime_limits`

### AgentRun

- `id`
- `run_id`
- `agent_id`
- `step_name`
- `input_context`
- `status`
- `started_at`
- `finished_at`
- `output_summary`

### Handoff

- `id`
- `run_id`
- `from_agent_run_id`
- `to_agent_id`
- `summary`
- `artifact_refs`
- `open_questions`
- `next_objective`
- `risk_notes`

### Artifact

- `id`
- `run_id`
- `agent_run_id`
- `type`：`design_doc`、`patch`、`test_report`、`source_summary`、`research_note`、`final_report`
- `path`
- `content_hash`
- `source_refs`
- `validation_status`
- `created_at`

### TraceEvent

- `id`
- `run_id`
- `agent_run_id`
- `event_type`：`model_action`、`tool_call`、`tool_result`、`handoff`、`artifact_created`、`eval_result`、`error`
- `payload`
- `duration_ms`
- `created_at`

### EvalResult

- `id`
- `run_id`
- `artifact_id`
- `check_name`
- `status`：`pass`、`warn`、`fail`
- `message`
- `created_at`

## 8. Code R&D Pack

目的：帮助团队从代码任务推进到经过测试和 review 的交付结果。

默认步骤：

1. `Clarifier`：把用户请求转成明确需求和验收标准。
2. `Architect`：提出实现设计，识别受影响模块。
3. `Coder`：执行修改或准备 patch。
4. `Tester`：运行相关测试并记录结果。
5. `Reviewer`：检查正确性、可维护性、安全性和性能。
6. `Finalizer`：总结变更、测试状态、风险和下一步。

必须产出的 artifact：

- Requirements summary。
- Implementation design。
- Patch 或 changed-file summary。
- Test report。
- Review report。
- Final delivery summary。

Evaluation checks：

- 需求是否明确。
- 变更文件是否符合设计。
- 测试是否执行，若未执行是否说明原因。
- Review 是否没有 unresolved blocker。
- Final summary 是否包含 residual risk。

## 9. Research Pack

目的：帮助团队产出有来源依据的研究结果。

默认步骤：

1. `Planner`：定义 research questions、范围和证据标准。
2. `Searcher`：收集候选来源。
3. `Reader`：总结来源并提取相关事实。
4. `Verifier`：交叉验证 claim，标记不确定性。
5. `Writer`：生成报告。
6. `Reviewer`：检查来源覆盖、无依据 claim、矛盾和清晰度。

必须产出的 artifact：

- Research plan。
- Source list。
- Source notes。
- Claim-evidence map。
- Draft report。
- Review report。
- Final report。

Evaluation checks：

- 每个主要 claim 是否有 source reference。
- 是否呈现冲突证据。
- 未验证 claim 是否被标记。
- 报告是否回答原始 research questions。
- 当时效性重要时，是否记录 source date。

## 10. Tool Gateway

所有工具调用必须经过统一 gateway。

Gateway 职责：

- 校验 tool name 和 input schema。
- 根据 task、pack policy、agent permission 检查权限。
- 应用 path、network、runtime 和 command restrictions。
- 必要时从 trace payload 中脱敏敏感数据。
- 记录 tool call 和 tool result event。
- 将工具错误标准化后返回给 runner。

初始工具组：

- File tools：在 workspace 内 read、write、list、search。
- Shell tools：受限命令，用于测试和项目检查。
- Web tools：Research Pack 可控 search 和 page fetch。
- Code tools：diff、lint、test command execution。
- Document tools：artifact creation 和 export。

危险操作需要明确 human approval，不属于 MVP 的自主执行范围。

## 11. Context Management

MVP 使用简单上下文模型：

- 完整 artifact 单独存储。
- Agent prompt 中传入 compact summary。
- 通过 artifact reference 传递大内容，而不是复制全文。
- 每一步都保留 task constraints 和 acceptance criteria。
- 每个 run 维护 context index，关联 task、handoff、artifact 和 trace event。

第一阶段不做跨任务 long-term memory。如果后续增加，应作为独立子系统设计，带有明确 retention 和 review 规则。

## 12. Trace、Audit 与 Evaluation

Trace 是一等产品能力，不是事后 debug log。

每个 run 必须能回答：

- 用户请求了什么 task？
- 运行了哪个 workflow pack？
- 哪些 agent 参与？
- 每个 agent 收到了什么？
- 每个 agent 产出了什么？
- 调用了哪些工具？
- 应用了哪些权限？
- 哪些步骤失败或重试？
- 哪些 evaluation check 通过或失败？

UI 需要提供可读 timeline 和 raw JSON trace view。

## 13. Error Handling

Runner 行为：

- Step failure 默认让 run 进入 `failed`，除非 pack 定义 retry policy。
- Tool validation failure 记录为 trace event，并返回给 agent 一次。
- 重复 tool failure 停止当前 step。
- Evaluation failure 根据 severity 决定阻塞 finalization 或生成 warning。
- Human intervention 可以把 run 从 `waiting` 恢复到 `running`。

MVP retry policy：

- transient tool failure 重试一次。
- permission denial 不自动重试。
- safety check 失败不自动重试。

## 14. UI 要求

Thin UI 包含：

- Task creation form。
- Workflow pack selector。
- Run list with status。
- Run detail page。
- Step timeline。
- Agent output summaries。
- Artifact list and download links。
- Evaluation result panel。
- Raw trace viewer。

第一阶段 UI 不承担完整项目管理系统职责。

## 15. API 草图

初始 endpoints：

- `POST /tasks`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/trace`
- `GET /runs/{run_id}/artifacts`
- `GET /artifacts/{artifact_id}`
- `GET /workflow-packs`
- `GET /agents`

本地 MVP 可以同步触发 runner，也可以用 FastAPI 进程内 background task。

## 16. 测试策略

Backend tests：

- Task creation validation。
- Workflow pack loading。
- Agent registry permission resolution。
- Tool gateway permission denial 和 allowed calls。
- Handoff creation 和 artifact linking。
- Trace event creation。
- Evaluation pass/warn/fail behavior。
- Runner success and failure paths。

Pack tests：

- Code R&D happy path with mocked agents。
- Code R&D reviewer blocker stops finalization。
- Research happy path with mocked sources。
- Research unsupported claim creates evaluation failure。

UI tests：

- Create task。
- Start run。
- View run status。
- Inspect trace。
- Download artifact。

## 17. 实现顺序

1. 定义 core domain models。
2. 实现 SQLite persistence。
3. 实现 workflow pack schema。
4. 实现 agent registry。
5. 实现 trace logger。
6. 实现带 mock tools 的 tool gateway。
7. 实现 single-process workflow runner。
8. 加入带 mocked agent responses 的 Code R&D Pack。
9. 加入带 mocked agent responses 的 Research Pack。
10. 加入 evaluation checks。
11. 加入 FastAPI endpoints。
12. 加入 thin UI。
13. 用真实 model calls 替换 mocked agent responses。
14. 加入 integration tests。

## 18. 关键设计决策

- 使用结构化 handoff，而不是自由 agent 群聊。
- `Harness Core` 保持场景无关。
- Workflow-specific roles 和 checks 放在 pack 中。
- MVP 使用 SQLite 和本地 artifact storage。
- 先用 single-process runner，再考虑 queue-based workers。
- Trace 和 evaluation 是必需能力，不是可选日志。
- 第一阶段不包含 business workflow automation。
