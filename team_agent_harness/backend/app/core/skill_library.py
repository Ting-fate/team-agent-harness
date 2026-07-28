from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from app.core.models import AgentDefinition, HarnessModel, Task
from app.core.role_cards import RoleCardError, _reject_sensitive_text
from app.packs.base import WorkflowPack


LOCAL_SKILL_BINDINGS = Path("config/skill-bindings.local.json")
MAX_SKILL_BYTES = 64 * 1024
MAX_BOUND_SKILLS_PER_AGENT = 5
MAX_AUTO_SKILLS_PER_AGENT = 3
MAX_BOUND_SKILL_CONTENT_BYTES = 96 * 1024
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
CODE_TOOL_NAMES = frozenset({"read_file", "list_files", "search_files", "run_test_command"})
UI_CONTEXT_TERMS = frozenset(
    {
        "ui",
        "ux",
        "frontend",
        "front-end",
        "react",
        "css",
        "html",
        "dashboard",
        "界面",
        "前端",
        "视觉",
        "样式",
    }
)
IMAGE_CONTEXT_TERMS = frozenset({"image", "images", "photo", "asset", "visual", "图片", "图像", "视觉资产"})
BROWSER_AUTOMATION_TERMS = frozenset(
    {
        "playwright",
        "e2e",
        "browser automation",
        "ui test",
        "screenshot",
        "浏览器自动化",
        "端到端",
        "截图",
    }
)
TASK_DOCUMENT_TERMS_BY_FORMAT: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf", ".pdf"}),
    "docx": frozenset({"docx", ".docx", "word", "word doc", "word document", "word 文档"}),
    "pptx": frozenset({"ppt", "pptx", ".ppt", ".pptx", "slides", "presentation", "幻灯片"}),
    "xlsx": frozenset({"xlsx", "xlsm", ".xlsx", ".xlsm", "csv", ".csv", "excel", "spreadsheet", "excel 表格"}),
}
TASK_DOMAIN_TERMS_BY_LABEL: dict[str, frozenset[str]] = {
    "security": frozenset(
        {"security", "auth", "authentication", "authorization", "jwt", "oauth", "token", "secret", "安全", "认证", "授权", "权限", "密钥"}
    ),
    "performance": frozenset({"performance", "latency", "slow", "cache", "optimize", "性能", "延迟", "慢", "缓存", "优化"}),
    "database": frozenset({"database", "sqlite", "postgres", "mysql", "schema", "migration", "数据库", "表结构", "迁移"}),
    "testing": frozenset({"test", "testing", "pytest", "coverage", "e2e", "测试", "覆盖率", "端到端", "验收"}),
    "architecture": frozenset({"architecture", "design", "refactor", "workflow", "架构", "设计", "重构", "工作流"}),
    "web_ui": frozenset({"ui", "ux", "frontend", "react", "css", "html", "界面", "前端", "样式"}),
    "ai_model": frozenset({"ai", "model", "llm", "prompt", "agent", "openai", "deepseek", "模型", "提示词", "智能体"}),
}
AUTO_SKILL_BLOCKLIST_TERMS = frozenset(
    {
        "using-superpowers",
        "brainstorming",
        "subagent-driven-development",
        "pua",
        "neat-freak",
        "storage-analyzer",
        "aigc-reduce",
        "aihot",
        "hv-analysis",
        "khazix-writer",
        "skill-installer",
        "skill-creator",
        "plugin-creator",
    }
)


class SkillLibraryError(RuntimeError):
    pass


class SkillCard(HarnessModel):
    skill_id: str
    name: str
    description: str = ""
    source_root: str
    path: str
    hash: str
    size: int
    has_scripts: bool = False
    has_references: bool = False
    risk_flags: list[str] = Field(default_factory=list)


class SkillDetail(SkillCard):
    content: str


class SkillBindingWrite(HarnessModel):
    skill_ids: list[str] = Field(default_factory=list)

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, values: list[str]) -> list[str]:
        unique: list[str] = []
        for value in values:
            if not SKILL_ID_PATTERN.fullmatch(value):
                raise ValueError("skill_ids may contain only letters, numbers, underscores, and dashes")
            if value not in unique:
                unique.append(value)
        return unique


