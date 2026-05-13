from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import aiosqlite
from loguru import logger

from nova.store import DB_PATH
from nova.user_resolver import canonical_user_id


TASK_ARTIFACT_METADATA_KEY = "nova_active_task_artifact_id"


@dataclass
class TaskArtifact:
    task_id: str
    user_id: str
    conversation_id: str
    session_id: str = ""
    kind: str = "general"
    status: str = "grounding"
    goal: str = ""
    source_context: list[dict[str, Any]] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    candidate_outputs: list[dict[str, Any]] = field(default_factory=list)
    selected_output: dict[str, Any] | None = None
    execution: dict[str, Any] = field(default_factory=lambda: {"tools_used": [], "delegations": [], "workspace_page_id": None})
    qa: dict[str, Any] = field(default_factory=lambda: {"status": "not_run", "checks": []})
    handoff: dict[str, Any] = field(default_factory=lambda: {"formats": ["markdown", "workspace_blocks", "json"], "links": []})
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def ensure_task_artifacts_table(path: str = DB_PATH) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_artifacts (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                session_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                goal TEXT,
                source_context_json TEXT NOT NULL,
                requirements_json TEXT NOT NULL,
                decisions_json TEXT NOT NULL,
                open_questions_json TEXT NOT NULL,
                candidate_outputs_json TEXT NOT NULL,
                selected_output_json TEXT,
                execution_json TEXT NOT NULL,
                qa_json TEXT NOT NULL,
                handoff_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_artifacts_user_updated ON task_artifacts(user_id, updated_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_artifacts_conversation ON task_artifacts(conversation_id, updated_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_artifacts_status ON task_artifacts(status, updated_at DESC)"
        )
        await db.commit()


def new_task_id(kind: str = "task") -> str:
    clean_kind = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in (kind or "task").lower()).strip("-") or "task"
    return f"{clean_kind}-{uuid.uuid4().hex[:12]}"


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _row_to_artifact(row: aiosqlite.Row) -> TaskArtifact:
    return TaskArtifact(
        task_id=row["task_id"],
        user_id=row["user_id"],
        conversation_id=row["conversation_id"],
        session_id=row["session_id"] or "",
        kind=row["kind"],
        status=row["status"],
        goal=row["goal"] or "",
        source_context=_loads(row["source_context_json"], []),
        requirements=_loads(row["requirements_json"], []),
        decisions=_loads(row["decisions_json"], []),
        open_questions=_loads(row["open_questions_json"], []),
        candidate_outputs=_loads(row["candidate_outputs_json"], []),
        selected_output=_loads(row["selected_output_json"], None),
        execution=_loads(row["execution_json"], {"tools_used": [], "delegations": [], "workspace_page_id": None}),
        qa=_loads(row["qa_json"], {"status": "not_run", "checks": []}),
        handoff=_loads(row["handoff_json"], {"formats": ["markdown", "workspace_blocks", "json"], "links": []}),
        metadata=_loads(row["metadata_json"], {}),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
    )


async def upsert_task_artifact(artifact: TaskArtifact, path: str = DB_PATH) -> TaskArtifact:
    await ensure_task_artifacts_table(path)
    now = time.time()
    if not artifact.task_id:
        artifact.task_id = new_task_id(artifact.kind)
    artifact.user_id = canonical_user_id(artifact.user_id)
    artifact.created_at = artifact.created_at or now
    artifact.updated_at = now
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO task_artifacts (
                task_id, user_id, conversation_id, session_id, kind, status, goal,
                source_context_json, requirements_json, decisions_json, open_questions_json,
                candidate_outputs_json, selected_output_json, execution_json, qa_json,
                handoff_json, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                user_id=excluded.user_id,
                conversation_id=excluded.conversation_id,
                session_id=excluded.session_id,
                kind=excluded.kind,
                status=excluded.status,
                goal=excluded.goal,
                source_context_json=excluded.source_context_json,
                requirements_json=excluded.requirements_json,
                decisions_json=excluded.decisions_json,
                open_questions_json=excluded.open_questions_json,
                candidate_outputs_json=excluded.candidate_outputs_json,
                selected_output_json=excluded.selected_output_json,
                execution_json=excluded.execution_json,
                qa_json=excluded.qa_json,
                handoff_json=excluded.handoff_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                artifact.task_id,
                artifact.user_id,
                artifact.conversation_id,
                artifact.session_id,
                artifact.kind,
                artifact.status,
                artifact.goal,
                json.dumps(artifact.source_context),
                json.dumps(artifact.requirements),
                json.dumps(artifact.decisions),
                json.dumps(artifact.open_questions),
                json.dumps(artifact.candidate_outputs),
                json.dumps(artifact.selected_output) if artifact.selected_output is not None else None,
                json.dumps(artifact.execution),
                json.dumps(artifact.qa),
                json.dumps(artifact.handoff),
                json.dumps(artifact.metadata),
                artifact.created_at,
                artifact.updated_at,
            ),
        )
        await db.commit()
    logger.info(f"Task artifact upserted: {artifact.task_id} status={artifact.status}")
    return artifact


