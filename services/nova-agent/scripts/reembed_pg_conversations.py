#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg

from nova.store import PG_DSN, _canonical_search_content, generate_embedding

TARGET_DIMS = 2048


async def _column_type(conn) -> str:
    row = await conn.fetchrow(
        """
        SELECT format_type(a.atttypid, a.atttypmod) AS type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'workspace'
          AND c.relname = 'ai_messages'
          AND a.attname = 'embedding'
          AND NOT a.attisdropped
        """
    )
    return row["type"] if row else ""


async def _embedding_dims(conn) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT vector_dims(embedding) AS dims, count(*) AS count
        FROM workspace.ai_messages
        WHERE embedding IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """
    )


async def _candidate_count(conn) -> int:
    return await conn.fetchval(
        """
        SELECT count(*)
        FROM workspace.ai_messages
        WHERE role = 'user'
           OR (role = 'assistant' AND importance_score >= 60)
        """
    )


async def _migrate_column(conn):
    col_type = await _column_type(conn)
    if col_type == f"vector({TARGET_DIMS})":
        return
    indexes = await conn.fetch(
        """
        SELECT schemaname, indexname
        FROM pg_indexes
        WHERE schemaname = 'workspace'
          AND tablename = 'ai_messages'
          AND indexdef ILIKE '%embedding%'
        """
    )
    for index in indexes:
        await conn.execute(f'DROP INDEX IF EXISTS "{index["schemaname"]}"."{index["indexname"]}"')
    await conn.execute("UPDATE workspace.ai_messages SET embedding = NULL WHERE embedding IS NOT NULL")
    await conn.execute(
        f"ALTER TABLE workspace.ai_messages ALTER COLUMN embedding TYPE vector({TARGET_DIMS})"
    )


async def _backfill(conn, batch_size: int, limit: int | None):
    total = 0
    while True:
        rows = await conn.fetch(
            """
            SELECT id, role, content
            FROM workspace.ai_messages
            WHERE embedding IS NULL
              AND (role = 'user' OR (role = 'assistant' AND importance_score >= 60))
            ORDER BY created_at ASC
            LIMIT $1
            """,
            batch_size,
        )
        if not rows:
            break
        if limit is not None:
            rows = rows[: max(0, limit - total)]
        for row in rows:
            content = _canonical_search_content(row["content"])
            if not content:
                continue
            embedding = await generate_embedding(content, input_type="passage")
            if not embedding:
                continue
            if len(embedding) != TARGET_DIMS:
                raise RuntimeError(f"Embedding model returned {len(embedding)} dims, expected {TARGET_DIMS}")
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            await conn.execute(
                "UPDATE workspace.ai_messages SET embedding = $2::vector WHERE id = $1::uuid",
                row["id"],
                embedding_str,
            )
            total += 1
            if total % 25 == 0:
                print(f"backfilled={total}", flush=True)
            if limit is not None and total >= limit:
                return total
    return total


async def main():
    parser = argparse.ArgumentParser(description="Migrate Nova PG conversation embeddings to the active 2048-dim model.")
    parser.add_argument("--apply", action="store_true", help="Actually mutate PostgreSQL. Without this, only reports state.")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    conn = await asyncpg.connect(dsn=os.environ.get("DATABASE_URL", PG_DSN))
    try:
        col_type = await _column_type(conn)
        dims = await _embedding_dims(conn)
        candidates = await _candidate_count(conn)
        print(f"embedding_column={col_type}")
        print("existing_dims=" + json.dumps([{ "dims": r["dims"], "count": r["count"] } for r in dims]))
        print(f"candidate_messages={candidates}")
        probe = await generate_embedding("Nova semantic search migration probe", input_type="query")
        print(f"active_model_dims={len(probe) if probe else None}")

        if not args.apply:
            print("dry_run=true")
            return

        await _migrate_column(conn)
        print(f"embedding_column_after={await _column_type(conn)}")
        total = await _backfill(conn, args.batch_size, args.limit)
        print(f"backfilled_total={total}")
        dims_after = await _embedding_dims(conn)
        print("final_dims=" + json.dumps([{ "dims": r["dims"], "count": r["count"] } for r in dims_after]))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
