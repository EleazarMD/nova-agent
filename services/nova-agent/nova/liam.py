"""
LIAM (Life Intelligence Augmentation Matrix) — Client for Nova Agent.

Direct access to PCG LIAM endpoints for framework discovery,
dimension querying, and decision-support context.

Backed by the PCG service on port 8765.
"""

import os
import aiohttp
from typing import Any, Optional
from loguru import logger

PCG_URL = os.environ.get("PCG_URL", "http://localhost:8765")
PCG_READ_KEY = os.environ.get("PCG_READ_KEY", "dev-read-key-change-in-prod")
PCG_ADMIN_KEY = os.environ.get("PCG_ADMIN_KEY", "dev-admin-key-change-in-prod")

_TIMEOUT = aiohttp.ClientTimeout(total=8)


def _read_headers() -> dict[str, str]:
    return {"X-PIC-Read-Key": PCG_READ_KEY}


def _admin_headers() -> dict[str, str]:
    return {
        "X-PIC-Admin-Key": PCG_ADMIN_KEY,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

async def list_dimensions(
    group: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List all LIAM dimensions, optionally filtered by group or status."""
    params = {}
    if group:
        params["group"] = group
    if status:
        params["status"] = status

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/liam/dimensions",
                params=params,
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"LIAM list_dimensions failed: {resp.status}")
                return []
    except Exception as e:
        logger.warning(f"LIAM list_dimensions error: {e}")
        return []


async def get_dimension(dimension_id: str) -> Optional[dict[str, Any]]:
    """Get a specific LIAM dimension by ID."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/liam/dimensions/{dimension_id}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"LIAM get_dimension failed: {resp.status}")
                return None
    except Exception as e:
        logger.warning(f"LIAM get_dimension error: {e}")
        return None


async def query_dimensions(
    problem_description: str,
    context: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Query which LIAM dimensions are applicable to a problem/decision."""
    body: dict[str, Any] = {"problem_description": problem_description}
    if context:
        body["context"] = context

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/liam/query/dimensions",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("applicable_dimensions", [])
                logger.warning(f"LIAM query_dimensions failed: {resp.status}")
                return []
    except Exception as e:
        logger.warning(f"LIAM query_dimensions error: {e}")
        return []


# ---------------------------------------------------------------------------
# Frameworks
# ---------------------------------------------------------------------------

async def list_frameworks(
    category: Optional[str] = None,
    dimension: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List all LIAM frameworks, optionally filtered by category or dimension."""
    params = {}
    if category:
        params["category"] = category
    if dimension:
        params["dimension"] = dimension

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/liam/frameworks",
                params=params,
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"LIAM list_frameworks failed: {resp.status}")
                return []
    except Exception as e:
        logger.warning(f"LIAM list_frameworks error: {e}")
        return []


async def get_framework(framework_id: str) -> Optional[dict[str, Any]]:
    """Get a specific LIAM framework by ID."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/liam/frameworks/{framework_id}",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"LIAM get_framework failed: {resp.status}")
                return None
    except Exception as e:
        logger.warning(f"LIAM get_framework error: {e}")
        return None


async def query_frameworks(
    problem_description: str,
    dimension_filter: Optional[list[str]] = None,
    limit: int = 5,
) -> dict[str, Any]:
    """
    Query which frameworks are applicable to a problem.

    Returns full framework details with relevance scores, ranked by relevance.
    This is the primary entry point for Nova's decision-support.
    """
    body: dict[str, Any] = {
        "problem_description": problem_description,
        "limit": limit,
    }
    if dimension_filter:
        body["dimension_filter"] = dimension_filter

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PCG_URL}/api/liam/query/frameworks",
                headers=_admin_headers(),
                json=body,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.warning(f"LIAM query_frameworks failed: {resp.status} {text[:200]}")
                return {"query": problem_description, "frameworks": [], "total_frameworks": 0}
    except Exception as e:
        logger.warning(f"LIAM query_frameworks error: {e}")
        return {"query": problem_description, "frameworks": [], "total_frameworks": 0}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def get_stats() -> dict[str, Any]:
    """Get LIAM statistics (dimension counts, framework counts, etc.)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PCG_URL}/api/liam/stats",
                headers=_read_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {}
    except Exception as e:
        logger.debug(f"LIAM stats skipped: {e}")
        return {}


# ---------------------------------------------------------------------------
# Decision support — high-level composite
# ---------------------------------------------------------------------------

async def get_decision_context(
    problem: str,
    context: Optional[str] = None,
) -> dict[str, Any]:
    """
    Get a complete decision-support context for a problem.

    Combines applicable dimensions + top frameworks with full knowledge
    into a single structured response for Nova's reasoning.
    """
    # Query dimensions
    applicable_dims = await query_dimensions(problem, context)

    # Extract dimension IDs for framework filtering
    dim_ids = []
    for d in applicable_dims:
        dim_obj = d.get("dimension", d)
        dim_id = dim_obj.get("id") if isinstance(dim_obj, dict) else None
        if dim_id:
            dim_ids.append(dim_id)

    # Query frameworks (with dimension filter if we found dimensions)
    fw_result = await query_frameworks(
        problem,
        dimension_filter=dim_ids if dim_ids else None,
        limit=5,
    )

    # Build structured response
    frameworks_with_content = []
    for rec in fw_result.get("frameworks", []):
        fw_data = rec.get("framework", {})
        if fw_data:
            frameworks_with_content.append({
                "name": fw_data.get("name", rec.get("framework_name")),
                "source": fw_data.get("source", ""),
                "category": fw_data.get("category", ""),
                "description": fw_data.get("description", ""),
                "when_to_use": fw_data.get("when_to_use", ""),
                "key_concepts": fw_data.get("key_concepts", []),
                "limitations": fw_data.get("limitations", ""),
                "applicable_dimensions": fw_data.get("applicable_dimensions", []),
                "relevance_score": rec.get("relevance_score", 0),
                "reasoning": rec.get("reasoning", ""),
            })
        else:
            # Fallback: framework content not loaded, use recommendation data
            frameworks_with_content.append({
                "name": rec.get("framework_name", ""),
                "source": "",
                "category": "",
                "description": "",
                "when_to_use": "",
                "key_concepts": rec.get("key_insights", []),
                "limitations": "",
                "applicable_dimensions": rec.get("applicable_dimensions", []),
                "relevance_score": rec.get("relevance_score", 0),
                "reasoning": rec.get("reasoning", ""),
            })

    return {
        "problem": problem,
        "applicable_dimensions": [
            {
                "id": (d.get("dimension", d).get("id") if isinstance(d.get("dimension", d), dict) else d.get("id", "")),
                "label": (d.get("dimension", d).get("label") if isinstance(d.get("dimension", d), dict) else d.get("label", "")),
                "relevance_score": d.get("relevance_score", 0),
                "reasoning": d.get("reasoning", ""),
            }
            for d in applicable_dims
        ],
        "applicable_frameworks": frameworks_with_content,
        "total_dimensions": len(applicable_dims),
        "total_frameworks": fw_result.get("total_frameworks", 0),
    }
