"""
Skill Loader — generates OpenAI function-calling tool definitions from SKILL.md files.

Each skill's SKILL.md contains YAML frontmatter with:
  - name: skill directory name (hyphenated)
  - tool_name: function name for the LLM (underscored)
  - description: used as the function description
  - parameters: OpenAI-format JSON Schema for the function parameters

Skills that include a `parameters` block in frontmatter get their tool
definition generated here.  Skills without `parameters` are documentation-only
and must still define their tool definition inline in tools.py.

Usage:
    from nova.skill_loader import load_skill_tool_definitions
    defs = load_skill_tool_definitions()   # list of OpenAI tool dicts
"""

import yaml
from pathlib import Path
from loguru import logger

SKILLS_DIR = Path(__file__).parent.parent / "skills"

SKILL_INTENT_BINDINGS = {
    "workspace_creation": "workspace-manager",
    "lookup_then_workspace_creation": "workspace-manager",
    "workspace_context_continuation": "workspace-manager",
    "workspace_creation_continuation": "workspace-manager",
    "task_artifact_continuation": "workspace-manager",
    "email_lookup": "email-navigator",
    "conversation_recall": "conversation-search",
    "personal_memory_recall": "pcg-recall-memory",
    "current_events_lookup": "web-search",
    "weather_lookup": "",
    "workflow_trigger": "",
    "workflow_status": "",
}


def _parse_frontmatter(path: Path) -> dict | None:
    """Extract YAML frontmatter from a SKILL.md file."""
    try:
        text = path.read_text()
    except Exception as e:
        logger.warning(f"Could not read {path}: {e}")
        return None

    if not text.startswith("---"):
        return None

    end = text.find("---", 3)
    if end == -1:
        return None

    try:
        return yaml.safe_load(text[3:end])
    except yaml.YAMLError as e:
        logger.warning(f"YAML parse error in {path}: {e}")
        return None


