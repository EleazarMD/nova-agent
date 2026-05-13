import asyncio
import os
import aiosqlite
from nova.store import DB_PATH, append_learning_event
from nova.learning import consolidate_session_learning, upsert_learned_plan_candidate

async def main():
    print(f"Using DB: {DB_PATH}")
    session_id = "test_phase6_123"
    
    await upsert_learned_plan_candidate(
        trigger_text="Memorize my social security number",
        intent="memory_save_request",
        tools_used=["save_memory"],
        source_session_id=session_id
    )
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE learned_plan_candidates SET confidence = 0.50 WHERE trigger_text = 'Memorize my social security number'")
        await db.commit()
        
        cursor = await db.execute("SELECT id, confidence FROM learned_plan_candidates WHERE trigger_text = 'Memorize my social security number'")
        row = await cursor.fetchone()
        candidate_id = row[0]
        initial_conf = row[1]
        print(f"Initial Candidate ID: {candidate_id}, Confidence: {initial_conf}")
        
    print("Injecting candidate_applied event...")
    await append_learning_event(
        event_type="candidate_applied",
        source_layer="orchestrator",
        session_id=session_id,
        payload={"candidate_id": candidate_id}
    )
    
    print("Injecting user correction ('No, stop, that is wrong')...")
    await append_learning_event(
        event_type="user_turn_received",
        source_layer="transport",
        session_id=session_id,
        canonical_text="No, stop, that is wrong",
    )
    
    print("Running consolidator...")
    await consolidate_session_learning(session_id)
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT confidence FROM learned_plan_candidates WHERE id = ?", (candidate_id,))
        row = await cursor.fetchone()
        if row:
            print(f"Final Confidence: {row[0]} (Should be penalized or deleted)")
        else:
            print("Candidate was PURGED from database!")

if __name__ == "__main__":
    os.environ["SQLITE_PATH"] = "./data/nova_test.db"
    asyncio.run(main())