class SkillBinding(HarnessModel):
    agent_id: str
    skill_ids: list[str] = Field(default_factory=list)
    restart_required: bool = True


class AutoSkillRoute(HarnessModel):
    agent_id: str
    skill_id: str
    reason: str


@dataclass(frozen=True)
class AutoRouteContext:
    pack_name: str
    text: str
    structural_text: str
    role_text: str
    tool_names: frozenset[str]
    step_names: frozenset[str]
    step_phases: frozenset[str]


@dataclass(frozen=True)
class SkillLibrary:
    skills: dict[str, SkillDetail]

    @classmethod
    def from_roots(cls, roots: list[str | Path], *, allow_duplicate_ids: bool = False) -> "SkillLibrary":
        skills: dict[str, SkillDetail] = {}
        for root in roots:
            root_path = Path(root).expanduser().resolve()
            if not root_path.exists():
                continue
            if not root_path.is_dir():
                raise SkillLibraryError(f"Skill root is not a directory: {root_path}")
            for skill_md in sorted(root_path.glob("*/SKILL.md")):
                detail = _read_skill(root_path, skill_md)
                if detail.skill_id in skills:
                    if allow_duplicate_ids:
                        continue
                    raise SkillLibraryError(f"Duplicate skill id: {detail.skill_id}")
                skills[detail.skill_id] = detail
        return cls(skills=skills)

    def list_skills(self) -> list[SkillCard]:
        return [
            SkillCard(**detail.model_dump(exclude={"content"}))
            for detail in sorted(self.skills.values(), key=lambda item: item.skill_id)
        ]

    def get_skill(self, skill_id: str) -> SkillDetail:
        try:
            return self.skills[skill_id]
        except KeyError as exc:
            raise SkillLibraryError("skill not found") from exc

    def has_skill(self, skill_id: str) -> bool:
        return skill_id in self.skills


def load_skill_library(config_root: str | Path, roots_override: list[str | Path] | None = None) -> SkillLibrary:
    roots = _configured_roots(config_root, roots_override)
    return SkillLibrary.from_roots(roots, allow_duplicate_ids=roots_override is None)


def read_skill_bindings(root: Path) -> list[SkillBinding]:
    raw = _read_binding_config(root)
    return [
        SkillBinding(agent_id=agent_id, skill_ids=list(route.get("skill_ids", [])))
        for agent_id, route in sorted(raw.get("agents", {}).items())
        if isinstance(route, dict)
    ]


def upsert_skill_binding(
    root: Path,
    agent_id: str,
    payload: SkillBindingWrite,
    *,
    known_agent_ids: set[str],
    library: SkillLibrary,
) -> SkillBinding:
    if agent_id not in known_agent_ids:
        raise SkillLibraryError("agent not found")
    missing = [skill_id for skill_id in payload.skill_ids if not library.has_skill(skill_id)]
    if missing:
        raise SkillLibraryError(f"unknown skill ids: {', '.join(missing)}")
    _validate_binding_limits(payload.skill_ids, library)
    raw = _read_binding_config(root)
    agents = raw.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise SkillLibraryError("skill binding config agents must be an object")
    if payload.skill_ids:
        agents[agent_id] = {"skill_ids": payload.skill_ids}
    else:
        agents.pop(agent_id, None)
    _write_binding_config(root, raw)
    return SkillBinding(agent_id=agent_id, skill_ids=payload.skill_ids)


def delete_skill_binding(root: Path, agent_id: str) -> None:
    raw = _read_binding_config(root)
    agents = raw.setdefault("agents", {})
    if isinstance(agents, dict):
        agents.pop(agent_id, None)
    _write_binding_config(root, raw)


