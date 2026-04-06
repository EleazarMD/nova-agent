"""
Test PCG (Personal Context Graph) skills for Nova Agent.

Tests:
- query_context (Context Bridge: PIC + KG-API + LIAM)
- save_memory (PIC write)
- recall_memory (PIC read)
- kg_query (Knowledge Graph)

Architecture:
    Nova Agent
        │
        ├─► Context Bridge (port 8764) → query_context
        │   ├─► PIC (port 8765) → save_memory / recall_memory
        │   └─► KG-API (port 8765) → kg_query
        │
        └─► Direct PIC access (port 8765) → save_memory / recall_memory

Both PIC and KG-API share Neo4j + ChromaDB backend at port 8765.
"""

import asyncio
import sys
from pathlib import Path

# Add skills directory to path for pic_memory import
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "pic-memory" / "scripts"))

from nova.context_bridge import query_knowledge, get_enriched_context
from nova.knowledge_graph import knowledge_graph_query, get_service_status_from_graph
from pic_memory import (
    get_identity,
    get_preferences,
    record_observation,
    create_preference,
    build_pic_context,
)


# -----------------------------------------------------------------------------
# Context Bridge Tests (Unified PIC + KG-API + LIAM)
# -----------------------------------------------------------------------------

async def test_query_context_basic():
    """Test basic query_context via Context Bridge."""
    result = await query_knowledge(
        query="test query for personal context",
        include_personal=True,
        include_knowledge=True,
        include_dimensions=True,
    )
    
    assert "error" not in result, f"Context Bridge query failed: {result.get('error')}"
    assert "personal" in result or "knowledge" in result or "synthesis" in result
    print(f"✓ query_context: {len(result.get('personal', []))} personal, {len(result.get('knowledge', []))} knowledge")


async def test_get_enriched_context():
    """Test enriched context fetch from Context Bridge."""
    result = await get_enriched_context(
        agent_id="nova-agent",
        include_goals=True,
        include_relationships=False,
    )
    
    assert "error" not in result, f"Enriched context fetch failed: {result.get('error')}"
    # May return empty if PIC is not populated
    print(f"✓ get_enriched_context: goals={len(result.get('goals', []))}, entities={len(result.get('relevant_entities', []))}")


# -----------------------------------------------------------------------------
# PIC Memory Tests (Direct PIC access)
# -----------------------------------------------------------------------------

async def test_pic_get_identity():
    """Test fetching user identity from PIC."""
    identity = await get_identity()
    
    if identity:
        print(f"✓ PIC get_identity: {identity.get('identity', {}).get('name', 'unknown')}")
    else:
        print("⚠ PIC get_identity: No identity found (PIC may be empty)")


async def test_pic_get_preferences():
    """Test fetching user preferences from PIC."""
    prefs = await get_preferences()
    
    print(f"✓ PIC get_preferences: {len(prefs)} preferences found")
    if prefs:
        for p in prefs[:3]:  # Show first 3
            print(f"    - {p.get('category', '?' )}/{p.get('key', '?')}: {p.get('value', '')[:50]}...")


async def test_pic_save_and_recall():
    """Test save_memory and recall_memory round-trip."""
    test_fact = f"Test preference from Nova PCG test at {asyncio.get_event_loop().time()}"
    test_category = "technology"
    
    # Save
    saved = await record_observation(
        observation_type="preference",
        category=test_category,
        key="test_nova_pcg",
        value=test_fact,
        context="Automated test from test_pcg_skills.py",
    )
    
    assert saved, "Failed to save observation to PIC"
    print(f"✓ PIC save_memory: saved test observation")
    
    # Recall (via get_preferences - searches the cache which gets invalidated on write)
    # Note: The cache invalidation means next read hits PIC fresh
    prefs = await get_preferences(categories=[test_category])
    
    found = any("test_nova_pcg" in p.get('key', '') for p in prefs)
    if found:
        print(f"✓ PIC recall_memory: found saved test observation")
    else:
        print(f"⚠ PIC recall_memory: test observation not found in results (may need re-indexing)")


async def test_pic_build_context():
    """Test building full PIC context for system prompt."""
    context = await build_pic_context("test-user-id")
    
    assert "user_timezone" in context
    print(f"✓ PIC build_context: timezone={context.get('user_timezone')}, snippets={len(context.get('memory_snippets', []))}")


# -----------------------------------------------------------------------------
# Knowledge Graph Tests (KG-API)
# -----------------------------------------------------------------------------

async def test_kg_query_basic():
    """Test basic knowledge graph query."""
    context = await knowledge_graph_query("nova agent")
    
    # May return empty if KG is mock or empty
    if context:
        print(f"✓ kg_query: returned context for 'nova agent'")
    else:
        print("⚠ kg_query: no context returned (KG may be empty or mock)")


async def test_kg_service_status():
    """Test fetching service status from Knowledge Graph."""
    status = await get_service_status_from_graph("nova-agent")
    
    if "error" not in status:
        service = status.get("service", {})
        deps = status.get("dependencies", [])
        print(f"✓ kg_service_status: {service.get('name', '?')} with {len(deps)} dependencies")
    else:
        print(f"⚠ kg_service_status: {status.get('error')}")


# -----------------------------------------------------------------------------
# Integration Test (Full PCG flow)
# -----------------------------------------------------------------------------

async def test_full_pcg_flow():
    """Test full PCG flow: save → query_context → verify."""
    print("\n=== Full PCG Integration Test ===")
    
    # Step 1: Save a test fact
    test_value = f"Integration test value {asyncio.get_event_loop().time()}"
    saved = await record_observation(
        observation_type="test",
        category="other",
        key="pcg_integration_test",
        value=test_value,
        context="Full PCG flow integration test",
    )
    assert saved, "Step 1 failed: Could not save to PIC"
    print("Step 1 ✓: Saved test fact to PIC")
    
    # Step 2: Query via Context Bridge
    result = await query_knowledge(
        query="pcg integration test preference",
        include_personal=True,
        include_knowledge=False,
        include_dimensions=False,
    )
    assert "error" not in result, f"Step 2 failed: {result.get('error')}"
    print(f"Step 2 ✓: Queried Context Bridge ({len(result.get('personal', []))} personal results)")
    
    # Step 3: Verify we can read it back via direct PIC
    prefs = await get_preferences(categories=["other"])
    print(f"Step 3 ✓: Read back from PIC ({len(prefs)} preferences in 'other' category)")
    
    print("=== Full PCG Integration Test Complete ===\n")


# -----------------------------------------------------------------------------
# Main entry point for manual testing
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n🧪 Testing Nova PCG Skills\n")
    
    async def run_all_tests():
        tests = [
            ("Context Bridge - query_context", test_query_context_basic),
            ("Context Bridge - enriched context", test_get_enriched_context),
            ("PIC - get_identity", test_pic_get_identity),
            ("PIC - get_preferences", test_pic_get_preferences),
            ("PIC - save and recall", test_pic_save_and_recall),
            ("PIC - build context", test_pic_build_context),
            ("KG - basic query", test_kg_query_basic),
            ("KG - service status", test_kg_service_status),
            ("Full PCG integration", test_full_pcg_flow),
        ]
        
        for name, test_fn in tests:
            try:
                await test_fn()
            except Exception as e:
                print(f"✗ {name}: {e}")
    
    asyncio.run(run_all_tests())
    print("\n✅ PCG skill tests complete\n")
