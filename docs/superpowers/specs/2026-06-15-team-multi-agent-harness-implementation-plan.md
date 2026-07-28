# 团队版 Multi-Agent Harness MVP 实施计划

> Historical baseline from 2026-06-15. This plan records the original MVP sequence; current local worker, restart recovery, and bounded context behavior are specified in the 2026-07-10 durable-run documents.

## 1. 实施原则

- 先做能跑通的 vertical slice，再补完整性。
- `Harness Core` 与 workflow pack 分离，避免把 Code R&D 或 Research 的逻辑写死在 runner 里。
- 第一阶段使用 mocked agent responses 打通链路，再接真实 model calls。
- 所有工具调用必须通过 `Tool Gateway`，不能让 agent 直接访问 shell、文件或网络。
- Trace、artifact、handoff 从第一天就是必需数据，不后补。

## 2. 推荐项目结构

```text
team_agent_harness/
  backend/
    app/
      main.py
      core/
        models.py
        storage.py
        runner.py
        registry.py
        tool_gateway.py
        trace.py
        evaluation.py
        artifacts.py
      packs/
        base.py
        code_rd/
          pack.py
          agents.py
          evals.py
        research/
          pack.py
          agents.py
          evals.py
      api/
        tasks.py
        runs.py
        artifacts.py
        packs.py
      tools/
        file_tools.py
        shell_tools.py
        web_tools.py
      tests/
        test_models.py
        test_runner.py
        test_tool_gateway.py
        test_code_rd_pack.py
        test_research_pack.py
    pyproject.toml
  frontend/
    src/
      app/
      components/
      api/
    package.json
  data/
    artifacts/
    harness.sqlite3
```

## 3. Phase 0：项目骨架

目标：创建可运行的 backend skeleton。

任务：

1. 初始化 Python 项目。
2. 安装 FastAPI、Pydantic、SQLAlchemy 或 SQLModel、pytest、httpx。
3. 创建 `app/main.py`，提供 health check。
4. 创建基础测试和本地运行命令。

验收：

- `pytest` 通过。
- `uvicorn app.main:app --reload` 能启动。
- `GET /health` 返回 OK。

## 4. Phase 1：Core Domain Models

目标：先把核心对象定义清楚。

实现：

- `Task`
- `Run`
- `AgentDefinition`
- `AgentRun`
- `Handoff`
- `Artifact`
- `TraceEvent`
- `EvalResult`

建议：

- 先用 Pydantic model 定义输入输出。
- SQLite persistence 可以随后接入，不要一开始被 ORM 细节拖住。

验收：

- 每个 model 有单元测试。
- status、event_type、artifact type 使用 enum。
- model 能序列化成 JSON。

## 5. Phase 2：Storage Layer

目标：将核心对象持久化到 SQLite。

任务：

1. 建立 SQLite schema。
2. 实现 repository 函数：
   - create/list/get task
   - create/update/get run
   - create agent run
   - create handoff
   - create artifact
   - append trace event
   - create eval result
3. 添加事务边界。

验收：

- storage tests 覆盖 create/get/list。
- run 状态更新可追踪。
- artifact 与 run、agent_run 能正确关联。

## 6. Phase 3：Workflow Pack Schema

目标：定义 pack 如何声明 workflow。

核心接口：

```python
class WorkflowPack:
    name: str
    description: str
    steps: list[WorkflowStep]
    eval_checks: list[EvalCheck]
```

```python
class WorkflowStep:
    name: str
    agent_role: str
    required_inputs: list[str]
    required_artifacts: list[str]
    allowed_tools: list[str]
```

验收：

- 能加载 `Code R&D Pack` 和 `Research Pack`。
- Runner 不需要知道 pack 的具体业务逻辑。
- Pack 能声明 agent、步骤、工具权限和 evaluation check。

## 7. Phase 4：Agent Registry

目标：统一管理 agent 定义。

任务：

1. 定义 agent role、system prompt、model_config、tool_permissions。
2. 根据 pack 自动注册默认 agents。
3. 提供查询接口：按 pack/role 获取 agent definition。

验收：

- `Code R&D Pack` 能注册 Clarifier、Architect、Coder、Tester、Reviewer、Finalizer。
- `Research Pack` 能注册 Planner、Searcher、Reader、Verifier、Writer、Reviewer。
- 每个 agent 都有明确 tool permission。

## 8. Phase 5：Trace Logger 与 Artifact Store

目标：所有关键行为可观察。

Trace events：

- `model_action`
- `tool_call`
- `tool_result`
- `handoff`
- `artifact_created`
- `eval_result`
- `error`

Artifact store：

- MVP 写入 `data/artifacts/{run_id}/...`。
- Artifact metadata 入库。
- 文件内容计算 `content_hash`。

验收：

- 创建 artifact 时自动生成 trace event。
- 每个 agent step 至少有 start、artifact、handoff 或 error 记录。
- UI/API 可读取 run trace。

## 9. Phase 6：Tool Gateway

目标：建立唯一工具调用入口。

第一批工具：

- `read_file`
- `write_artifact`
- `list_files`
- `search_files`
- `run_test_command`
- `web_search_mock`
- `fetch_page_mock`

MVP 可以先 mock web tools 和 shell tools，但接口必须按真实工具设计。

权限策略：