def apply_skill_bindings_to_packs(
    packs: dict[str, WorkflowPack],
    bindings: list[SkillBinding],
    library: SkillLibrary,
) -> dict[str, WorkflowPack]:
    by_agent = {binding.agent_id: binding.skill_ids for binding in bindings}
    if not by_agent:
        return packs
    for skill_ids in by_agent.values():
        _validate_binding_limits(skill_ids, library)
    known_agents = {agent.id for pack in packs.values() for agent in pack.agents}
    unknown_agents = sorted(set(by_agent) - known_agents)
    if unknown_agents:
        raise SkillLibraryError(f"Skill bindings reference unknown agents: {', '.join(unknown_agents)}")
    return {
        pack_name: pack.model_copy(
            update={"agents": [_apply_skills_to_agent(agent, by_agent.get(agent.id, []), library) for agent in pack.agents]}
        )
        for pack_name, pack in packs.items()
    }


def apply_auto_skill_routes_to_packs(
    packs: dict[str, WorkflowPack],
    library: SkillLibrary,
) -> tuple[dict[str, WorkflowPack], list[AutoSkillRoute]]:
    routes_by_agent: dict[str, list[tuple[str, str]]] = {}
    for pack in packs.values():
        for agent in pack.agents:
            selected = _select_auto_skills(agent, pack, library)
            if selected:
                routes_by_agent[agent.id] = selected
    if not routes_by_agent:
        return packs, []

    updated_packs: dict[str, WorkflowPack] = {}
    routes: list[AutoSkillRoute] = []
    for pack_name, pack in packs.items():
        agents = []
        for agent in pack.agents:
            selected = routes_by_agent.get(agent.id, [])
            if not selected:
                agents.append(agent)
                continue
            skill_ids = [skill_id for skill_id, _ in selected]
            agents.append(_apply_skills_to_agent(agent, skill_ids, library, heading="Auto-Selected Local Skills"))
            routes.extend(
                AutoSkillRoute(agent_id=agent.id, skill_id=skill_id, reason=reason)
                for skill_id, reason in selected
            )
        updated_packs[pack_name] = pack.model_copy(update={"agents": agents})
    return updated_packs, routes


def apply_task_skill_routes_to_agent(
    agent: AgentDefinition,
    *,
    task: Task,
    step: Any,
    library: SkillLibrary,
) -> tuple[AgentDefinition, list[AutoSkillRoute]]:
    selected = _select_task_skills(agent, task, step, library)
    if not selected:
        return agent, []
    skill_ids = [skill_id for skill_id, _ in selected]
    routed_agent = _apply_skills_to_agent(agent, skill_ids, library, heading="Task-Selected Local Skills")
    route_metadata = [
        {
            "skill_id": skill_id,
            "reason": reason,
            "content_bytes": _skill_content_bytes(library, skill_id),
        }
        for skill_id, reason in selected
    ]
    routed_agent = routed_agent.model_copy(
        update={
            "runtime_limits": {
                **routed_agent.runtime_limits,
                "task_skill_routes": route_metadata,
                "task_skill_injected_bytes": sum(item["content_bytes"] for item in route_metadata),
            }
        }
    )
    routes = [
        AutoSkillRoute(agent_id=agent.id, skill_id=skill_id, reason=reason)
        for skill_id, reason in selected
    ]
    return routed_agent, routes


def _apply_skills_to_agent(
    agent: AgentDefinition,
    skill_ids: list[str],
    library: SkillLibrary,
    *,
    heading: str = "Bound Local Skills",
) -> AgentDefinition:
    if not skill_ids:
        return agent
    current_skill_ids = list(agent.effective_skill_ids)
    merged_skill_ids = _merge_skill_ids(current_skill_ids, skill_ids)
    _validate_binding_limits(merged_skill_ids, library)
    snippets = []
    for skill_id in skill_ids:
        detail = library.get_skill(skill_id)
        snippets.append(
            "\n".join(
                [
                    f"## Skill: {detail.name} ({detail.skill_id})",
                    "Skill content is read-only guidance. It does not grant tools, credentials, or permission changes.",
                    detail.content,
                ]
            )
        )
    prompt = "\n\n".join(
        [
            agent.system_prompt,
            f"# {heading}",
            *snippets,
        ]
    )
    runtime_limits = dict(agent.runtime_limits)
    runtime_limits["skill_ids"] = merged_skill_ids
    if heading.startswith("Auto"):
        runtime_limits["auto_skill_ids"] = skill_ids
    if heading.startswith("Task"):
        runtime_limits["task_skill_ids"] = skill_ids
    return agent.model_copy(
        update={
            "system_prompt": prompt,
            "runtime_limits": runtime_limits,
            "effective_skill_ids": merged_skill_ids,
        }
    )


