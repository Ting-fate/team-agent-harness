from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.skill_library import (
    SkillBindingWrite,
    SkillLibrary,
    SkillLibraryError,
    apply_task_skill_routes_to_agent,
    apply_auto_skill_routes_to_packs,
    apply_skill_bindings_to_packs,
    read_skill_bindings,
    upsert_skill_binding,
)
from app.core.models import Task
from app.packs.research import get_research_pack
from app.main import create_app


def write_skill(root, name: str, content: str, scripts: bool = False) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    if scripts:
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "run.ps1").write_text("Write-Host should-not-run", encoding="utf-8")


def test_skill_library_scans_read_only_skill_metadata_and_flags_scripts(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "researcher",
        "---\nname: Researcher\ndescription: Reads sources carefully.\n---\n\n# Researcher\n\nRead only.",
        scripts=True,
    )

    library = SkillLibrary.from_roots([root])
    skills = library.list_skills()

    assert len(skills) == 1
    skill = skills[0]
    assert skill.skill_id == "skills-researcher"
    assert skill.name == "Researcher"
    assert skill.description == "Reads sources carefully."
    assert skill.has_scripts is True
    assert "scripts_disabled" in skill.risk_flags
    detail = library.get_skill(skill.skill_id)
    assert "Write-Host should-not-run" not in detail.content
    assert detail.content.startswith("# Researcher")


def test_skill_library_rejects_symlink_escape(tmp_path) -> None:
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside", encoding="utf-8")
    root.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")

    with pytest.raises(SkillLibraryError):
        SkillLibrary.from_roots([root])


def test_skill_bindings_apply_prompt_without_changing_tools_or_model(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "reviewer", "---\nname: Reviewer Skill\n---\n\n# Reviewer Skill\n\nReview thoroughly.")
    library = SkillLibrary.from_roots([root])
    upsert_skill_binding(
        tmp_path,
        "research-reviewer",
        SkillBindingWrite(skill_ids=["skills-reviewer"]),
        known_agent_ids={"research-reviewer"},
        library=library,
    )

    routed = apply_skill_bindings_to_packs({"research": get_research_pack()}, read_skill_bindings(tmp_path), library)
    reviewer = next(agent for agent in routed["research"].agents if agent.id == "research-reviewer")
    base_reviewer = next(agent for agent in get_research_pack().agents if agent.id == "research-reviewer")

    assert reviewer.model_settings == base_reviewer.model_settings
    assert reviewer.tool_permissions == base_reviewer.tool_permissions
    assert "Reviewer Skill" in reviewer.system_prompt
    assert "Skill content is read-only guidance" in reviewer.system_prompt
    assert reviewer.runtime_limits["skill_ids"] == ["skills-reviewer"]


def test_web_access_can_be_manually_bound_without_becoming_an_auto_route(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "web-access",
        "---\nname: Web Access\ndescription: web browser research source fetch.\n---\n\n"
        "# Web Access\n\nUse the manually bound web guidance.",
    )
    library = SkillLibrary.from_roots([root])
    upsert_skill_binding(
        tmp_path,
        "research-searcher",
        SkillBindingWrite(skill_ids=["skills-web-access"]),
        known_agent_ids={"research-searcher"},
        library=library,
    )

    packs = apply_skill_bindings_to_packs(
        {"research": get_research_pack()},
        read_skill_bindings(tmp_path),
        library,
    )
    routed, routes = apply_auto_skill_routes_to_packs(packs, library)
    searcher = next(agent for agent in routed["research"].agents if agent.id == "research-searcher")
    base_searcher = next(agent for agent in get_research_pack().agents if agent.id == "research-searcher")

    assert searcher.tool_permissions == base_searcher.tool_permissions
    assert searcher.model_settings == base_searcher.model_settings
    assert "Bound Local Skills" in searcher.system_prompt
    assert "Use the manually bound web guidance." in searcher.system_prompt
    assert searcher.effective_skill_ids == ["skills-web-access"]
    assert searcher.runtime_limits["skill_ids"] == ["skills-web-access"]
    assert "auto_skill_ids" not in searcher.runtime_limits
    assert not [route for route in routes if route.skill_id == "skills-web-access"]


