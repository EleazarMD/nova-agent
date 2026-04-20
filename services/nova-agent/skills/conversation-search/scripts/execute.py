#!/usr/bin/env python3
"""
Conversation Search - Standalone executable script
Searches Nova's conversation history in PostgreSQL and ChromaDB.
"""

import os
import sys
import json
import asyncio
import aiohttp
from datetime import datetime, timedelta

# Configuration from environment
ECOSYSTEM_URL = os.getenv("ECOSYSTEM_URL", "http://localhost:8404")
ECOSYSTEM_API_KEY = os.getenv("ECOSYSTEM_API_KEY", "ai-gateway-api-key-2024")
ECOSYSTEM_USER_ID = os.getenv("ECOSYSTEM_USER_ID", "dfd9379f-a9cd-4241-99e7-140f5e89e3cd")


async def search_conversations(
    query: str,
    days_back: int = 30,
    limit: int = 5,
    from_days: int = None,
    to_days: int = None,
) -> dict:
    """
    Search past conversations.
    
    Args:
        query: Search query
        days_back: How many days back to search (default 30)
        limit: Max results to return (default 5)
        from_days: Start of date range (days ago)
        to_days: End of date range (days ago)
    
    Returns:
        {
            "success": bool,
            "query": str,
            "results": [...],
            "total_results": int,
            "search_time_ms": int
        }
    """
    start_time = datetime.now()
    
    # Validate parameters
    days_back = min(max(1, days_back), 365)
    limit = min(max(1, limit), 20)
    
    # Build API request
    url = f"{ECOSYSTEM_URL}/api/memory/conversations/search"
    headers = {"X-API-Key": ECOSYSTEM_API_KEY}
    
    params = {
        "q": query,
        "user_id": ECOSYSTEM_USER_ID,
        "limit": limit,
    }
    
    # Date range handling
    if from_days is not None and to_days is not None:
        from_days = min(max(0, from_days), 365)
        to_days = min(max(0, to_days), 365)
        params["from_days"] = from_days
        params["to_days"] = to_days
    else:
        params["days_back"] = days_back
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    search_time = int((datetime.now() - start_time).total_seconds() * 1000)
                    
                    return {
                        "success": True,
                        "query": query,
                        "results": data.get("conversations", []),
                        "total_results": len(data.get("conversations", [])),
                        "search_time_ms": search_time,
                    }
                elif resp.status == 404:
                    return {
                        "success": False,
                        "query": query,
                        "message": f"No conversations found matching '{query}' in the specified date range",
                        "suggestion": "Try expanding the date range or using different search terms",
                    }
                else:
                    error_text = await resp.text()
                    return {
                        "success": False,
                        "error": f"Dashboard API returned HTTP {resp.status}",
                        "details": error_text[:200],
                    }
                    
    except aiohttp.ClientConnectionError:
        return {
            "success": False,
            "error": "Connection error",
            "message": f"Could not connect to Dashboard API at {ECOSYSTEM_URL}",
            "suggestion": "Check if ecosystem-dashboard is running on port 8404",
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "Timeout",
            "message": "Search took too long (>10s)",
            "suggestion": "Try a more specific query or smaller date range",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "type": type(e).__name__,
        }


def parse_args():
    """Parse command-line arguments."""
    if len(sys.argv) < 2:
        return {
            "error": "Missing required argument: query",
            "usage": "execute.py <query> [days_back=30] [limit=5] [from_days=N] [to_days=N]",
            "examples": [
                "execute.py 'homelab diagnostics'",
                "execute.py 'Tesla integration' days_back=90",
                "execute.py 'Hub delegation' from_days=7 to_days=0",
            ],
        }
    
    query = sys.argv[1]
    kwargs = {
        "days_back": 30,
        "limit": 5,
        "from_days": None,
        "to_days": None,
    }
    
    for arg in sys.argv[2:]:
        if "=" in arg:
            key, value = arg.split("=", 1)
            if key in kwargs:
                try:
                    kwargs[key] = int(value) if value.isdigit() else value
                except ValueError:
                    pass
    
    return {"query": query, **kwargs}


async def main():
    """Main entry point."""
    args = parse_args()
    
    if "error" in args:
        print(json.dumps(args, indent=2))
        sys.exit(1)
    
    result = await search_conversations(**args)
    print(json.dumps(result, indent=2))
    
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    asyncio.run(main())