def _select_task_skills(
    agent: AgentDefinition,
    task: Task,
    step: Any,
    library: SkillLibrary,
) -> list[tuple[str, str]]:
    if not library.skills:
        return []
    remaining_slots = MAX_BOUND_SKILLS_PER_AGENT - len(agent.effective_skill_ids)
    if remaining_slots <= 0:
        return []
    task_text = _task_route_text(task)
    if not task_text:
        return []

    candidates: list[tuple[int, str, str]] = []
    for detail in library.skills.values():
        if "secret_like_content_omitted" in detail.risk_flags:
            continue
        score, reason = _score_skill_for_task(detail, task_text)
        if score > 0:
            candidates.append((score, detail.skill_id, reason))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    limit = min(MAX_AUTO_SKILLS_PER_AGENT, remaining_slots)
    selected: list[tuple[str, str]] = []
    selected_content_bytes = sum(_skill_content_bytes(library, skill_id) for skill_id in agent.effective_skill_ids)
    for _, skill_id, reason in candidates:
        if len(selected) >= limit:
            break
        if skill_id in agent.effective_skill_ids:
            continue
        next_content_bytes = selected_content_bytes + _skill_content_bytes(library, skill_id)
        if next_content_bytes > MAX_BOUND_SKILL_CONTENT_BYTES:
            continue
        selected.append((skill_id, reason))
        selected_content_bytes = next_content_bytes
    return selected


def _score_skill_for_task(detail: SkillDetail, task_text: str) -> tuple[int, str]:
    labels = _routable_skill_labels(detail)
    if not labels:
        return 0, ""

    score = 0
    reasons: list[str] = []
    if "document_file" in labels:
        matched_formats = [
            document_format
            for document_format in _skill_document_formats(detail)
            if _context_has_any(task_text, TASK_DOCUMENT_TERMS_BY_FORMAT[document_format])
        ]
        if matched_formats:
            score += 70
            reasons.append(f"任务内容明确包含 {', '.join(matched_formats)} 文件处理信号")

    if "browser_automation" in labels and _context_has_any(task_text, BROWSER_AUTOMATION_TERMS):
        score += 70
        reasons.append("任务内容明确包含浏览器自动化或端到端测试信号")

    specialized_labels = {
        "browser_automation",
        "document_file",
        "image_generation",
        "openai_docs",
        "ui_design",
    }
    skill_identity_text = " ".join([detail.skill_id, detail.name]).lower()

    domain_labels = [
        label
        for label, terms in TASK_DOMAIN_TERMS_BY_LABEL.items()
        if not labels.intersection(specialized_labels)
        and label in labels
        and _context_has_any(skill_identity_text, terms)
        and _context_has_any(task_text, terms)
    ]
    if domain_labels:
        score += 65
        reasons.append(f"任务内容明确包含 {', '.join(domain_labels)} 领域信号")

    if score <= 0:
        return 0, ""
    return _skill_score_result(detail, score, reasons)


def _select_auto_skills(
    agent: AgentDefinition,
    pack: WorkflowPack,
    library: SkillLibrary,
) -> list[tuple[str, str]]:
    if not library.skills:
        return []
    remaining_slots = MAX_BOUND_SKILLS_PER_AGENT - len(agent.effective_skill_ids)
    if remaining_slots <= 0:
        return []
    context = _auto_route_context(pack, agent)
    candidates: list[tuple[int, str, str]] = []
    for detail in library.skills.values():
        if "secret_like_content_omitted" in detail.risk_flags:
            continue
        score, reason = _score_skill_for_context(detail, context, agent)
        if score > 0:
            candidates.append((score, detail.skill_id, reason))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    limit = min(MAX_AUTO_SKILLS_PER_AGENT, remaining_slots)
    selected: list[tuple[str, str]] = []
    selected_content_bytes = sum(_skill_content_bytes(library, skill_id) for skill_id in agent.effective_skill_ids)
    for _, skill_id, reason in candidates:
        if len(selected) >= limit:
            break
        next_content_bytes = selected_content_bytes + _skill_content_bytes(library, skill_id)
        if next_content_bytes > MAX_BOUND_SKILL_CONTENT_BYTES:
            continue
        selected.append((skill_id, reason))
        selected_content_bytes = next_content_bytes
    return selected