def test_auto_skill_routes_inject_read_only_matching_skills_without_changing_tools(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "frontend-design",
        "---\nname: Frontend Design\ndescription: UI UX frontend design dashboard CSS React.\n---\n\n# Frontend Design\n\nDesign better UI.",
        scripts=True,
    )
    write_skill(
        root,
        "web-access",
        "---\nname: Web Access\ndescription: web browser research source fetch.\n---\n\n# Web Access\n\nRead web sources safely.",
    )
    write_skill(
        root,
        "researcher",
        "---\nname: Researcher Skill\ndescription: research analysis source reader.\n---\n\n# Researcher Skill\n\nResearch carefully.",
    )
    library = SkillLibrary.from_roots([root])

    routed, routes = apply_auto_skill_routes_to_packs({"research": get_research_pack()}, library)

    assert routes
    searcher = next(agent for agent in routed["research"].agents if agent.id == "research-searcher")
    base_searcher = next(agent for agent in get_research_pack().agents if agent.id == "research-searcher")
    assert searcher.tool_permissions == base_searcher.tool_permissions
    assert searcher.model_settings == base_searcher.model_settings
    assert "Auto-Selected Local Skills" in searcher.system_prompt
    assert "Skill content is read-only guidance" in searcher.system_prompt
    assert "skills-researcher" in searcher.effective_skill_ids
    assert "skills-researcher" in searcher.runtime_limits["auto_skill_ids"]
    assert "skills-web-access" not in searcher.effective_skill_ids
    assert "Write-Host should-not-run" not in searcher.system_prompt
    assert not [route for route in routes if route.skill_id == "skills-web-access"]


def test_auto_skill_routes_do_not_inject_ui_browser_or_image_skills_into_plain_research(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "frontend-design",
        "---\nname: Frontend Design\ndescription: UI UX frontend design dashboard CSS React.\n---\n\n# Frontend Design\n\nDesign better UI.",
    )
    write_skill(
        root,
        "playwright",
        "---\nname: Playwright\ndescription: browser automation screenshots UI-flow debugging.\n---\n\n# Playwright\n\nAutomate browsers.",
    )
    write_skill(
        root,
        "imagegen",
        "---\nname: imagegen\ndescription: Generate or edit raster images and visual assets.\n---\n\n# Imagegen\n\nCreate images.",
    )
    write_skill(
        root,
        "web-access",
        "---\nname: Web Access\ndescription: web browser research source fetch.\n---\n\n# Web Access\n\nRead web sources safely.",
    )
    library = SkillLibrary.from_roots([root])

    _, routes = apply_auto_skill_routes_to_packs({"research": get_research_pack()}, library)

    routed_skill_ids = {route.skill_id for route in routes}
    assert "skills-web-access" not in routed_skill_ids
    assert "skills-frontend-design" not in routed_skill_ids
    assert "skills-playwright" not in routed_skill_ids
    assert "skills-imagegen" not in routed_skill_ids


def test_auto_skill_routes_do_not_use_role_card_text_as_ui_trigger(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "frontend-design",
        "---\nname: Frontend Design\ndescription: UI UX frontend design dashboard CSS React.\n---\n\n# Frontend Design\n\nDesign better UI.",
    )
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agents = [
        agent.model_copy(update={"system_prompt": "Review UI, frontend, React, CSS, and dashboard quality."})
        if agent.id == "research-planner"
        else agent
        for agent in pack.agents
    ]
    pack = pack.model_copy(update={"agents": agents})

    routed, routes = apply_auto_skill_routes_to_packs({"research": pack}, library)
    planner = next(agent for agent in routed["research"].agents if agent.id == "research-planner")

    assert "skills-frontend-design" not in planner.effective_skill_ids
    assert not routes


