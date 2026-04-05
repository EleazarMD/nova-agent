"""
Web Search via Perplexity Sonar

This module provides web search functionality using Perplexity Sonar through AI Gateway.
Perplexity Sonar is the ONLY search engine used - it provides fast, grounded results
with citations.

Key Features:
- Fast search (2-5 seconds typical)
- Grounded results (no hallucination)
- Structured citations
- iOS integration for citation display

Citation Flow:
1. Query sent to AI Gateway with model="sonar"
2. AI Gateway routes to Perplexity API
3. Perplexity returns content + citations array
4. Nova extracts both content and citations
5. Citations sent to iOS via server message (type: "sources")
6. iOS displays citations in UI
"""

import asyncio
import aiohttp
from typing import Optional, Callable
from loguru import logger

# Will be set by tools.py
AI_GATEWAY_URL = "http://127.0.0.1:8777/api/v1"
AI_GATEWAY_API_KEY = "ai-gateway-api-key-2024"
_server_msg_fn: Optional[Callable] = None


async def handle_web_search(query: str) -> str:
    """
    Search the web using Perplexity Sonar via AI Gateway.
    
    Args:
        query: Search query string
        
    Returns:
        Search results with citation count appended
        
    Side Effects:
        - Sends citation URLs to iOS via _server_msg_fn
        - Logs search metrics
    """
    url = f"{AI_GATEWAY_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_GATEWAY_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "sonar",  # AI Gateway routes to Perplexity Sonar
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a research assistant. Provide concise, factual answers with "
                    "specific data points. Include source URLs when available. "
                    "If information is uncertain or unavailable, say so clearly."
                )
            },
            {"role": "user", "content": query},
        ],
        "max_tokens": 1024,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Web search HTTP {resp.status}: {text[:200]}")
                    return f"Search failed (HTTP {resp.status}). Try again or use openclaw_delegate."

                data = await resp.json()
                
                # Extract content from Perplexity response
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                
                # Extract citations array from Perplexity response
                citations = data.get("citations", [])

                if not content:
                    return "Search returned no results."

                # Send citations as structured server message for iOS UI
                if citations and _server_msg_fn:
                    source_items = [
                        {"index": i + 1, "url": u}
                        for i, u in enumerate(citations[:5])  # Limit to 5 for UI
                    ]
                    try:
                        await _server_msg_fn({
                            "type": "sources",
                            "query": query,
                            "citations": source_items,
                        })
                    except Exception as e:
                        logger.warning(f"Could not send sources message: {e}")

                # Append citation count to LLM response
                # iOS will display the actual URLs, so we just note they're available
                if citations:
                    content += f"\n\n({len(citations)} sources available — the user's device will display them.)"

                logger.info(f"Web search OK: {len(content)} chars, {len(citations)} citations")
                return content
                
    except asyncio.TimeoutError:
        return "Search timed out. Try a simpler query or use openclaw_delegate."
    except Exception as e:
        logger.error(f"Web search error: {e}")
        return f"Search error: {str(e)}"


def set_server_message_fn(fn: Callable):
    """Set the server message callback for sending citations to iOS."""
    global _server_msg_fn
    _server_msg_fn = fn


def set_config(gateway_url: str, api_key: str):
    """Set AI Gateway configuration."""
    global AI_GATEWAY_URL, AI_GATEWAY_API_KEY
    AI_GATEWAY_URL = gateway_url
    AI_GATEWAY_API_KEY = api_key
