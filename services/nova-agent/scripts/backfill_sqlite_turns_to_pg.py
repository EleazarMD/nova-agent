#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiosqlite

from nova.store import DB_PATH, _canonical_search_content, _sync_message_to_backend
from nova.user_resolver import canonical_user_id


async def _rows(limit: int | None):
    query = """
        SELECT s.session_id, s.conversation_id, s.user_id, t.rowid, t.role, t.content, t.tool_calls
        FROM turns t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE t.role IN ('user', 'assistant')
        ORDER BY t.timestamp ASC, t.rowid ASC
    """
    params = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await db.execute_fetchall(query, params)


async def main():
    parser = argparse.ArgumentParser(description="Backfill Nova SQLite turns into PostgreSQL semantic search corpus.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    rows = await _rows(args.limit)
    usable = [r for r in rows if _canonical_search_content(r["content"])]
    print(f"sqlite_turns={len(rows)}")
    print(f"usable_turns={len(usable)}")
    if not args.apply:
        print("dry_run=true")
        return

    count = 0
    for row in usable:
        tool_calls = None
        if row["tool_calls"]:
            try:
                tool_calls = json.loads(row["tool_calls"])
            except json.JSONDecodeError:
                tool_calls = None
        await _sync_message_to_backend(
            row["conversation_id"],
            canonical_user_id(row["user_id"]),
            row["role"],
            row["content"],
            tool_calls=tool_calls,
        )
        count += 1
        if count % 50 == 0:
            print(f"backfilled={count}", flush=True)
    print(f"backfilled_total={count}")


if __name__ == "__main__":
    asyncio.run(main())
