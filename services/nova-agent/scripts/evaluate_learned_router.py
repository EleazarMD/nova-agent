#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from nova.learned_router import evaluate_learned_router


async def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Nova learned-router candidates against recent turn policy observations.")
    parser.add_argument("--limit", type=int, default=200, help="Number of recent policy observations to evaluate.")
    parser.add_argument("--db", default="", help="Optional SQLite DB path. Defaults to Nova store DB_PATH.")
    parser.add_argument("--items", type=int, default=10, help="Number of detailed items to print.")
    args = parser.parse_args()

    report = await evaluate_learned_router(limit=args.limit, path=args.db or None)
    details = report.pop("items", [])
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.items > 0:
        print("\n# Sample items")
        for item in details[: args.items]:
            print(json.dumps(item, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
