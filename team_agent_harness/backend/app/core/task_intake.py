from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from app.core.models import HarnessModel, Task


class TaskIntakeRequest(HarnessModel):
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class TaskIntakeResult(HarnessModel):
    task_type: str = Field(min_length=1)
    complexity: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    recommended_pack: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


TASK_TYPE_TERMS: dict[str, set[str]] = {
    "bug": {"bug", "fix", "error", "exception", "failure", "broken", "报错", "错误", "修复"},
    "review": {"review", "audit", "inspect", "审查", "审核", "评审", "复核"},
    "research": {"research", "investigate", "compare", "analyze", "调研", "研究", "分析", "对比"},
    "refactor": {"refactor", "cleanup", "restructure", "重构", "整理代码"},
    "test": {"test", "pytest", "coverage", "测试", "验收"},
    "documentation": {"doc", "docs", "document", "readme", "文档", "说明"},
    "feature": {"add", "implement", "build", "create", "新增", "实现", "添加", "开发"},
}

DOMAIN_TERMS: dict[str, set[str]] = {
    "security": {"auth", "jwt", "oauth", "permission", "secret", "token", "安全", "认证", "授权", "权限", "密钥"},
    "performance": {"performance", "latency", "cache", "slow", "优化", "性能", "延迟", "缓存"},
    "database": {"database", "sqlite", "postgres", "mysql", "migration", "schema", "数据库", "迁移", "表结构"},
    "testing": {"test", "pytest", "coverage", "e2e", "测试", "覆盖率", "验收"},
    "web_ui": {"ui", "ux", "frontend", "react", "css", "html", "界面", "前端", "样式"},
    "ai_model": {"model", "prompt", "agent", "llm", "openai", "deepseek", "模型", "提示词", "智能体"},
    "documentation": {"docx", "pdf", "pptx", "xlsx", "readme", "文档", "报告", "表格", "演示"},
}

HIGH_RISK_TERMS = {
    "auth",
    "jwt",
    "oauth",
    "permission",
    "secret",
    "token",
    "payment",
    "writeback",
    "delete",
    "migration",
    "schema",
    "安全",
    "认证",
    "授权",
    "权限",
    "密钥",
    "支付",
    "删除",
    "迁移",
}
MEDIUM_RISK_TERMS = {
    "concurrency",
    "parallel",
    "cache",
    "performance",
    "database",
    "api",
    "并发",
    "性能",
    "数据库",
    "接口",
}
LARGE_TERMS = {"architecture", "multi", "workflow", "cross-module", "架构", "多模块", "跨模块", "工作流"}
SMALL_TERMS = {"typo", "copy", "rename", "one file", "小改", "错字", "改名", "单文件"}


def analyze_task_intake(
    request: TaskIntakeRequest | Task,
    *,
    available_packs: set[str] | None = None,
) -> TaskIntakeResult:
    text = _route_text(request)
    task_type, task_type_reason = _classify_task_type(text)
    domain, domain_reason = _classify_domain(text)
    risk, risk_reason = _classify_risk(text, domain)
    complexity, complexity_reason = _classify_complexity(request, text, risk, domain)
    recommended_pack, pack_reason = _recommend_pack(task_type, complexity, risk, domain, available_packs)

    constraints = _recommended_constraints(request.constraints, task_type, complexity, risk, domain)
    reasons = [
        task_type_reason,
        domain_reason,
        risk_reason,
        complexity_reason,
        pack_reason,
    ]
    confidence = _confidence(task_type, domain, risk, complexity, recommended_pack, available_packs)
    return TaskIntakeResult(
        task_type=task_type,
        complexity=complexity,
        risk=risk,
        domain=domain,
        recommended_pack=recommended_pack,
        confidence=confidence,
        reasons=[reason for reason in reasons if reason],
        constraints=constraints,
    )


def _route_text(request: TaskIntakeRequest | Task) -> str:
    serialized_inputs = json.dumps(request.inputs, ensure_ascii=False, sort_keys=True)
    return " ".join(
        [
            request.title,
            request.goal,
            serialized_inputs,
            " ".join(request.constraints),
            " ".join(request.acceptance_criteria),
        ]
    ).lower()


def _classify_task_type(text: str) -> tuple[str, str]:
    for task_type in ("bug", "review", "research", "refactor", "test", "documentation", "feature"):
        matches = sorted(term for term in TASK_TYPE_TERMS[task_type] if _contains_term(text, term))
        if matches:
            return task_type, f"任务文本命中 {task_type} 信号: {', '.join(matches[:3])}"
    return "feature", "未命中更具体任务类型，默认按 feature 处理"