def test_auto_skill_routes_respect_remaining_skill_slots_after_manual_bindings(tmp_path) -> None:
    root = tmp_path / "skills"
    for index in range(4):
        write_skill(root, f"manual-{index}", f"# Manual {index}\n\nManual guidance.")
    write_skill(
        root,
        "web-access",
        "---\nname: Web Access\ndescription: web browser research source fetch.\n---\n\n# Web Access\n\nRead web sources safely.",
    )
    write_skill(
        root,
        "researcher",
        "---\nname: Researcher Skill\ndescription: research analysis source reader.\n---\n\n# Researcher Skill\n\nResearch carefully.",
    )
    library = SkillLibrary.from_roots([root])
    upsert_skill_binding(
        tmp_path,
        "research-searcher",
        SkillBindingWrite(skill_ids=[f"skills-manual-{index}" for index in range(4)]),
        known_agent_ids={"research-searcher"},
        library=library,
    )

    packs = apply_skill_bindings_to_packs({"research": get_research_pack()}, read_skill_bindings(tmp_path), library)
    routed, routes = apply_auto_skill_routes_to_packs(packs, library)
    searcher = next(agent for agent in routed["research"].agents if agent.id == "research-searcher")

    assert len(searcher.effective_skill_ids) == 5
    assert len(searcher.runtime_limits["auto_skill_ids"]) == 1
    assert len([route for route in routes if route.agent_id == "research-searcher"]) == 1


def test_auto_skill_routes_skip_candidates_that_exceed_remaining_content_budget(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "manual-0", "# Manual 0\n\n" + ("m" * 49000))
    write_skill(root, "manual-1", "# Manual 1\n\n" + ("m" * 49000))
    write_skill(
        root,
        "researcher",
        "---\nname: Researcher Skill\ndescription: research analysis source reader.\n---\n\n# Researcher Skill\n\n"
        + ("w" * 500),
    )
    library = SkillLibrary.from_roots([root])
    upsert_skill_binding(
        tmp_path,
        "research-searcher",
        SkillBindingWrite(skill_ids=["skills-manual-0", "skills-manual-1"]),
        known_agent_ids={"research-searcher"},
        library=library,
    )

    packs = apply_skill_bindings_to_packs({"research": get_research_pack()}, read_skill_bindings(tmp_path), library)
    routed, routes = apply_auto_skill_routes_to_packs(packs, library)
    searcher = next(agent for agent in routed["research"].agents if agent.id == "research-searcher")

    assert searcher.effective_skill_ids == ["skills-manual-0", "skills-manual-1"]
    assert "auto_skill_ids" not in searcher.runtime_limits
    assert not [route for route in routes if route.agent_id == "research-searcher"]


def test_task_skill_routes_select_document_skills_from_task_text_without_changing_runtime_contract(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "pdf", "---\nname: PDF Skill\n---\n\n# PDF Skill\n\nRead PDF files carefully.")
    write_skill(root, "docx", "---\nname: Word Skill\n---\n\n# Word Skill\n\nCreate Word documents.")
    write_skill(root, "xlsx", "---\nname: Excel Skill\n---\n\n# Excel Skill\n\nHandle spreadsheets.")
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agent = next(item for item in pack.agents if item.id == "research-planner")
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(
        title="读取 PDF 并写 Word 文档报告",
        goal="分析本地 PDF 资料，然后生成 docx 文档。",
        workflow_pack="research",
    )

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert [route.skill_id for route in routes] == ["skills-docx", "skills-pdf"]
    assert routed_agent.model_settings == agent.model_settings
    assert routed_agent.tool_permissions == agent.tool_permissions
    assert "Task-Selected Local Skills" in routed_agent.system_prompt
    assert "PDF Skill" in routed_agent.system_prompt
    assert "Word Skill" in routed_agent.system_prompt
    assert "Excel Skill" not in routed_agent.system_prompt
    assert routed_agent.runtime_limits["task_skill_ids"] == ["skills-docx", "skills-pdf"]
    assert routed_agent.runtime_limits["task_skill_injected_bytes"] > 0
    assert [route["skill_id"] for route in routed_agent.runtime_limits["task_skill_routes"]] == [
        "skills-docx",
        "skills-pdf",
    ]


