"""
Context Bridge client for Nova Agent.

Orchestrates PIC (Personal Identity Core) and PCG Knowledge Graph for unified knowledge access.
Provides Nova with single-entry access to both personal and general knowledge.

Port: 8764
"""

import os
import aiohttp
from typing import Optional, Any
from loguru import logger

CONTEXT_BRIDGE_URL = os.environ.get("CONTEXT_BRIDGE_URL", "http://localhost:8764")
_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def query_knowledge(
    query: str,
    include_personal: bool = True,
    include_knowledge: bool = True,
    include_dimensions: bool = True,
) -> dict[str, Any]:
    """
    Query across PIC and PCG Knowledge Graph through Context Bridge.
    
    Returns unified results with:
    - personal: PIC identity, goals, context
    - knowledge: PCG Knowledge Graph entities, facts
    - applicable_dimensions: LIAM dimensions matching query
    - synthesis: pre-formatted summary
    """
    url = f"{CONTEXT_BRIDGE_URL}/v1/query"
    body = {
        "query": query,
        "include_personal": include_personal,
        "include_knowledge": include_knowledge,
        "include_dimensions": include_dimensions,
        "max_results": 10,
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"Context Bridge query failed: HTTP {resp.status} - {text[:200]}")
                    return {"error": f"HTTP {resp.status}", "query": query}
                
                data = await resp.json()
                logger.info(f"Context Bridge query OK: '{query[:50]}...' -> {len(data.get('personal', []))} personal, {len(data.get('knowledge', []))} knowledge items")
                return data
    except Exception as e:
        logger.error(f"Context Bridge query error: {e}")
        return {"error": str(e), "query": query}


async def get_enriched_context(
    agent_id: str = "nova-agent",
    include_goals: bool = True,
    include_relationships: bool = False,
) -> dict[str, Any]:
    """
    Get enriched context from Context Bridge.
    
    Returns:
    - identity: PIC identity profile
    - goals: Enriched with applicable LIAM frameworks
    - relevant_entities: Knowledge related to user's context
    - applicable_dimensions: Active LIAM dimensions
    - context_prompt: Pre-formatted for LLM consumption
    """
    url = f"{CONTEXT_BRIDGE_URL}/v1/context"
    body = {
        "agent_id": agent_id,
        "include_identity": True,
        "include_goals": include_goals,
        "include_relationships": include_relationships,
        "include_preferences": True,
        "include_knowledge_references": True,
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"Context Bridge context failed: HTTP {resp.status}")
                    return {"error": f"HTTP {resp.status}"}
                
                data = await resp.json()
                logger.info(f"Context Bridge enriched context OK: {len(data.get('goals', []))} goals, {len(data.get('relevant_entities', []))} entities")
                return data
    except Exception as e:
        logger.error(f"Context Bridge context error: {e}")
        return {"error": str(e)}


async def link_goal_to_entity(
    goal_id: str,
    entity_id: str,
    relevance: float = 0.5,
    context: str = "",
) -> dict[str, Any]:
    """
    Create bi-directional link between PIC goal and PCG Knowledge Graph entity.
    """
    url = f"{CONTEXT_BRIDGE_URL}/v1/link/goal-to-entity"
    params = {
        "goal_id": goal_id,
        "entity_id": entity_id,
        "relevance": relevance,
    }
    if context:
        params["context"] = context
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params=params,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"Context Bridge link failed: HTTP {resp.status}")
                    return {"linked": False, "error": f"HTTP {resp.status}"}
                
                data = await resp.json()
                logger.info(f"Context Bridge link OK: goal={goal_id} -> entity={entity_id}")
                return data
    except Exception as e:
        logger.error(f"Context Bridge link error: {e}")
        return {"linked": False, "error": str(e)}


async def get_goal_related_knowledge(goal_id: str) -> dict[str, Any]:
    """
    Get PCG Knowledge Graph entities related to a specific PIC goal.
    """
    url = f"{CONTEXT_BRIDGE_URL}/v1/goal/{goal_id}/related-knowledge"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                return await resp.json()
    except Exception as e:
        logger.error(f"Context Bridge goal-knowledge fetch error: {e}")
        return {"error": str(e)}
