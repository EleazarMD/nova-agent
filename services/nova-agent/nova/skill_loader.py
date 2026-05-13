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


# ---------------------------------------------------------------------------
# Skill body tiering (Phase 1a context optimization)
# ---------------------------------------------------------------------------
# Without tiering, every active tool's full SKILL.md is inlined into the
# system prompt every turn. workspace-manager alone is ~27KB / ~6.8K tokens.
# Empirically the model only needs the *triggers, routing rules, and a handful
# of canonical examples* — the full Failure Recovery / Technical Details /
# verbose Examples sections rarely change behavior but cost thousands of tokens
# per turn.
#
# Strategy (all overridable via env vars for rollback):
#   - Small skill body  (≤ NOVA_SKILL_INLINE_FULL_CHARS, default 2000):
#         inline verbatim.
#   - Medium / large body:
#         inline a compacted "essentials" view (description + When to Invoke /
#         Triggers + first one or two Examples + Routing rules), capped at
#         NOVA_SKILL_ESSENTIALS_MAX_CHARS (default 1800).
#   - Doc-only skill (no tool_name, no parameters):
#         SKIPPED by default. Set `always_inline: true` in the frontmatter to
#         keep one in the prompt, or call `discover_skills(name=...)` on demand.
#
# Hard kill switch: NOVA_SKILL_TIERING=0 reverts to the legacy "full body always".

import os
import re

_TIERING_ENABLED = os.environ.get("NOVA_SKILL_TIERING", "1").lower() not in {"0", "false", "no"}
_INLINE_FULL_CHARS = int(os.environ.get("NOVA_SKILL_INLINE_FULL_CHARS", "2000"))
_ESSENTIALS_MAX_CHARS = int(os.environ.get("NOVA_SKILL_ESSENTIALS_MAX_CHARS", "1800"))
_INCLUDE_DOC_SKILLS = os.environ.get("NOVA_SKILL_INCLUDE_DOC_ONLY", "0").lower() in {"1", "true", "yes"}

# Section headers we consider "essential" — keep these verbatim (within budget).
# Ordered from highest to lowest priority.
_ESSENTIAL_HEADERS = (
    "When to Invoke",
    "When to Use",
    "Triggers",
    "Routing",
    "Instructions",
    "Critical Rules",
    "Session Protocol",
    "Decision rule",
    "Anti-retry rules",
    "Examples",
    "Categories",
    "Tools",
    "Actions",
)

# Headers we explicitly drop in essentials mode — they're useful for human
# documentation but don't change LLM behavior.
_NOISE_HEADERS = (
    "Technical Details",
    "References",
    "Architecture",
    "Failure Recovery",
    "Failure Recovery Examples",
    "Error Handling",
    "Search Tips",
    "Status",
    "Features",
    "Caching Strategy",
    "Source Tracking",
    "Templates",
    "Advanced Canvas Page Composition",
    "Nova Authorship Metadata",
    "Response Format",
    "Model Selection",
    "Preference Categories",
    "Read Operations",
    "Write Operations",
)


