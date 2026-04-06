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