def test_task_skill_routes_do_not_select_document_skills_for_unrelated_task(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "pdf", "---\nname: PDF Skill\n---\n\n# PDF Skill\n\nRead PDF files carefully.")
    write_skill(root, "docx", "---\nname: Word Skill\n---\n\n# Word Skill\n\nCreate Word documents.")
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agent = next(item for item in pack.agents if item.id == "research-planner")
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(title="研究多智能体协作", goal="整理架构思路。", workflow_pack="research")

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert routed_agent == agent
    assert routes == []


def test_task_skill_routes_do_not_treat_generic_report_or_acceptance_as_specialized_file_work(
    tmp_path,
) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "docx",
        "---\nname: docx\ndescription: Create Word documents and reports.\n---\n\n# Docx\n\nWrite documents.",
    )
    write_skill(
        root,
        "xlsx",
        "---\nname: xlsx\ndescription: Create spreadsheets; do not use for Word documents or HTML reports.\n---\n\n# Xlsx\n\nWrite spreadsheets.",
    )
    write_skill(
        root,
        "browser-testing-with-devtools",
        "---\nname: Browser Testing\ndescription: Test browser UI behavior.\n---\n\n# Browser Testing\n\nInspect a browser.",
    )
    write_skill(
        root,
        "idea-refine",
        "---\nname: Idea Refine\ndescription: Refine ideas and stress-test assumptions.\n---\n\n# Idea Refine\n\nShape an idea.",
    )
    write_skill(
        root,
        "interview-me",
        "---\nname: Interview Me\ndescription: Test assumptions by interviewing the user.\n---\n\n# Interview\n\nAsk questions.",
    )
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agent = next(item for item in pack.agents if item.id == "research-planner")
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(
        title="真实长线恢复验收",
        goal="基于公开官方来源产出带来源核验的简明报告。",
        acceptance_criteria=["完成六个 Research 步骤"],
        workflow_pack="research",
    )

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert routed_agent == agent
    assert routes == []


def test_task_skill_routes_require_explicit_browser_automation_signal_for_browser_testing_skill(
    tmp_path,
) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "browser-testing-with-devtools",
        "---\nname: Browser Testing\ndescription: Test browser UI behavior.\n---\n\n# Browser Testing\n\nInspect a browser.",
    )
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agent = next(item for item in pack.agents if item.id == "research-planner")
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(
        title="浏览器自动化验收",
        goal="用 Playwright 完成端到端 UI test。",
        workflow_pack="research",
    )

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert [route.skill_id for route in routes] == ["skills-browser-testing-with-devtools"]
    assert "Browser Testing" in routed_agent.system_prompt


def test_task_skill_routes_select_domain_skills_with_matching_task_signals(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "security",
        "---\nname: Security Review\ndescription: Security auth jwt token review.\n---\n\n# Security\n\nCheck auth boundaries.",
    )
    write_skill(
        root,
        "database",
        "---\nname: Database\ndescription: Database schema migration guidance.\n---\n\n# Database\n\nCheck migrations.",
    )
    write_skill(
        root,
        "frontend",
        "---\nname: Frontend\ndescription: UI frontend React CSS design guidance.\n---\n\n# Frontend\n\nCheck UI.",
    )
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agent = next(item for item in pack.agents if item.id == "research-planner")
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(
        title="给 API 加 JWT 认证",
        goal="实现 auth token 校验，更新数据库 schema，并补 pytest。",
        workflow_pack="code_rd_institutional",
    )

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert [route.skill_id for route in routes] == ["skills-database", "skills-security"]
    assert "Task-Selected Local Skills" in routed_agent.system_prompt
    assert "Security" in routed_agent.system_prompt
    assert "Database" in routed_agent.system_prompt
    assert "Frontend" not in routed_agent.system_prompt
    assert routed_agent.tool_permissions == agent.tool_permissions
    assert routed_agent.model_settings == agent.model_settings
    assert routed_agent.runtime_limits["task_skill_injected_bytes"] > 0