def _read_skill_markdown(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text()
    except Exception as e:
        logger.warning(f"Could not read {path}: {e}")
        return {}, ""
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text
    try:
        frontmatter = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as e:
        logger.warning(f"YAML parse error in {path}: {e}")
        frontmatter = {}
    return frontmatter, text[end + 3:].strip()


def _safe_relative_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Invalid skill resource path")
    return candidate


def list_skills(include_body: bool = False) -> list[dict]:
    skills: list[dict] = []
    if not SKILLS_DIR.is_dir():
        logger.warning(f"Skills directory not found: {SKILLS_DIR}")
        return skills
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        frontmatter, body = _read_skill_markdown(skill_md)
        resource_counts = {"references": 0, "templates": 0, "scripts": 0, "resources": 0}
        for name in resource_counts:
            folder = skill_dir / name
            if folder.is_dir():
                resource_counts[name] = sum(1 for item in folder.rglob("*") if item.is_file())
        item = {
            "name": frontmatter.get("name") or skill_dir.name,
            "directory": skill_dir.name,
            "description": str(frontmatter.get("description") or "").strip(),
            "tool_name": frontmatter.get("tool_name") or "",
            "has_parameters": bool(frontmatter.get("parameters")),
            "frontmatter": frontmatter,
            "resource_counts": resource_counts,
            "read_only": True,
        }
        if include_body:
            item["body"] = body
        skills.append(item)
    return skills


def get_skill(skill_name: str, include_body: bool = True) -> dict | None:
    clean_name = _safe_relative_path(skill_name).parts[0] if skill_name else ""
    for skill in list_skills(include_body=include_body):
        if skill.get("directory") == clean_name or skill.get("name") == clean_name:
            return skill
    return None


def get_skill_bindings() -> dict[str, dict]:
    bindings: dict[str, dict] = {}
    for intent, skill_name in SKILL_INTENT_BINDINGS.items():
        if not skill_name:
            bindings[intent] = {"intent": intent, "skill": None, "bound": False}
            continue
        skill = get_skill(skill_name, include_body=False)
        bindings[intent] = {
            "intent": intent,
            "skill": skill_name,
            "bound": bool(skill),
            "description": (skill or {}).get("description", ""),
            "tool_name": (skill or {}).get("tool_name", ""),
            "resource_counts": (skill or {}).get("resource_counts", {}),
        }
    return bindings


def get_skill_binding_for_intent(intent: str) -> dict | None:
    binding = get_skill_bindings().get(str(intent or ""))
    if not binding or not binding.get("bound"):
        return None
    return binding


def list_skill_resources(skill_name: str) -> dict | None:
    clean_name = _safe_relative_path(skill_name).parts[0] if skill_name else ""
    skill_dir = SKILLS_DIR / clean_name
    if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
        return None
    resources = []
    for folder_name in ("references", "templates", "scripts", "resources"):
        folder = skill_dir / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                resources.append({
                    "path": str(path.relative_to(skill_dir)),
                    "kind": folder_name,
                    "size_bytes": path.stat().st_size,
                })
    return {"skill": clean_name, "resources": resources, "total": len(resources)}


def read_skill_resource(skill_name: str, resource_path: str, max_chars: int = 20000) -> dict | None:
    clean_name = _safe_relative_path(skill_name).parts[0] if skill_name else ""
    relative = _safe_relative_path(resource_path)
    skill_dir = SKILLS_DIR / clean_name
    target = (skill_dir / relative).resolve()
    root = skill_dir.resolve()
    if not skill_dir.is_dir() or root not in target.parents:
        return None
    allowed_roots = {"references", "templates", "scripts", "resources"}
    if not relative.parts or relative.parts[0] not in allowed_roots:
        raise ValueError("Skill resource must be under references, templates, scripts, or resources")
    if not target.is_file():
        return None
    text = target.read_text(errors="replace")
    return {
        "skill": clean_name,
        "path": str(relative),
        "size_bytes": target.stat().st_size,
        "content": text[:max(1, min(int(max_chars), 50000))],
        "truncated": len(text) > max(1, min(int(max_chars), 50000)),
    }


def build_skill_index() -> str:
    """Return a compact index of all skills — one line per skill.

    Format:
        - <tool_name|name>: <description (≤100 chars)>

    Used in every system prompt so the LLM knows what capabilities exist
    without paying the full per-skill body token cost.
    """
    lines: list[str] = []
    if not SKILLS_DIR.is_dir():
        return ""
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = _parse_frontmatter(skill_md)
        if not fm:
            continue
        label = fm.get("tool_name") or fm.get("name") or skill_dir.name
        desc = str(fm.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 100:
            desc = desc[:97] + "..."
        lines.append(f"- {label}: {desc}")
    return "\n".join(lines)


def load_skill_bodies_for_tools(tool_names: list[str]) -> str:
    """Return the full markdown body for every skill whose tool_name is in
    *tool_names*.  Used to lazily inject behavioral protocol only for tools
    that are actually available in the current turn.

    Returns a combined markdown string ready to append to the system prompt,
    or an empty string if nothing matched.
    """
    if not tool_names or not SKILLS_DIR.is_dir():
        return ""
    name_set = set(tool_names)
    sections: list[str] = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        fm, body = _read_skill_markdown(skill_md)
        if not fm:
            continue
        tool_name = fm.get("tool_name") or ""
        skill_name = fm.get("name") or skill_dir.name
        # Include if tool is active OR if skill is doc-only (no tool_name)
        is_active_tool = tool_name and tool_name in name_set
        is_doc_skill = not tool_name and not fm.get("parameters")
        if not is_active_tool and not is_doc_skill:
            continue
        if body:
            sections.append(f"### Skill: {skill_name}\n{body}")
    return "\n\n".join(sections)


def load_skill_tool_definitions() -> list[dict]:
    """Scan skills/ for SKILL.md files with parameter schemas and return
    OpenAI function-calling tool definitions."""
    definitions: list[dict] = []

    if not SKILLS_DIR.is_dir():
        logger.warning(f"Skills directory not found: {SKILLS_DIR}")
        return definitions

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        fm = _parse_frontmatter(skill_md)
        if not fm:
            continue

        tool_name = fm.get("tool_name")
        description = fm.get("description", "").strip()
        parameters = fm.get("parameters")

        if not tool_name or not parameters:
            continue

        definitions.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": parameters,
            },
        })
        logger.debug(f"Loaded tool definition from skill: {skill_dir.name} → {tool_name}")

    logger.info(f"Loaded {len(definitions)} tool definitions from skills/")
    return definitions
