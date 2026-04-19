#!/usr/bin/env python3
"""Backfill embeddings for existing ai_messages using NVIDIA NIM (nv-embedqa-e5-v5).

Processes messages in batches, generates 1024-dim embeddings via NIM,
and updates the embedding column in PostgreSQL.

Usage:
    python backfill_embeddings.py [--batch-size 50] [--dry-run] [--user-only]
"""

import argparse
import asyncio
import json
import os
import sys
import time

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger

# Configure logging
logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

NIM_EMBED_URL = os.environ.get("NIM_EMBED_URL", "http://localhost:8006/v1/embeddings")
NIM_EMBED_MODEL = os.environ.get("NIM_EMBED_MODEL", "nvidia/nv-embedqa-e5-v5")
PG_DSN = os.environ.get("DATABASE_URL", "postgresql://eleazar@localhost/ecosystem_unified")


async def generate_embedding_batch(texts: list[str], input_type: str = "passage") -> list[list[float] | None]:
    """Generate embeddings for a batch of texts via NVIDIA NIM.
    
    NIM has a 512-token limit per input, so we truncate to ~500 chars.
    Falls back to single-message requests if batch fails.
    """
    # Truncate to stay under NIM's 512-token limit (~500 chars conservative)
    truncated = [t[:500] for t in texts]
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                NIM_EMBED_URL,
                json={
                    "model": NIM_EMBED_MODEL,
                    "input": truncated,
                    "input_type": input_type,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return [d.get("embedding") for d in data.get("data", [])]
            logger.warning(f"NIM batch error {resp.status_code}, falling back to single requests")
    except Exception as e:
        logger.warning(f"NIM batch failed: {e}, falling back to single requests")
    
    # Fallback: embed one at a time
    results = []
    for text in truncated:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    NIM_EMBED_URL,
                    json={"model": NIM_EMBED_MODEL, "input": [text], "input_type": input_type},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results.append(data.get("data", [{}])[0].get("embedding"))
                else:
                    results.append(None)
        except Exception:
            results.append(None)
    return results


async def backfill(batch_size: int = 50, dry_run: bool = False, user_only: bool = False):
    """Backfill embeddings for messages that don't have one yet."""
    import asyncpg

    pool = await asyncpg.create_pool(dsn=PG_DSN, min_size=1, max_size=3)

    # Count messages needing embeddings
    role_filter = "AND role = 'user'" if user_only else ""
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"""SELECT count(*) FROM workspace.ai_messages
                WHERE embedding IS NULL {role_filter}"""
        )
    logger.info(f"Messages needing embeddings: {total} (user_only={user_only})")

    if total == 0:
        logger.info("All messages already have embeddings!")
        return

    processed = 0
    embedded = 0
    failed = 0
    start_time = time.time()

    while True:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, role, content FROM workspace.ai_messages
                    WHERE embedding IS NULL {role_filter}
                    ORDER BY created_at ASC
                    LIMIT $1""",
                batch_size,
            )

        if not rows:
            break

        # Generate embeddings in batch
        texts = [r["content"][:2000] for r in rows]
        embeddings = await generate_embedding_batch(texts)

        if not dry_run:
            # Update messages with embeddings
            async with pool.acquire() as conn:
                for row, emb in zip(rows, embeddings):
                    if emb:
                        emb_str = "[" + ",".join(str(v) for v in emb) + "]"
                        await conn.execute(
                            """UPDATE workspace.ai_messages SET embedding = $2::vector WHERE id = $1::uuid""",
                            row["id"], emb_str,
                        )
                        embedded += 1
                    else:
                        failed += 1
        else:
            embedded += sum(1 for e in embeddings if e)
            failed += sum(1 for e in embeddings if not e)

        processed += len(rows)
        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        eta = (total - processed) / rate if rate > 0 else 0
        logger.info(
            f"Progress: {processed}/{total} ({processed*100//total}%) | "
            f"embedded={embedded} failed={failed} | "
            f"rate={rate:.0f}/s ETA={eta/60:.1f}min"
        )

        # Small delay to avoid overwhelming NIM
        await asyncio.sleep(0.1)

    elapsed = time.time() - start_time
    logger.info(f"Done! {embedded} embedded, {failed} failed, in {elapsed:.1f}s")

    if not dry_run:
        # Rebuild IVFFlat index for better recall now that we have data
        logger.info("Rebuilding IVFFlat index for better recall...")
        async with pool.acquire() as conn:
            await conn.execute("DROP INDEX IF EXISTS workspace.idx_ai_messages_embedding")
            await conn.execute(
                """CREATE INDEX idx_ai_messages_embedding
                   ON workspace.ai_messages USING ivfflat (embedding vector_cosine_ops)
                   WITH (lists = 100)"""
            )
        logger.info("Index rebuilt!")

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill embeddings for ai_messages")
    parser.add_argument("--batch-size", type=int, default=20, help="Messages per batch")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--user-only", action="store_true", help="Only embed user messages")
    args = parser.parse_args()

    asyncio.run(backfill(batch_size=args.batch_size, dry_run=args.dry_run, user_only=args.user_only))