def _score_skill_for_context(
    detail: SkillDetail,
    context: AutoRouteContext,
    agent: AgentDefinition,
) -> tuple[int, str]:
    if agent.effective_skill_ids and detail.skill_id in agent.effective_skill_ids:
        return 0, ""

    labels = _routable_skill_labels(detail)
    if not labels:
        return 0, ""
    if "web_access" in labels:
        return 0, ""

    score = 0
    reasons: list[str] = []
    is_reviewer = _context_has_any(context.role_text, {"review", "reviewer", "verifier", "审核", "审查", "把关"})
    is_research_pack = context.pack_name == "research"
    is_code_pack = context.pack_name.startswith("code_rd")
    has_code_tools = bool(context.tool_names & CODE_TOOL_NAMES)

    if "knowledge_research" in labels and is_research_pack:
        score += 60
        reasons.append("工作流是知识研究场景")
        if _context_has_any(context.role_text, {"planner", "reader", "verifier", "writer", "searcher", "研究", "资料"}):
            score += 10
            reasons.append("智能体职责与研究技能匹配")

    if "code_review" in labels and is_code_pack and is_reviewer:
        score += 60
        reasons.append("代码研发工作流中的审查或质量把关角色")

    if "code_development" in labels and is_code_pack and has_code_tools:
        score += 35
        reasons.append("代码研发工作流中的代码读取、测试或实现角色")

    if "subagent_workflow" in labels and context.pack_name == "code_rd_institutional":
        if _context_has_any(context.role_text, {"planner", "dispatcher", "synthesizer", "approver", "主线程", "派发"}):
            score += 45
            reasons.append("制度化代码研发工作流中的主控/派发/汇总角色")

    if "ui_design" in labels and _context_has_any(context.structural_text, UI_CONTEXT_TERMS):
        score += 50
        reasons.append("上下文明确包含 UI / 前端设计信号")

    if "browser_automation" in labels and _context_has_any(context.structural_text, BROWSER_AUTOMATION_TERMS):
        score += 50
        reasons.append("上下文明确包含浏览器自动化或端到端测试信号")

    if "image_generation" in labels and _context_has_any(context.structural_text, IMAGE_CONTEXT_TERMS):
        score += 50
        reasons.append("上下文明确包含图片或视觉资产生成信号")

    if "openai_docs" in labels and _context_has_any(
        context.text,
        {"openai api", "responses api", "chat completions api", "codex cli", "codex sdk"},
    ):
        score += 35
        reasons.append("上下文明确涉及 OpenAI / Codex 产品或 API")

    if "document_file" in labels:
        document_terms = {"pdf", "docx", "pptx", "xlsx", "word", "excel", "slides", "spreadsheet", "文档", "表格", "演示"}
        if _context_has_any(context.structural_text, document_terms):
            score += 45
            reasons.append("上下文明确包含文档或表格文件处理信号")

    if score <= 0:
        return 0, ""
    return _skill_score_result(detail, score, reasons)


def _routable_skill_labels(detail: SkillDetail) -> set[str]:
    labels = _skill_labels(detail)
    if not labels or "blocked_by_default" in labels:
        return set()
    return labels


def _skill_score_result(detail: SkillDetail, score: int, reasons: list[str]) -> tuple[int, str]:
    if detail.has_scripts:
        reasons.append("包含脚本但仅作为只读说明注入")
    if detail.has_references:
        reasons.append("包含 references 但不会自动读取")
    return score, "；".join(dict.fromkeys(reasons))