async def create_task_artifact(
    *,
    user_id: str,
    conversation_id: str,
    session_id: str = "",
    kind: str = "general",
    goal: str = "",
    status: str = "grounding",
    metadata: dict[str, Any] | None = None,
    path: str = DB_PATH,
) -> TaskArtifact:
    artifact = TaskArtifact(
        task_id=new_task_id(kind),
        user_id=user_id,
        conversation_id=conversation_id,
        session_id=session_id,
        kind=kind,
        status=status,
        goal=goal,
        metadata=metadata or {},
    )
    return await upsert_task_artifact(artifact, path=path)


async def get_task_artifact(task_id: str, path: str = DB_PATH) -> dict[str, Any] | None:
    await ensure_task_artifacts_table(path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM task_artifacts WHERE task_id = ?", (task_id,))
    if not rows:
        return None
    return _row_to_artifact(rows[0]).to_dict()


async def list_task_artifacts(
    *,
    user_id: str = "",
    conversation_id: str = "",
    status: str = "",
    limit: int = 50,
    path: str = DB_PATH,
) -> list[dict[str, Any]]:
    await ensure_task_artifacts_table(path)
    clauses = []
    params: list[Any] = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(canonical_user_id(user_id))
    if conversation_id:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            f"SELECT * FROM task_artifacts {where_sql} ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        )
    return [_row_to_artifact(row).to_dict() for row in rows]


def run_task_artifact_qa(artifact: TaskArtifact) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    add_check("goal_present", bool(artifact.goal.strip()), "Artifact has a durable task goal.")
    add_check("conversation_linked", bool(artifact.conversation_id.strip()), "Artifact is linked to a conversation.")
    add_check("source_context_present", bool(artifact.source_context), "Artifact has grounding/source context.")
    add_check(
        "execution_tools_recorded",
        bool((artifact.execution or {}).get("tools_used")),
        "Artifact records tool or delegation evidence.",
    )
    if artifact.status in {"executing", "handoff", "complete"}:
        add_check(
            "handoff_evidence_present",
            bool(artifact.source_context or artifact.candidate_outputs or (artifact.handoff or {}).get("links")),
            "Artifact has handoff evidence before claiming delivery.",
        )
    add_check(
        "open_questions_resolved_for_handoff",
        artifact.status not in {"handoff", "complete"} or not artifact.open_questions,
        "No unresolved open questions remain for a handoff artifact.",
    )

    failed = [check for check in checks if not check["passed"]]
    return {
        "status": "failed" if failed else "passed",
        "checks": checks,
        "checked_at": time.time(),
        "failed_count": len(failed),
    }


def extract_handoff_links(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    candidates: list[dict[str, Any]] = []

    def add_link(kind: str, value: str, label: str = "") -> None:
        clean = str(value or "").strip().strip(".,);]")
        if not clean:
            return
        item = {"kind": kind, "value": clean}
        if label:
            item["label"] = label
        if item not in candidates:
            candidates.append(item)

    def walk(value: Any, label: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_label = str(key)
                lower_key = key_label.lower()
                if isinstance(child, str):
                    if lower_key in {"url", "link", "href", "page_url", "workspace_url"}:
                        add_link("url", child, key_label)
                    if lower_key in {"page_id", "pageid", "workspace_page_id", "workspacepageid", "document_id", "documentid"}:
                        add_link("workspace_page_id", child, key_label)
                walk(child, key_label)
        elif isinstance(value, list):
            for child in value:
                walk(child, label)
        elif isinstance(value, str):
            for url in re.findall(r"https?://[^\s\"'<>]+", value):
                add_link("url", url, label)
            for match in re.finditer(
                r"\b(?:workspace[_ -]?page[_ -]?id|page[_ -]?id|document[_ -]?id)\b\s*[:=]\s*([A-Za-z0-9_.:-]{6,})",
                value,
                flags=re.IGNORECASE,
            ):
                add_link("workspace_page_id", match.group(1), label)

    walk(result)
    return candidates


def merge_handoff_links(artifact: TaskArtifact, result: Any) -> TaskArtifact:
    links = extract_handoff_links(result)
    if not links:
        return artifact
    artifact.handoff.setdefault("links", [])
    for link in links:
        if link not in artifact.handoff["links"]:
            artifact.handoff["links"].append(link)
        if link.get("kind") == "workspace_page_id" and not artifact.execution.get("workspace_page_id"):
            artifact.execution["workspace_page_id"] = link.get("value")
    return artifact


async def mark_task_artifact_qa(task_id: str, path: str = DB_PATH) -> dict[str, Any] | None:
    artifact_data = await get_task_artifact(task_id, path=path)
    if not artifact_data:
        return None
    artifact = TaskArtifact(**artifact_data)
    artifact.qa = run_task_artifact_qa(artifact)
    await upsert_task_artifact(artifact, path=path)
    return artifact.to_dict()

async def get_task_artifact_summary(limit: int = 500, path: str = DB_PATH) -> dict[str, Any]:
    artifacts = await list_task_artifacts(limit=limit, path=path)
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_qa: dict[str, int] = {}
    with_handoff_links = 0
    for artifact in artifacts:
        status = str(artifact.get("status") or "unknown")
        kind = str(artifact.get("kind") or "unknown")
        qa = artifact.get("qa") if isinstance(artifact.get("qa"), dict) else {}
        qa_status = str(qa.get("status") or "not_run")
        handoff = artifact.get("handoff") if isinstance(artifact.get("handoff"), dict) else {}
        by_status[status] = by_status.get(status, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_qa[qa_status] = by_qa.get(qa_status, 0) + 1
        if handoff.get("links"):
            with_handoff_links += 1
    return {
        "total": len(artifacts),
        "by_status": by_status,
        "by_kind": by_kind,
        "qa": by_qa,
        "with_handoff_links": with_handoff_links,
        "limit": max(1, min(int(limit), 500)),
    }


async def get_task_artifact_timeline(task_id: str, path: str = DB_PATH) -> dict[str, Any] | None:
    artifact = await get_task_artifact(task_id, path=path)
    if not artifact:
        return None
    events: list[dict[str, Any]] = []
    if artifact.get("created_at"):
        events.append({"ts": artifact["created_at"], "type": "created", "status": artifact.get("status"), "text": artifact.get("goal", "")})
    for item in artifact.get("source_context") or []:
        if isinstance(item, dict):
            events.append({"ts": item.get("ts") or artifact.get("updated_at") or artifact.get("created_at"), "type": item.get("type") or "source_context", "tool": item.get("tool", ""), "text": item.get("text", "")})
    for item in artifact.get("decisions") or []:
        if isinstance(item, dict):
            events.append({"ts": item.get("ts") or artifact.get("updated_at") or artifact.get("created_at"), "type": "decision", "decision_type": item.get("type", ""), "text": item.get("text", "")})
    execution = artifact.get("execution") if isinstance(artifact.get("execution"), dict) else {}
    for item in execution.get("delegations") or []:
        if isinstance(item, dict):
            events.append({"ts": item.get("ts") or artifact.get("updated_at") or artifact.get("created_at"), "type": "delegation", "agent": item.get("agent", ""), "action": item.get("action", ""), "text": item.get("result", "")})
    handoff = artifact.get("handoff") if isinstance(artifact.get("handoff"), dict) else {}
    for link in handoff.get("links") or []:
        if isinstance(link, dict):
            events.append({"ts": artifact.get("updated_at"), "type": "handoff_link", "kind": link.get("kind", ""), "value": link.get("value", ""), "label": link.get("label", "")})
    qa = artifact.get("qa") if isinstance(artifact.get("qa"), dict) else {}
    if qa.get("checked_at"):
        events.append({"ts": qa.get("checked_at"), "type": "qa", "status": qa.get("status", "not_run"), "failed_count": qa.get("failed_count", 0)})
    if artifact.get("updated_at"):
        events.append({"ts": artifact.get("updated_at"), "type": "updated", "status": artifact.get("status"), "text": ""})
    events.sort(key=lambda item: float(item.get("ts") or 0))
    return {"task_id": task_id, "artifact": artifact, "events": events, "total": len(events)}


async def get_task_artifact_qa_failures(limit: int = 100, path: str = DB_PATH) -> dict[str, Any]:
    artifacts = await list_task_artifacts(limit=max(1, min(int(limit), 500)), path=path)
    failures = []
    for artifact in artifacts:
        qa = artifact.get("qa") if isinstance(artifact.get("qa"), dict) else {}
        if qa.get("status") != "failed":
            continue
        failed_checks = [check for check in qa.get("checks", []) if isinstance(check, dict) and not check.get("passed")]
        failures.append({
            "task_id": artifact.get("task_id"),
            "goal": artifact.get("goal"),
            "kind": artifact.get("kind"),
            "status": artifact.get("status"),
            "updated_at": artifact.get("updated_at"),
            "failed_count": qa.get("failed_count", len(failed_checks)),
            "failed_checks": failed_checks,
        })
    return {"failures": failures, "total": len(failures)}


async def get_recent_task_artifact_handoffs(limit: int = 50, path: str = DB_PATH) -> dict[str, Any]:
    artifacts = await list_task_artifacts(status="handoff", limit=max(1, min(int(limit), 500)), path=path)
    handoffs = []
    for artifact in artifacts:
        handoff = artifact.get("handoff") if isinstance(artifact.get("handoff"), dict) else {}
        execution = artifact.get("execution") if isinstance(artifact.get("execution"), dict) else {}
        links = handoff.get("links") or []
        if not links and not execution.get("workspace_page_id"):
            continue
        handoffs.append({
            "task_id": artifact.get("task_id"),
            "goal": artifact.get("goal"),
            "kind": artifact.get("kind"),
            "conversation_id": artifact.get("conversation_id"),
            "workspace_page_id": execution.get("workspace_page_id"),
            "links": links,
            "updated_at": artifact.get("updated_at"),
        })
    return {"handoffs": handoffs, "total": len(handoffs)}
async def get_active_task_artifact(session_id: str, path: str = DB_PATH) -> dict[str, Any] | None:
    from nova.store import get_session_metadata

    metadata = await get_session_metadata(session_id, path=path)
    task_id = str(metadata.get(TASK_ARTIFACT_METADATA_KEY) or "")
    if not task_id:
        return None
    return await get_task_artifact(task_id, path=path)


async def set_active_task_artifact(session_id: str, task_id: str, path: str = DB_PATH) -> None:
    from nova.store import update_session_metadata_key

    await update_session_metadata_key(session_id, TASK_ARTIFACT_METADATA_KEY, task_id, path=path)
