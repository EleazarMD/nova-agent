#!/usr/bin/env python3
"""Nova session-planner CLI — directly invoke the planner spine.

Examples
--------
    # what am I working on (cross-conversation spine)?
    ./scripts/nova_plans.py projects --user dfd9379f-a9cd-4241-99e7-140f5e89e3cd

    # full graph for a single project
    ./scripts/nova_plans.py project ceo-meeting-houston-methodist-baytown \
        --user dfd9379f-a9cd-4241-99e7-140f5e89e3cd

    # flat list of active plans
    ./scripts/nova_plans.py plans --user dfd9379f-a9cd-4241-99e7-140f5e89e3cd

    # detail for one plan (8-char prefix accepted)
    ./scripts/nova_plans.py plan 4965951e

    # consolidate a fragment into a canonical plan
    ./scripts/nova_plans.py merge <from_plan_id> --into <canonical_plan_id>

    # health check (calls the same logic Nova uses internally)
    ./scripts/nova_plans.py health 4965951e

JSON output is the default; pass --pretty for human-readable rendering.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any

# Make sibling `nova/` importable when invoked directly from the repo
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _short(s: str | None, n: int = 60) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_projects(out: dict[str, Any], pretty: bool) -> None:
    if not pretty:
        print(json.dumps(out, indent=2, default=str))
        return
    totals = out.get("totals") or {}
    print(
        f"\n{totals.get('projects', 0)} projects · "
        f"{totals.get('active_plans', 0)} active plans · "
        f"{totals.get('merged_plans', 0)} merged fragments\n"
    )
    print(f"  {'PROJECT_KEY':<48}  {'TITLE':<48}  STEPS  SESS  FRAG  UPDATED")
    print(f"  {'-'*48}  {'-'*48}  -----  ----  ----  -----------------")
    for p in out.get("projects") or []:
        print(
            f"  {p['project_key']:<48}  "
            f"{_short(p['title'], 48):<48}  "
            f"{p['step_open']:>2}/{p['step_total']:<2}  "
            f"{p['session_total']:>4}  "
            f"{p['merged_count']:>4}  "
            f"{_fmt_ts(p['last_updated_at'])}"
        )
    eph = out.get("ephemeral_plans") or []
    if eph:
        print(f"\n  Ephemeral (no project_key):")
        for p in eph:
            print(
                f"    {p['active_plan_id'][:8]}  {_short(p['title'], 60)}  "
                f"updated {_fmt_ts(p['last_updated_at'])}"
            )


def _print_project(out: dict[str, Any], pretty: bool) -> None:
    if not pretty:
        print(json.dumps(out, indent=2, default=str))
        return
    canonical = out.get("canonical") or {}
    print(f"\n  📌 {out.get('title')}")
    print(f"     project_key   : {out.get('project_key')}")
    print(f"     canonical plan: {canonical.get('plan_id', '—')}")
    print(f"     workspace page: {canonical.get('workspace_page_id') or '—'}")
    pages = out.get("workspace_pages") or []
    for pg in pages:
        print(
            f"        ↳ page {pg['id'][:8]} · {_short(pg['title'], 50)} · "
            f"{pg.get('block_count', 0)} blocks"
        )
    fragments = out.get("fragments") or []
    if fragments:
        print(f"     fragments     : {len(fragments)} merged")
        for f in fragments[:5]:
            print(
                f"        ↳ {f['plan_id'][:8]}  '{_short(f.get('topic', ''), 50)}'  "
                f"→ {f.get('merged_into', '?')[:8]}"
            )

    steps = canonical.get("steps") or []
    if steps:
        print(f"\n  Steps ({len(steps)}):")
        for s in steps:
            mark = {"done": "✓", "skipped": "⊘"}.get(s["status"], "·")
            print(f"    {mark} [{s['status']:<10}] {s['title']}")

    sessions = canonical.get("sessions") or []
    if sessions:
        print(f"\n  Recent sessions ({len(sessions)}):")
        for s in sessions[:6]:
            print(f"    [{_fmt_ts(s.get('timestamp'))}] {_short(s.get('summary'), 80)}")

    timeline = out.get("timeline") or []
    if timeline:
        print(f"\n  Timeline ({len(timeline)} events):")
        for ev in timeline[:12]:
            kind = (ev.get("kind") or "?")[:14]
            print(f"    [{_fmt_ts(ev.get('ts'))}] {kind:<14} {_short(ev.get('summary'), 70)}")


async def cmd_projects(args) -> int:
    from nova.context_layer import projects as ncl_projects
    out = await ncl_projects(args.user)
    _print_projects(out, args.pretty)
    return 0


async def cmd_project(args) -> int:
    from nova.context_layer import project as ncl_project
    out = await ncl_project(args.user, args.project_key, include_turns=not args.no_turns)
    if out.get("error"):
        print(out["error"], file=sys.stderr)
        return 1
    _print_project(out, args.pretty)
    return 0


async def cmd_plans(args) -> int:
    from nova.task_plan import list_plans
    plans = await list_plans(user_id=args.user, status=args.status)
    if not args.pretty:
        print(json.dumps(plans, indent=2, default=str))
        return 0
    print(f"\n  {len(plans)} {args.status} plan(s):")
    for p in plans:
        pk = (p.get("project_key") or "—").ljust(48)
        print(
            f"    {p['plan_id'][:8]}  {pk}  "
            f"updated {_fmt_ts(p.get('updated_at'))}  "
            f"'{_short(p.get('topic'), 60)}'"
        )
    return 0


async def cmd_plan(args) -> int:
    from nova.task_plan import get_plan
    p = await get_plan(args.plan_id)
    if not p:
        print(f"No plan found for {args.plan_id!r}", file=sys.stderr)
        return 1
    if not args.pretty:
        print(json.dumps(p, indent=2, default=str))
        return 0
    print(f"\n  📋 {p['topic']}")
    print(f"     plan_id     : {p['plan_id']}")
    print(f"     project_key : {p.get('project_key') or '—'}")
    print(f"     workspace   : {p.get('workspace_page_id') or '—'}")
    print(f"     status      : {p['status']}")
    print(f"     updated_at  : {_fmt_ts(p.get('updated_at'))}")
    steps = p.get("steps") or []
    if steps:
        print(f"\n  Steps ({len(steps)}):")
        for s in steps:
            mark = {"done": "✓", "skipped": "⊘"}.get(s["status"], "·")
            print(f"    {mark} [{s['status']:<10}] {s['title']}")
    sessions = p.get("sessions") or []
    if sessions:
        print(f"\n  Sessions ({len(sessions)}):")
        for s in sessions[:6]:
            print(f"    [{_fmt_ts(s.get('timestamp'))}] {_short(s.get('summary'), 80)}")
    return 0


async def cmd_merge(args) -> int:
    from nova.task_plan import merge_plans
    res = await merge_plans(args.from_plan_id, args.into)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 1


async def cmd_health(args) -> int:
    from nova.tools import handle_verify_plan_state
    out = await handle_verify_plan_state(plan_id=args.plan_id)
    print(out)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Nova session-planner CLI")
    p.add_argument(
        "--user",
        default=os.environ.get("NOVA_USER_ID", ""),
        help="user_id to scope queries (default: env NOVA_USER_ID, else all)",
    )
    p.add_argument("--pretty", action="store_true", help="human-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("projects", help="cross-conversation spine view")

    sp = sub.add_parser("project", help="full project graph")
    sp.add_argument("project_key")
    sp.add_argument("--no-turns", action="store_true", help="skip recent turn scan")

    sp = sub.add_parser("plans", help="list plans (flat)")
    sp.add_argument("--status", default="active", choices=["active", "completed", "merged"])

    sp = sub.add_parser("plan", help="single plan detail")
    sp.add_argument("plan_id", help="full UUID or 8-char prefix")

    sp = sub.add_parser("merge", help="merge a fragment into a canonical plan")
    sp.add_argument("from_plan_id")
    sp.add_argument("--into", required=True, help="canonical plan_id to merge into")

    sp = sub.add_parser("health", help="verify_plan_state for a single plan")
    sp.add_argument("plan_id")

    return p


def main() -> int:
    args = _build_parser().parse_args()
    fn = {
        "projects": cmd_projects,
        "project": cmd_project,
        "plans": cmd_plans,
        "plan": cmd_plan,
        "merge": cmd_merge,
        "health": cmd_health,
    }[args.cmd]
    return asyncio.run(fn(args))


if __name__ == "__main__":
    sys.exit(main())