- Agent 只能调用 workflow step 允许的工具。
- 文件读写限制在 workspace / artifact 目录。
- Shell 命令使用 allowlist。
- Permission denial 记录 trace，不自动重试。

验收：

- 未授权工具调用被拒绝。
- 授权工具调用记录 `tool_call` 和 `tool_result`。
- 工具错误标准化返回。

## 10. Phase 7：Single-Process Workflow Runner

目标：跑通一个完整 run。

Runner 流程：

1. 创建 run。
2. 加载 workflow pack。
3. 对每个 step 创建 `AgentRun`。
4. 构造 input context。
5. 调用 agent executor。
6. 保存 artifact。
7. 保存 handoff。
8. 执行 checkpoint eval。
9. 更新 run 状态。

MVP agent executor：

- 先使用 deterministic mocked responses。
- 每个 mocked agent 必须返回结构化 output：summary、artifacts、handoff。

验收：

- 一个 Code R&D task 能从 Clarifier 跑到 Finalizer。
- 一个 Research task 能从 Planner 跑到 Reviewer。
- 失败时 run 进入 `failed`，trace 中有 error。

## 11. Phase 8：Code R&D Pack

目标：实现代码研发工作流。

步骤：

1. `Clarifier`
2. `Architect`
3. `Coder`
4. `Tester`
5. `Reviewer`
6. `Finalizer`

Mock artifact：

- requirements summary
- implementation design
- changed-file summary
- test report
- review report
- final delivery summary

Eval checks：

- requirements explicit
- design exists
- test report exists
- review has no blocker
- final summary includes residual risk

验收：

- happy path completed。
- reviewer blocker 能阻止 finalization 或标记 run failed。
- trace 能看到每个步骤产物。

## 12. Phase 9：Research Pack

目标：实现知识研究工作流。

步骤：

1. `Planner`
2. `Searcher`
3. `Reader`
4. `Verifier`
5. `Writer`
6. `Reviewer`

Mock artifact：

- research plan
- source list
- source notes
- claim-evidence map
- draft report
- review report
- final report

Eval checks：

- major claims have sources
- unsupported claims marked
- conflicting evidence represented
- report answers research questions
- source date recorded when recency matters

验收：

- happy path completed。
- unsupported claim 触发 eval fail。
- final report 能追溯到 source list。

## 13. Phase 10：FastAPI Endpoints

目标：暴露最小可用 API。

Endpoints：

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

验收：

- API tests 使用 `httpx` 覆盖主要路径。
- 可以通过 API 创建 task、启动 run、查看 trace、读取 artifact。

## 14. Phase 11：Thin UI

目标：做一个不复杂但可用的控制台。

页面：

- Task creation page。
- Run list。
- Run detail。
- Step timeline。
- Trace viewer。
- Artifact list。
- Evaluation panel。

实现建议：

- 第一版不要做复杂权限和用户系统。
- 不做项目管理看板。
- Run detail 是最重要页面。

验收：

- 用户能从 UI 创建 task。
- 用户能启动 run。
- 用户能看到每个 step 的状态。
- 用户能查看 artifact 和 trace。

## 15. Phase 12：接入真实模型调用

目标：将 mocked agent executor 替换为真实 model executor。

任务：

1. 定义 model provider interface。
2. 将 agent system prompt、task context、artifact refs 组装成 model input。
3. 要求模型输出结构化 JSON。
4. 对输出做 schema validation。
5. 失败时记录 trace 并进入 retry 或 failed。

验收：

- 至少一个 pack 能使用真实模型跑通。
- 非 JSON 输出会被拒绝并记录 trace。
- 模型调用不会绕过 Tool Gateway。

## 16. Phase 13：集成测试与演示脚本

目标：提供可重复验证的 demo。

演示任务：

- Code R&D：输入一个小型代码修改请求，跑出 design、mock patch、test report、review、final summary。
- Research：输入一个研究问题，跑出 source list、claim-evidence map、final report。

验收：

- 一条命令能初始化数据库。
- 一条命令能启动后端。
- 一条命令能跑完 demo task。
- README 记录本地运行步骤。

## 17. 风险与控制

主要风险：

- Scope creep：容易提前做企业权限、分布式队列、复杂 UI。
- Agent output 不稳定：真实模型输出可能不符合 schema。
- Trace 数据过大：payload 需要摘要和敏感信息处理。
- Tool safety：shell 和文件工具需要严格 allowlist。
- Pack/core 耦合：runner 不能写死具体 workflow。

控制方式：

- 所有新增功能先判断是否属于 MVP。
- 真实模型接入前先用 mocked executor 完成全链路测试。
- Tool Gateway 做唯一入口。
- Evaluation failure 的 severity 明确化。
- Code R&D 和 Research 两个 pack 都跑通后再扩展第三个 pack。

## 18. 第一周建议目标

第一周只做 backend vertical slice：

1. 项目骨架。
2. Core models。
3. SQLite storage。
4. Pack schema。
5. Agent registry。
6. Trace logger。
7. Mocked Code R&D Pack 跑通。

第一周不做：

- UI。
- 真实模型调用。
- Research Pack 完整实现。
- 分布式 worker。
- 用户权限系统。

验收标准：

- 能通过一个 Python 命令创建 task 并跑完 mocked Code R&D workflow。
- SQLite 中能看到 task、run、agent_run、handoff、artifact、trace_event。
- 输出目录中能看到 final delivery summary artifact。