def _auto_route_context(pack: WorkflowPack, agent: AgentDefinition) -> AutoRouteContext:
    agent_steps = [step for step in pack.steps if step.agent_role == agent.role]
    tool_names = set(agent.tool_permissions)
    for step in agent_steps:
        tool_names.update(step.allowed_tools)
    structural_text = " ".join(
        [
            pack.name,
            pack.description,
            agent.id,
            agent.pack_name,
            agent.role,
            " ".join(sorted(tool_names)),
            " ".join(step.name for step in agent_steps),
            " ".join(step.phase or "" for step in agent_steps),
            " ".join(step.produces_artifact_type or "" for step in agent_steps),
        ]
    ).lower()
    text = " ".join(
        [
            structural_text,
            agent.system_prompt,
        ]
    ).lower()
    role_text = " ".join([agent.id, agent.role]).lower()
    return AutoRouteContext(
        pack_name=pack.name,
        text=text,
        structural_text=structural_text,
        role_text=role_text,
        tool_names=frozenset(tool_names),
        step_names=frozenset(step.name for step in agent_steps),
        step_phases=frozenset(step.phase or "" for step in agent_steps),
    )


def _task_route_text(task: Task) -> str:
    serialized_inputs = json.dumps(task.inputs, ensure_ascii=False, sort_keys=True)
    return " ".join(
        [
            task.title,
            task.goal,
            serialized_inputs,
            " ".join(task.constraints),
            " ".join(task.acceptance_criteria),
        ]
    ).lower()


def _skill_document_formats(detail: SkillDetail) -> list[str]:
    key_text = " ".join([detail.skill_id, detail.name]).lower()
    formats = [
        document_format
        for document_format, terms in TASK_DOCUMENT_TERMS_BY_FORMAT.items()
        if _context_has_any(key_text, terms)
    ]
    return formats


def _skill_labels(detail: SkillDetail) -> set[str]:
    key_text = " ".join([detail.skill_id, detail.name]).lower()
    full_text = " ".join([detail.skill_id, detail.name, detail.description]).lower()
    labels: set[str] = set()

    if any(term in key_text for term in AUTO_SKILL_BLOCKLIST_TERMS):
        labels.add("blocked_by_default")
    if "web-access" in key_text:
        labels.add("web_access")
    if _context_has_any(key_text, {"research", "researcher", "analysis", "研究", "调研"}):
        labels.add("knowledge_research")
    if any(term in key_text for term in ("frontend", "ui-ux", "ui_ux")):
        labels.add("ui_design")
    if any(term in key_text for term in ("playwright", "browser-testing", "browser testing", "devtools")):
        labels.add("browser_automation")
    if "imagegen" in key_text or "image-generation" in key_text:
        labels.add("image_generation")
    if "openai-docs" in key_text:
        labels.add("openai_docs")
    if any(term in key_text for term in ("pdf", "docx", "pptx", "xlsx")):
        labels.add("document_file")
    if _context_has_any(full_text, {"code review", "code reviewer", "security review", "correctness", "maintainability"}):
        labels.add("code_review")
    if _context_has_any(key_text, {"code development", "coding", "test-driven", "implementation", "代码研发"}):
        labels.add("code_development")
    for label, terms in TASK_DOMAIN_TERMS_BY_LABEL.items():
        if _context_has_any(full_text, terms):
            labels.add(label)
    return labels


def _context_has_any(text: str, terms: set[str] | frozenset[str]) -> bool:
    return any(_text_contains_term(text, term) for term in terms)