def _split_markdown_sections(body: str) -> list[tuple[str, str]]:
    """Split a markdown body into (heading, content) tuples.

    Top-level sections are detected by ``^## `` or ``^### ``. Content before
    the first heading is returned with heading == "" (the lead paragraph).
    """
    parts: list[tuple[str, str]] = []
    current_heading = ""
    current_buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^(#{2,3}) +(.+?)\s*$", line)
        if m:
            if current_buf or current_heading:
                parts.append((current_heading, "\n".join(current_buf).strip()))
            current_heading = m.group(2).strip()
            current_buf = []
        else:
            current_buf.append(line)
    if current_buf or current_heading:
        parts.append((current_heading, "\n".join(current_buf).strip()))
    return parts


def _extract_skill_essentials(body: str, max_chars: int = _ESSENTIALS_MAX_CHARS) -> str:
    """Produce a compact "essentials" view of a skill body.

    Keeps the lead paragraph and any sections whose heading matches
    ``_ESSENTIAL_HEADERS``. Drops sections whose heading matches
    ``_NOISE_HEADERS``. Truncates examples sections aggressively.
    Caller is responsible for the surrounding ``### Skill: <name>`` wrapper.
    """
    if not body:
        return ""
    sections = _split_markdown_sections(body)

    def is_noise(heading: str) -> bool:
        h = heading.lower()
        return any(noise.lower() in h for noise in _NOISE_HEADERS)

    def is_essential(heading: str) -> bool:
        h = heading.lower()
        return any(ess.lower() in h for ess in _ESSENTIAL_HEADERS)

    out: list[str] = []
    used = 0

    # Always keep the lead paragraph (before any heading) if present.
    for heading, content in sections:
        if heading:
            continue
        if not content.strip():
            continue
        snippet = content.strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rstrip() + "…"
        out.append(snippet)
        used += len(snippet)
        break

    # Pass 1: essential headers in priority order.
    seen: set[str] = set()
    for priority in _ESSENTIAL_HEADERS:
        if used >= max_chars:
            break
        for heading, content in sections:
            if not heading or heading in seen:
                continue
            if priority.lower() not in heading.lower():
                continue
            if is_noise(heading):
                continue
            seen.add(heading)
            body_text = content.strip()
            if not body_text:
                continue
            # Examples sections: keep only the first ~400 chars to avoid
            # multi-screen example tables eating the budget.
            if "example" in heading.lower() and len(body_text) > 400:
                body_text = body_text[:400].rstrip() + "\n…"
            budget = max_chars - used - len(heading) - 8
            if budget <= 80:
                break
            if len(body_text) > budget:
                body_text = body_text[:budget].rstrip() + "\n…"
            section = f"### {heading}\n{body_text}"
            out.append(section)
            used += len(section) + 2

    return "\n\n".join(out).strip()


def load_skill_bodies_for_tools(tool_names: list[str]) -> str:
    """Return inlinable skill documentation for the active tools this turn.

    Tiering (Phase 1a):
      - tool body ≤ ``NOVA_SKILL_INLINE_FULL_CHARS`` → full body
      - tool body > threshold                       → essentials only
      - doc-only skill (no tool_name)               → SKIPPED unless
                                                       ``always_inline: true``
                                                       in frontmatter, or
                                                       NOVA_SKILL_INCLUDE_DOC_ONLY=1

    The legacy behaviour (full bodies, all doc skills) is restored by setting
    ``NOVA_SKILL_TIERING=0``.
    """
    if not SKILLS_DIR.is_dir():
        return ""
    name_set = set(tool_names or [])
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
        is_active_tool = bool(tool_name and tool_name in name_set)
        is_doc_skill = not tool_name and not fm.get("parameters")
        always_inline = bool(fm.get("always_inline"))

        if not is_active_tool and not is_doc_skill:
            continue
        if is_doc_skill and not always_inline and not _INCLUDE_DOC_SKILLS:
            # Skipped — available on demand via discover_skills().
            continue
        if not body:
            continue

        if not _TIERING_ENABLED or len(body) <= _INLINE_FULL_CHARS or always_inline:
            sections.append(f"### Skill: {skill_name}\n{body}")
        else:
            essentials = _extract_skill_essentials(body)
            if essentials:
                sections.append(
                    f"### Skill: {skill_name} (essentials — call "
                    f"`discover_skills(name='{skill_name}')` for the full protocol)\n"
                    f"{essentials}"
                )
            else:
                # Fall back to a short header so the model at least knows the
                # skill exists; bodies that failed essentials extraction are
                # almost certainly malformed.
                desc = str(fm.get("description") or "").strip()
                sections.append(
                    f"### Skill: {skill_name} (on-demand)\n{desc[:300]}"
                )

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