def test_task_skill_routes_do_not_globally_block_web_access_labeled_domain_skills(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "web-access-security",
        "---\nname: web-access-security\ndescription: Security authorization review.\n---\n\n"
        "# Web Access Security\n\nReview authorization boundaries.",
    )
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    agent = next(item for item in pack.agents if item.id == "research-planner")
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(
        title="Review security authorization",
        goal="Check the security boundary for authorization failures.",
        workflow_pack="research",
    )

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert [route.skill_id for route in routes] == ["skills-web-access-security"]
    assert "Web Access Security" in routed_agent.system_prompt


def test_task_skill_routes_skip_secret_like_and_over_budget_document_skills(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "manual", "# Manual\n\n" + ("m" * 50000))
    write_skill(root, "pdf", "# PDF\n\n" + ("p" * 60000))
    write_skill(
        root,
        "docx",
        "---\nname: docx\ndescription: OPENAI_API_KEY=sk-secret-value\n---\n\n# Docx\n\nWrite docs.",
    )
    library = SkillLibrary.from_roots([root])
    pack = get_research_pack()
    base_agent = next(item for item in pack.agents if item.id == "research-planner")
    agent = base_agent.model_copy(update={"effective_skill_ids": ["skills-manual"]})
    step = next(item for item in pack.steps if item.name == "plan_research")
    task = Task(title="读取 PDF 并写 Word 文档报告", goal="处理 pdf 和 docx。", workflow_pack="research")

    routed_agent, routes = apply_task_skill_routes_to_agent(agent, task=task, step=step, library=library)

    assert routed_agent == agent
    assert routes == []
    assert "sk-secret-value" not in routed_agent.system_prompt