def _text_contains_term(text: str, term: str) -> bool:
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        return term in text
    if re.fullmatch(r"[a-z0-9]+", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _merge_skill_ids(existing: list[str], new_skill_ids: list[str]) -> list[str]:
    merged = list(existing)
    for skill_id in new_skill_ids:
        if skill_id not in merged:
            merged.append(skill_id)
    return merged


def _skill_content_bytes(library: SkillLibrary, skill_id: str) -> int:
    return len(library.get_skill(skill_id).content.encode("utf-8"))


def _configured_roots(config_root: str | Path, roots_override: list[str | Path] | None = None) -> list[Path]:
    if roots_override is not None:
        return [Path(root).expanduser().resolve() for root in roots_override]
    home = Path.home()
    return _dedupe_nested_roots([
        home / ".codex" / "skills",
        home / ".agents" / "skills",
        home / ".codex" / "skills" / ".system",
    ])


def _dedupe_nested_roots(roots: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    for root in [path.expanduser().resolve() for path in roots]:
        if any(root == existing or _is_within(root, existing) for existing in deduped):
            continue
        deduped = [existing for existing in deduped if not _is_within(existing, root)]
        deduped.append(root)
    return deduped


def _read_skill(root: Path, skill_md: Path) -> SkillDetail:
    if skill_md.is_symlink() or skill_md.parent.is_symlink():
        raise SkillLibraryError("Skill symlinks are not allowed.")
    resolved = skill_md.resolve()
    if not _is_within(resolved, root):
        raise SkillLibraryError("Skill path must stay inside configured root.")
    if resolved.stat().st_size > MAX_SKILL_BYTES:
        raise SkillLibraryError("Skill file is too large.")
    raw = resolved.read_text(encoding="utf-8-sig")
    secret_like_content = False
    try:
        _reject_sensitive_text(raw)
    except RoleCardError:
        secret_like_content = True
    frontmatter, content = _parse_skill_markdown(raw)
    content = content.strip()
    if not content:
        raise SkillLibraryError(f"Skill file is empty after frontmatter: {resolved}")
    skill_dir = resolved.parent
    source_root_name = _safe_id(root.name or "skills")
    skill_id = f"{source_root_name}-{_safe_id(skill_dir.name)}"
    risk_flags = []
    has_scripts = (skill_dir / "scripts").is_dir()
    has_references = (skill_dir / "references").is_dir()
    if has_scripts:
        risk_flags.append("scripts_disabled")
    if has_references:
        risk_flags.append("references_not_loaded")
    if secret_like_content:
        risk_flags.append("secret_like_content_omitted")
        content = "Skill content omitted because it matched secret-like patterns. Review the local SKILL.md manually before binding."
        name = skill_dir.name
        description = ""
    else:
        name = frontmatter.get("name") or skill_dir.name
        description = frontmatter.get("description", "")
    return SkillDetail(
        skill_id=skill_id,
        name=name,
        description=description,
        source_root=str(root),
        path=str(resolved),
        hash=sha256(raw.encode("utf-8")).hexdigest(),
        size=resolved.stat().st_size,
        has_scripts=has_scripts,
        has_references=has_references,
        risk_flags=risk_flags,
        content=content,
    )


def _validate_binding_limits(skill_ids: list[str], library: SkillLibrary) -> None:
    if len(skill_ids) > MAX_BOUND_SKILLS_PER_AGENT:
        raise SkillLibraryError(f"at most {MAX_BOUND_SKILLS_PER_AGENT} skills can be bound to one agent")
    total_bytes = sum(_skill_content_bytes(library, skill_id) for skill_id in skill_ids)
    if total_bytes > MAX_BOUND_SKILL_CONTENT_BYTES:
        raise SkillLibraryError(f"bound skill content exceeds {MAX_BOUND_SKILL_CONTENT_BYTES} bytes")


def _read_binding_config(root: Path) -> dict[str, Any]:
    path = _binding_config_path(root)
    if not path.exists():
        return {"agents": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillLibraryError("skill binding config is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise SkillLibraryError("skill binding config must be an object")
    raw.setdefault("agents", {})
    try:
        for route in raw["agents"].values():
            if isinstance(route, dict):
                SkillBindingWrite.model_validate({"skill_ids": route.get("skill_ids", [])})
    except ValidationError as exc:
        raise SkillLibraryError("skill binding config is invalid") from exc
    return raw


def _write_binding_config(root: Path, raw: dict[str, Any]) -> None:
    path = _binding_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _binding_config_path(root: Path) -> Path:
    return (root / LOCAL_SKILL_BINDINGS).resolve()


def _parse_skill_markdown(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return _parse_simple_frontmatter(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillLibraryError("skill frontmatter is malformed")


def _parse_simple_frontmatter(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _safe_id(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")
    if not candidate:
        raise SkillLibraryError("skill id cannot be empty")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