def _classify_domain(text: str) -> tuple[str, str]:
    best_domain = "code"
    best_matches: list[str] = []
    for domain, terms in DOMAIN_TERMS.items():
        matches = sorted(term for term in terms if _contains_term(text, term))
        if len(matches) > len(best_matches):
            best_domain = domain
            best_matches = matches
    if best_matches:
        return best_domain, f"任务文本命中 {best_domain} 领域信号: {', '.join(best_matches[:3])}"
    return best_domain, "未命中特定领域信号，默认按 code 领域处理"


def _classify_risk(text: str, domain: str) -> tuple[str, str]:
    high_matches = sorted(term for term in HIGH_RISK_TERMS if _contains_term(text, term))
    if high_matches or domain in {"security", "database"}:
        reason = f"命中高风险信号: {', '.join(high_matches[:3])}" if high_matches else f"{domain} 领域默认高风险"
        return "high", reason
    medium_matches = sorted(term for term in MEDIUM_RISK_TERMS if _contains_term(text, term))
    if medium_matches or domain in {"performance", "ai_model"}:
        reason = f"命中中风险信号: {', '.join(medium_matches[:3])}" if medium_matches else f"{domain} 领域默认中风险"
        return "medium", reason
    return "low", "未命中高/中风险信号"


def _classify_complexity(
    request: TaskIntakeRequest | Task,
    text: str,
    risk: str,
    domain: str,
) -> tuple[str, str]:
    if any(_contains_term(text, term) for term in SMALL_TERMS):
        return "S", "命中小改/单文件信号"
    if risk == "high" or domain in {"security", "database"}:
        return "L", "高风险或关键领域任务默认按 L 级处理"
    if any(_contains_term(text, term) for term in LARGE_TERMS):
        return "L", "命中架构/多模块复杂度信号"
    explicit_surface = len(request.inputs) + len(request.constraints) + len(request.acceptance_criteria)
    if explicit_surface >= 4:
        return "M", "任务输入、约束或验收条件较多，按 M 级处理"
    return "M", "默认按 M 级处理，保留规划和验收空间"


def _recommend_pack(
    task_type: str,
    complexity: str,
    risk: str,
    domain: str,
    available_packs: set[str] | None,
) -> tuple[str, str]:
    if task_type == "research" and _pack_available("research", available_packs):
        return "research", "研究/分析任务映射到 research pack"
    if domain == "documentation" and task_type in {"research", "documentation"} and _pack_available("research", available_packs):
        return "research", "文档/资料任务优先映射到 research pack"
    if (complexity == "L" or risk == "high") and _pack_available("code_rd_institutional", available_packs):
        return "code_rd_institutional", "L 级或高风险代码任务映射到 code_rd_institutional pack"
    if _pack_available("code_rd", available_packs):
        return "code_rd", "默认代码任务映射到 code_rd pack"
    if available_packs:
        fallback = sorted(available_packs)[0]
        return fallback, f"默认 pack 不可用，回退到 {fallback}"
    return "code_rd", "未提供 pack 列表，默认推荐 code_rd"


def _recommended_constraints(
    existing: list[str],
    task_type: str,
    complexity: str,
    risk: str,
    domain: str,
) -> list[str]:
    constraints = list(dict.fromkeys(item for item in existing if item.strip()))
    additions: list[str] = []
    if risk == "high":
        additions.append("高风险任务：需要保留人工确认点和明确验证证据。")
    if complexity == "L":
        additions.append("L 级任务：需要先产出计划，再执行实现和复核。")
    if domain == "security":
        additions.append("安全相关：不得泄漏密钥、token 或认证细节到 trace/artifact。")
    if domain == "database":
        additions.append("数据库相关：schema 或迁移变更必须单独确认。")
    if task_type == "research":
        additions.append("研究任务：结论需要保留来源或证据说明。")
    return list(dict.fromkeys([*constraints, *additions]))


def _confidence(
    task_type: str,
    domain: str,
    risk: str,
    complexity: str,
    recommended_pack: str,
    available_packs: set[str] | None,
) -> float:
    score = 0.55
    if task_type != "feature":
        score += 0.1
    if domain != "code":
        score += 0.1
    if risk != "low":
        score += 0.05
    if complexity in {"S", "L"}:
        score += 0.05
    if available_packs is None or recommended_pack in available_packs:
        score += 0.1
    return min(score, 0.9)


def _pack_available(pack_name: str, available_packs: set[str] | None) -> bool:
    return available_packs is None or pack_name in available_packs


def _contains_term(text: str, term: str) -> bool:
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        return term in text
    if term.replace("-", "").replace("_", "").isalnum():
        import re

        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text
