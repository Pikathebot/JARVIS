import pytest
from pathlib import Path
from app.skills.loader import SkillsLoader, SkillMetadata, Skill


@pytest.fixture
def mock_skills_environment(tmp_path):
    """
    Creates an isolated mock skills directory hierarchy inside tmp_path.
    Never writes to root workspace/ or skills/.
    """
    root_skills = tmp_path / "skills"
    workspace_skills = tmp_path / "workspace" / "skills"

    root_skills.mkdir(parents=True, exist_ok=True)
    workspace_skills.mkdir(parents=True, exist_ok=True)

    # 1. Root skill: code_review in category 'development'
    dev_dir = root_skills / "development"
    dev_dir.mkdir(parents=True, exist_ok=True)
    (dev_dir / "skill.md").write_text(
        "---\n"
        "name: code_review\n"
        "description: Code quality and architectural review\n"
        "triggers:\n"
        "  - review code\n"
        "  - inspect code\n"
        "  - code review\n"
        "tools:\n"
        "  - read_file\n"
        "category: development\n"
        "---\n"
        "## Code Review Body Instructions\n"
        "Check static types, safety, and clean code principles.",
        encoding="utf-8"
    )

    # 2. Root skill: system_diagnostics
    (root_skills / "system_diagnostics.md").write_text(
        "---\n"
        "name: system_diagnostics\n"
        "description: Telemetry and hardware monitoring\n"
        "triggers:\n"
        "  - system stats\n"
        "  - check disk\n"
        "tools:\n"
        "  - get_disk_usage\n"
        "---\n"
        "## System Diagnostics Body Instructions\n"
        "Report system health with units.",
        encoding="utf-8"
    )

    # 3. Workspace skill: deployment in category 'devops'
    devops_dir = workspace_skills / "devops"
    devops_dir.mkdir(parents=True, exist_ok=True)
    (devops_dir / "skill.md").write_text(
        "---\n"
        "name: docker_deploy\n"
        "description: Docker container deployment workflow\n"
        "triggers:\n"
        "  - deploy container\n"
        "  - docker build\n"
        "tools:\n"
        "  - terminal_execute\n"
        "category: devops\n"
        "---\n"
        "## Deployment Body Instructions\n"
        "Run docker compose up -d.",
        encoding="utf-8"
    )

    return root_skills, workspace_skills


def test_stage1_discover_skills_metadata_only(mock_skills_environment):
    """Test Stage 1: discover_skills parses only frontmatter and metadata."""
    root_skills, workspace_skills = mock_skills_environment
    loader = SkillsLoader(
        skills_dir=str(root_skills),
        workspace_skills_dir=str(workspace_skills),
    )

    discovered = loader.discover_skills()
    assert len(discovered) == 3

    meta_names = [m.name for m in discovered]
    assert "code_review" in meta_names
    assert "system_diagnostics" in meta_names
    assert "docker_deploy" in meta_names

    cr_meta = next(m for m in discovered if m.name == "code_review")
    assert cr_meta.category == "development"
    assert "review code" in cr_meta.triggers
    assert "read_file" in cr_meta.tools
    assert not hasattr(cr_meta, "instructions")  # Metadata only


def test_stage2_determine_relevance(mock_skills_environment):
    """Test Stage 2: determine_relevance identifies matching skills from query."""
    root_skills, workspace_skills = mock_skills_environment
    loader = SkillsLoader(
        skills_dir=str(root_skills),
        workspace_skills_dir=str(workspace_skills),
    )

    # 1. Query matching code_review
    matches_cr = loader.determine_relevance("Please review code for security issues.")
    assert "code_review" in matches_cr

    # 2. Query matching docker_deploy
    matches_deploy = loader.determine_relevance("Let's do a docker build and deploy container.")
    assert "docker_deploy" in matches_deploy

    # 3. Unrelated query
    matches_none = loader.determine_relevance("What is the capital of France?")
    assert len(matches_none) == 0


def test_stage3_load_skill_instructions_lazy(mock_skills_environment):
    """Test Stage 3: load_skill_instructions lazily reads instruction body only for relevant skills."""
    root_skills, workspace_skills = mock_skills_environment
    loader = SkillsLoader(
        skills_dir=str(root_skills),
        workspace_skills_dir=str(workspace_skills),
    )

    # Load instructions ONLY for code_review
    prompt_injection = loader.load_skill_instructions(["code_review"])
    assert "### Skill Active: code_review" in prompt_injection
    assert "Check static types, safety, and clean code principles." in prompt_injection

    # Unrequested skills must NOT be in the injection
    assert "System Diagnostics Body Instructions" not in prompt_injection
    assert "Deployment Body Instructions" not in prompt_injection


def test_workspace_skill_precedence_override(tmp_path):
    """Test that workspace skills override root skills with identical name."""
    root_skills = tmp_path / "skills"
    workspace_skills = tmp_path / "workspace" / "skills"
    root_skills.mkdir(parents=True, exist_ok=True)
    workspace_skills.mkdir(parents=True, exist_ok=True)

    # Root version
    (root_skills / "custom.md").write_text(
        "---\nname: custom_skill\ndescription: Base version\ntriggers:\n  - trigger_base\n---\nBase instructions.",
        encoding="utf-8"
    )

    # Workspace version (overrides)
    (workspace_skills / "custom.md").write_text(
        "---\nname: custom_skill\ndescription: Workspace override version\ntriggers:\n  - trigger_override\n---\nOverride instructions.",
        encoding="utf-8"
    )

    loader = SkillsLoader(skills_dir=str(root_skills), workspace_skills_dir=str(workspace_skills))
    discovered = loader.discover_skills()
    custom_meta = next(m for m in discovered if m.name == "custom_skill")

    assert custom_meta.description == "Workspace override version"
    assert "trigger_override" in custom_meta.triggers

    injection = loader.load_skill_instructions(["custom_skill"])
    assert "Override instructions." in injection


def test_skills_loader_legacy_backward_compatibility(mock_skills_environment):
    """Test that legacy methods (load_skills, match_skills, build_skill_prompt_injection) remain operational."""
    root_skills, workspace_skills = mock_skills_environment
    loader = SkillsLoader(
        skills_dir=str(root_skills),
        workspace_skills_dir=str(workspace_skills),
    )

    skills_dict = loader.load_skills()
    assert "code_review" in skills_dict
    assert isinstance(skills_dict["code_review"], Skill)

    matched = loader.match_skills("Inspect code and review code please")
    assert len(matched) >= 1
    assert matched[0].name == "code_review"

    injection = loader.build_skill_prompt_injection(matched)
    assert "Skill Active: code_review" in injection


def test_the_shipped_study_skills_load_on_their_own_words():
    loader = SkillsLoader()   # the repo's skills/ folder
    cases = {"give me a hint for this leetcode problem": "practice_help", "find papers on arxiv about diffusion": "paper_search",
             "quiz me on my os notes": "flashcards", "start a pomodoro": "focus_timer"}
    for query, skill in cases.items():
        assert skill in loader.determine_relevance(query), query
        assert skill in [s.name for s in loader.match_skills(query)], query     # what the orchestrator runs
        body = loader.load_skill_instructions([skill])
        assert body and "instructions" in body.lower()
    for everyday in ("what's the weather tomorrow", "I revised the plan", "the paper on my desk", "a contested decision"):
        assert loader.match_skills(everyday) == [], everyday
