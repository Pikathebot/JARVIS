import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # Fallback to simple parser if yaml package is not installed

logger = logging.getLogger("jarvis.skills.loader")


def trigger_in(trigger: str, text_lower: str) -> bool:
    """A trigger phrase in lower-cased text as whole words: "uat" is not in "evaluating", "flashcard"
    is in "flashcards". Spaces in the trigger match any run of whitespace."""
    words = (trigger or "").lower().split()
    if not words:
        return False
    body = r"\s+".join(map(re.escape, words))
    return re.search(r"(?<![\w])" + body + r"(?:s|es)?(?![\w])", text_lower) is not None


@dataclass
class SkillMetadata:
    """
    Lightweight metadata container for progressive skill discovery (Stage 1).
    Does NOT hold full instruction bodies in memory to prevent prompt and RAM bloat.
    """
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    category: str = "general"
    file_path: str = ""


@dataclass
class Skill:
    """
    Full skill definition containing loaded prompt instructions.
    """
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    category: str = "general"
    instructions: str = ""
    file_path: str = ""


class SkillsLoader:
    """
    Progressive Disclosure Skills Loader for Project Nexus (Build Plan Section 13).
    Implements 3-stage progressive loading:
      Stage 1: discover_skills() -> list[SkillMetadata] (frontmatter only)
      Stage 2: determine_relevance(query) -> list[str] (evaluates triggers & semantics)
      Stage 3: load_skill_instructions(skill_ids) -> str (lazy body loading & prompt assembly)
    """

    def __init__(
        self,
        skills_dir: Optional[str] = None,
        workspace_skills_dir: Optional[str] = None,
    ):
        backend_dir = Path(__file__).resolve().parent.parent.parent
        root_dir = backend_dir.parent

        if skills_dir:
            self.skills_dir = Path(skills_dir)
        else:
            self.skills_dir = root_dir / "skills"

        if workspace_skills_dir:
            self.workspace_skills_dir = Path(workspace_skills_dir)
        else:
            self.workspace_skills_dir = root_dir / "workspace" / "skills"

        self._metadata_cache: dict[str, SkillMetadata] = {}
        self._skills_cache: dict[str, Skill] = {}
        self.discover_skills()

    def _parse_frontmatter_only(self, file_path: Path) -> Optional[SkillMetadata]:
        """
        Reads only frontmatter from a markdown file without storing instructions body.
        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.debug("Failed to read skill file '%s': %s", file_path, e)
            return None

        frontmatter_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not frontmatter_match:
            return None

        frontmatter_raw = frontmatter_match.group(1)
        meta: dict[str, Any] = {}

        if yaml:
            try:
                meta = yaml.safe_load(frontmatter_raw) or {}
            except Exception:
                meta = {}
        else:
            lines = frontmatter_raw.splitlines()
            current_list_key = None
            for line in lines:
                line_stripped = line.strip()
                if line_stripped.startswith("- ") and current_list_key:
                    meta.setdefault(current_list_key, []).append(line_stripped[2:].strip())
                elif ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    if v:
                        meta[k] = v
                        current_list_key = None
                    else:
                        meta[k] = []
                        current_list_key = k

        name = meta.get("name") or file_path.stem
        description = meta.get("description", "")
        triggers = meta.get("triggers", [])
        tools = meta.get("tools") or meta.get("allowed_tools", [])

        # Infer category from parent directory if in subfolder
        category = meta.get("category")
        if not category:
            parent_name = file_path.parent.name
            category = parent_name if parent_name not in ("skills", "workspace") else "general"

        return SkillMetadata(
            name=name,
            description=description,
            triggers=triggers if isinstance(triggers, list) else [str(triggers)],
            tools=tools if isinstance(tools, list) else [str(tools)],
            category=category,
            file_path=str(file_path),
        )

    def _scan_skill_paths(self) -> list[Path]:
        """Find all markdown skill files in skills_dir and workspace_skills_dir."""
        found_paths: list[Path] = []
        directories_to_scan = [self.skills_dir, self.workspace_skills_dir]

        for base_dir in directories_to_scan:
            if base_dir and base_dir.exists() and base_dir.is_dir():
                for p in base_dir.rglob("*.md"):
                    if p.is_file():
                        found_paths.append(p)
        return found_paths

    # -------------------------------------------------------------
    # Stage 1: discover_skills()
    # -------------------------------------------------------------
    def discover_skills(self) -> list[SkillMetadata]:
        """
        Stage 1: Scan skills directories and parse ONLY metadata/frontmatter.
        Does not load full instruction bodies into memory.
        """
        self._metadata_cache.clear()
        skill_files = self._scan_skill_paths()

        for file_path in skill_files:
            meta = self._parse_frontmatter_only(file_path)
            if meta:
                # Workspace skills override default root skills if duplicate name
                self._metadata_cache[meta.name] = meta

        return list(self._metadata_cache.values())

    # -------------------------------------------------------------
    # Stage 2: determine_relevance()
    # -------------------------------------------------------------
    def determine_relevance(
        self,
        query: str,
        available_skills: Optional[list[SkillMetadata]] = None,
    ) -> list[str]:
        """
        Stage 2: Evaluates user query against metadata triggers and descriptions.
        Returns a list of relevant skill IDs / names.
        """
        skills = available_skills if available_skills is not None else list(self._metadata_cache.values())
        if not skills:
            skills = self.discover_skills()

        matched_ids: list[str] = []
        query_lower = query.lower().strip()

        for s in skills:
            # 1. Check exact trigger match
            matched = False
            for trig in s.triggers:
                trig_clean = trig.lower().strip()
                if trig_clean and trigger_in(trig_clean, query_lower):
                    matched = True
                    break

            # 2. Check skill name match
            if not matched and s.name.lower().replace("_", " ") in query_lower:
                matched = True

            # 3. Check keywords in description if query mentions key capability
            if not matched and len(query_lower.split()) > 3:
                desc_words = set(s.description.lower().split())
                q_words = set(query_lower.split())
                if len(desc_words.intersection(q_words)) >= 3:
                    matched = True

            if matched:
                matched_ids.append(s.name)

        return matched_ids

    # -------------------------------------------------------------
    # Stage 3: load_skill_instructions()
    # -------------------------------------------------------------
    def load_skill_instructions(self, skill_ids: list[str]) -> str:
        """
        Stage 3: Lazily reads full instruction bodies from disk ONLY for the
        relevant skill IDs, and builds the formatted system prompt injection.
        """
        if not skill_ids:
            return ""

        sections: list[str] = []

        for s_id in skill_ids:
            meta = self._metadata_cache.get(s_id)
            if not meta and s_id in self._skills_cache:
                s = self._skills_cache[s_id]
                sections.append(f"### Skill Active: {s.name}\n{s.instructions}")
                continue

            if not meta or not meta.file_path:
                continue

            try:
                content = Path(meta.file_path).read_text(encoding="utf-8")
                body_match = re.match(r"^---\s*\n.*?\n---\s*\n(.*)$", content, re.DOTALL)
                instructions = body_match.group(1).strip() if body_match else content.strip()
                sections.append(f"### Skill Active: {meta.name}\n{instructions}")
            except Exception as e:
                logger.warning("Could not read instructions for skill '%s': %s", s_id, e)

        if not sections:
            return ""

        return "\n\n" + "\n\n".join(sections)

    # -------------------------------------------------------------
    # Backward Compatibility Methods for Phase 1-4 callers and tests
    # -------------------------------------------------------------
    def _parse_skill_file(self, file_path: Path) -> Optional[Skill]:
        """Parse full skill file (used for legacy load_skills)."""
        content = file_path.read_text(encoding="utf-8")
        frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
        if not frontmatter_match:
            return None

        frontmatter_raw = frontmatter_match.group(1)
        instructions = frontmatter_match.group(2).strip()

        meta = self._parse_frontmatter_only(file_path)
        if not meta:
            return None

        return Skill(
            name=meta.name,
            description=meta.description,
            triggers=meta.triggers,
            tools=meta.tools,
            category=meta.category,
            instructions=instructions,
            file_path=str(file_path),
        )

    def load_skills(self) -> dict[str, Skill]:
        """Full skill load for legacy callers."""
        self._skills_cache.clear()
        skill_files = self._scan_skill_paths()

        for file_path in skill_files:
            try:
                skill = self._parse_skill_file(file_path)
                if skill:
                    self._skills_cache[skill.name] = skill
            except Exception:
                pass

        return self._skills_cache

    def list_skills(self) -> list[dict[str, Any]]:
        if not self._skills_cache:
            self.load_skills()
        return [
            {
                "name": s.name,
                "description": s.description,
                "triggers": s.triggers,
                "tools": s.tools,
                "category": s.category,
                "file_path": s.file_path,
            }
            for s in self._skills_cache.values()
        ]

    def match_skills(self, user_message: str) -> list[Skill]:
        """The skills whose trigger phrase is in the message, as whole words (a plural 's' is allowed).

        The orchestrator calls this every turn. It was a plain substring test, so the Unreal skill's
        "uat" fired on "evaluating" and "ubt" on "doubt" (scripts/check_turn_routing.py, 2026-11-05)."""
        if not self._skills_cache:
            self.load_skills()

        matched: list[Skill] = []
        msg_lower = (user_message or "").lower()

        for skill in self._skills_cache.values():
            for trigger in skill.triggers:
                if trigger_in(trigger, msg_lower):
                    matched.append(skill)
                    break

        return matched

    def build_skill_prompt_injection(self, active_skills: list[Skill]) -> str:
        """Legacy prompt assembly."""
        if not active_skills:
            return ""

        sections = []
        for s in active_skills:
            sections.append(f"### Skill Active: {s.name}\n{s.instructions}")

        return "\n\n" + "\n\n".join(sections)
