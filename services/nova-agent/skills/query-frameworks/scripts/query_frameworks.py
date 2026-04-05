#!/usr/bin/env python3
"""
Query Frameworks Script

Queries LIAM (Life Intelligence Augmentation Matrix) for scientific frameworks
applicable to a decision, problem, or life question.

Usage:
    python3 query_frameworks.py "Should I switch careers?"
    python3 query_frameworks.py "How to build habits?" --dimension habits
    python3 query_frameworks.py "Why is this delayed?" --category systems --limit 3
"""

import os
import sys
import json
import argparse
import asyncio
import aiohttp
from typing import Optional, List, Dict, Any


# Configuration
CONTEXT_BRIDGE_URL = os.environ.get("CONTEXT_BRIDGE_URL", "http://localhost:8764")
PIC_URL = os.environ.get("PIC_URL", "http://localhost:8765")
TIMEOUT = aiohttp.ClientTimeout(total=10)


async def query_frameworks_via_context_bridge(
    problem_description: str,
    dimension_id: Optional[str] = None,
    limit: int = 5
) -> Dict[str, Any]:
    """
    Query frameworks via Context Bridge (preferred method).
    Uses semantic search to find applicable frameworks.
    """
    url = f"{CONTEXT_BRIDGE_URL}/v1/query"
    body = {
        "query": problem_description,
        "include_personal": False,
        "include_knowledge": True,
        "include_dimensions": True,
        "max_results": limit,
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {
                        "error": f"Context Bridge query failed: HTTP {resp.status}",
                        "details": text[:200]
                    }
                
                data = await resp.json()
                
                # Extract frameworks from knowledge results
                frameworks = []
                for item in data.get("knowledge", []):
                    if item.get("type") == "framework":
                        frameworks.append(item)
                
                return {
                    "query": problem_description,
                    "applicable_frameworks": frameworks,
                    "dimensions_detected": data.get("applicable_dimensions", []),
                    "synthesis": data.get("synthesis", "")
                }
    except Exception as e:
        return {"error": f"Context Bridge error: {str(e)}"}


async def query_frameworks_via_pic(
    problem_description: str,
    dimension_id: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 5
) -> Dict[str, Any]:
    """
    Query frameworks directly from PIC/LIAM using semantic search.
    Falls back to dimension/category filtering if semantic search unavailable.
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Try semantic search endpoint first
            url = f"{PIC_URL}/api/pic/liam/frameworks/search"
            body = {
                "query": problem_description,
                "limit": limit
            }
            if category:
                body["category_filter"] = category
            if dimension_id:
                body["dimension_filter"] = dimension_id
            
            async with session.post(url, json=body, timeout=TIMEOUT) as resp:
                if resp.status == 200:
                    # Semantic search succeeded
                    search_results = await resp.json()
                    
                    if search_results:
                        frameworks = []
                        dimensions_detected = set()
                        
                        for result in search_results:
                            fw = result.get("framework", {})
                            frameworks.append({
                                "id": fw.get("id"),
                                "name": fw.get("name"),
                                "category": fw.get("category"),
                                "authors": fw.get("authors", []),
                                "when_to_use": fw.get("when_to_use"),
                                "key_concepts": fw.get("key_concepts", []),
                                "core_thesis": fw.get("core_thesis"),
                                "limitations": fw.get("limitations", []),
                                "applicable_dimensions": fw.get("applicable_dimension_ids", []),
                                "relevance_score": result.get("relevance_score", 0),
                                "match_reason": result.get("match_reason", "")
                            })
                            dimensions_detected.update(fw.get("applicable_dimension_ids", []))
                        
                        return {
                            "query": problem_description,
                            "applicable_frameworks": frameworks,
                            "dimensions_detected": list(dimensions_detected),
                            "synthesis": _generate_synthesis(problem_description, frameworks),
                            "search_method": "semantic"
                        }
            
            # Fallback: dimension or category filtering
            if dimension_id:
                url = f"{PIC_URL}/api/pic/liam/dimensions/{dimension_id}/frameworks"
                async with session.get(url, timeout=TIMEOUT) as resp:
                    if resp.status != 200:
                        return {"error": f"Dimension query failed: HTTP {resp.status}"}
                    frameworks = await resp.json()
            else:
                url = f"{PIC_URL}/api/pic/liam/frameworks"
                params = {}
                if category:
                    params["category"] = category
                
                async with session.get(url, params=params, timeout=TIMEOUT) as resp:
                    if resp.status != 200:
                        return {"error": f"Framework query failed: HTTP {resp.status}"}
                    frameworks = await resp.json()
            
            # Limit results
            frameworks = frameworks[:limit]
            
            # Format response
            return {
                "query": problem_description,
                "applicable_frameworks": [
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "category": f.get("category"),
                        "authors": f.get("authors", []),
                        "when_to_use": f.get("when_to_use"),
                        "key_concepts": f.get("key_concepts", []),
                        "core_thesis": f.get("core_thesis"),
                        "limitations": f.get("limitations", []),
                        "applicable_dimensions": f.get("applicable_dimension_ids", [])
                    }
                    for f in frameworks
                ],
                "dimensions_detected": [dimension_id] if dimension_id else [],
                "synthesis": _generate_synthesis(problem_description, frameworks),
                "search_method": "filtered"
            }
    except Exception as e:
        return {"error": f"PIC query error: {str(e)}"}


def _generate_synthesis(problem: str, frameworks: List[Dict]) -> str:
    """Generate a brief synthesis of how frameworks apply to the problem."""
    if not frameworks:
        return "No applicable frameworks found."
    
    if len(frameworks) == 1:
        fw = frameworks[0]
        return f"Apply {fw.get('name')}: {fw.get('when_to_use', '')}"
    
    names = [f.get("name") for f in frameworks[:3]]
    return f"Use multiple frameworks (Model Thinker approach): {', '.join(names)}. Each provides a different lens on the problem."


async def query_frameworks(
    problem_description: str,
    dimension_id: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 5,
    use_context_bridge: bool = True
) -> Dict[str, Any]:
    """
    Main entry point for framework querying.
    
    Args:
        problem_description: Natural language description of the problem
        dimension_id: Optional LIAM dimension filter
        category: Optional framework category filter
        limit: Maximum number of frameworks to return
        use_context_bridge: Try Context Bridge first (semantic search)
    
    Returns:
        Dictionary with applicable_frameworks, dimensions_detected, synthesis
    """
    # Try Context Bridge first (semantic search)
    if use_context_bridge:
        result = await query_frameworks_via_context_bridge(
            problem_description, dimension_id, limit
        )
        if "error" not in result and result.get("applicable_frameworks"):
            return result
    
    # Fallback to direct PIC query
    result = await query_frameworks_via_pic(
        problem_description, dimension_id, category, limit
    )
    
    return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Query LIAM for applicable frameworks"
    )
    parser.add_argument(
        "problem",
        help="Problem description (e.g., 'Should I switch careers?')"
    )
    parser.add_argument(
        "--dimension",
        help="Filter by LIAM dimension (e.g., 'habits', 'decision_fatigue')"
    )
    parser.add_argument(
        "--category",
        choices=["decision_making", "behavioral", "cognitive", "probabilistic", "computational", "systems"],
        help="Filter by framework category"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of frameworks to return (default: 5)"
    )
    parser.add_argument(
        "--no-context-bridge",
        action="store_true",
        help="Skip Context Bridge, query PIC directly"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON (default: formatted)"
    )
    
    args = parser.parse_args()
    
    # Run async query
    result = asyncio.run(query_frameworks(
        problem_description=args.problem,
        dimension_id=args.dimension,
        category=args.category,
        limit=args.limit,
        use_context_bridge=not args.no_context_bridge
    ))
    
    # Output
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # Formatted output
        if "error" in result:
            print(f"❌ Error: {result['error']}")
            if "details" in result:
                print(f"   {result['details']}")
            sys.exit(1)
        
        print(f"\n🔍 Query: {result.get('query', args.problem)}")
        print(f"\n📊 Found {len(result.get('applicable_frameworks', []))} applicable frameworks:\n")
        
        for i, fw in enumerate(result.get("applicable_frameworks", []), 1):
            print(f"{i}. {fw.get('name')} ({fw.get('category', 'unknown')})")
            if fw.get("authors"):
                print(f"   Authors: {', '.join(fw['authors'])}")
            print(f"   When to use: {fw.get('when_to_use', 'N/A')}")
            if fw.get("key_concepts"):
                concepts = fw["key_concepts"][:3]
                print(f"   Key concepts: {', '.join(concepts)}")
            print()
        
        if result.get("dimensions_detected"):
            print(f"🎯 Dimensions: {', '.join(result['dimensions_detected'])}")
        
        if result.get("synthesis"):
            print(f"\n💡 Synthesis:\n{result['synthesis']}")
        
        print()


if __name__ == "__main__":
    main()