def test_skill_api_lists_details_and_bindings_without_leaking_large_content(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    write_skill(skill_root, "planner", "---\nname: Planner Skill\n---\n\n# Planner Skill\n\nPlan carefully.")
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        config_root=tmp_path,
        skill_roots_override=[skill_root],
    )

    with TestClient(app) as client:
        skills = client.get("/skills").json()
        assert skills[0]["skill_id"] == "skills-planner"
        assert "content" not in skills[0]

        detail = client.get("/skills/skills-planner").json()
        assert detail["content"].startswith("# Planner Skill")

        response = client.put(
            "/skill-bindings/research-planner",
            json={"skill_ids": ["skills-planner"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["restart_required"] is True
        assert client.get("/skill-bindings").json()[0]["skill_ids"] == ["skills-planner"]

    restarted_app = create_app(
        tmp_path / "harness-restarted.sqlite3",
        tmp_path / "artifacts-2",
        config_root=tmp_path,
        skill_roots_override=[skill_root],
    )
    with TestClient(restarted_app) as client:
        agents = client.get("/agents", params={"pack_name": "research"}).json()
        planner = next(agent for agent in agents if agent["id"] == "research-planner")
        assert "skills-planner" in planner["effective_skill_ids"]
        assert planner["prompt_redacted"] is True
        assert "Skill guidance is attached at runtime" in planner["system_prompt"]
        assert "Planner Skill" not in planner["system_prompt"]
        assert "Plan carefully" not in planner["system_prompt"]

        research_pack = client.get("/workflow-packs/research").json()
        planner_from_pack = next(agent for agent in research_pack["agents"] if agent["id"] == "research-planner")
        assert "skills-planner" in planner_from_pack["effective_skill_ids"]
        assert planner_from_pack["prompt_redacted"] is True
        assert "Planner Skill" not in planner_from_pack["system_prompt"]
        assert "Plan carefully" not in planner_from_pack["system_prompt"]


def test_skill_auto_routes_api_exposes_reasons_without_manual_binding(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    write_skill(
        skill_root,
        "researcher",
        "---\nname: Researcher Skill\ndescription: research analysis source reader.\n---\n\n# Researcher Skill\n\nResearch carefully.",
    )
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        config_root=tmp_path,
        skill_roots_override=[skill_root],
    )

    with TestClient(app) as client:
        routes = client.get("/skill-auto-routes").json()
        bindings = client.get("/skill-bindings").json()

    assert bindings == []
    assert routes
    assert any(route["skill_id"] == "skills-researcher" for route in routes)
    assert all(route["reason"] for route in routes)



def test_skill_library_omits_secret_like_frontmatter_from_api_and_prompt(tmp_path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "leaky",
        "---\nname: OPENAI_API_KEY=sk-secret-value\ndescription: bearer sk-secret-value\n---\n\n# Leaky\n\nDo work.",
    )
    library = SkillLibrary.from_roots([root])
    detail = library.get_skill("skills-leaky")

    assert detail.name == "leaky"
    assert detail.description == ""
    assert "secret_like_content_omitted" in detail.risk_flags
    assert "sk-secret-value" not in json.dumps(detail.model_dump())

    upsert_skill_binding(
        tmp_path,
        "research-reviewer",
        SkillBindingWrite(skill_ids=["skills-leaky"]),
        known_agent_ids={"research-reviewer"},
        library=library,
    )
    routed = apply_skill_bindings_to_packs({"research": get_research_pack()}, read_skill_bindings(tmp_path), library)
    reviewer = next(agent for agent in routed["research"].agents if agent.id == "research-reviewer")

    assert "sk-secret-value" not in reviewer.system_prompt
    assert "Skill content omitted" in reviewer.system_prompt


def test_skill_binding_rejects_too_many_or_too_large_bound_skills(tmp_path) -> None:
    root = tmp_path / "skills"
    for index in range(6):
        write_skill(root, f"skill-{index}", f"# Skill {index}\n\nUse this skill.")
    library = SkillLibrary.from_roots([root])

    with pytest.raises(SkillLibraryError, match="at most"):
        upsert_skill_binding(
            tmp_path,
            "research-reviewer",
            SkillBindingWrite(skill_ids=[f"skills-skill-{index}" for index in range(6)]),
            known_agent_ids={"research-reviewer"},
            library=library,
        )

    large_root = tmp_path / "large-skills"
    for index in range(2):
        write_skill(large_root, f"large-{index}", "# Large\n\n" + ("x" * 60000))
    large_library = SkillLibrary.from_roots([large_root])

    with pytest.raises(SkillLibraryError, match="exceeds"):
        upsert_skill_binding(
            tmp_path,
            "research-reviewer",
            SkillBindingWrite(skill_ids=["large-skills-large-0", "large-skills-large-1"]),
            known_agent_ids={"research-reviewer"},
            library=large_library,
        )


def test_skill_binding_rejects_unknown_agent_and_unknown_skill(tmp_path) -> None:
    library = SkillLibrary.from_roots([])

    with pytest.raises(SkillLibraryError):
        upsert_skill_binding(
            tmp_path,
            "missing-agent",
            SkillBindingWrite(skill_ids=[]),
            known_agent_ids={"research-planner"},
            library=library,
        )

    with pytest.raises(SkillLibraryError):
        upsert_skill_binding(
            tmp_path,
            "research-planner",
            SkillBindingWrite(skill_ids=["missing-skill"]),
            known_agent_ids={"research-planner"},
            library=library,
        )
