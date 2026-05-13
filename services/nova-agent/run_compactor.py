import asyncio
import os
os.environ["COMPACTION_MIN_AGE_DAYS"] = "0"
from nova.store import run_compaction_cycle

async def main():
    print("Starting compaction cycle for iOS user...")
    results = await run_compaction_cycle("dfd9379f-a9cd-4241-99e7-140f5e89e3cd")
    print("Compaction complete.")
    if not results:
        print("No conversations needed compaction.")
        return
        
    compacted = [r for r in results if r.get("status") == "compacted"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    failed = [r for r in results if r.get("status") == "failed"]
    total_facts = sum(r.get("facts_extracted", 0) for r in compacted)
    
    print(f"  Compacted: {len(compacted)} conversations")
    print(f"  Skipped: {len(skipped)}")
    print(f"  Failed: {len(failed)}")
    print(f"  Facts extracted: {total_facts}")
    
    for r in compacted[:5]:
        print(f"  - {r.get('title', 'Unknown')} -> {r.get('topics', [])}")

if __name__ == "__main__":
    asyncio.run(main())
