"""
Hypothesis Generator for Nova Agent.

Generates fast initial responses from trained knowledge before tool execution.
Uses Minimax M2.5 for sub-500ms response time.
"""

import asyncio
from typing import Optional
from loguru import logger

from nova.tools import TOOL_DEFINITIONS


async def generate_hypothesis(
    user_query: str,
    conversation_history: list[dict],
    ai_gateway_url: str,
    api_key: str,
) -> tuple[str, float, list[str]]:
    """
    Generate a hypothesis response from trained knowledge.
    
    Args:
        user_query: The user's question/request
        conversation_history: Recent conversation context
        ai_gateway_url: AI Gateway base URL
        api_key: AI Gateway API key
        
    Returns:
        Tuple of (hypothesis_text, confidence, tools_needed)
    """
    import aiohttp
    
    # Build hypothesis generation prompt
    system_prompt = """You are Nova, a helpful AI assistant. Generate a CONCISE initial response that will be spoken immediately.

CRITICAL RULES:
1. Keep it SHORT (1 sentence max) - this will be spoken as TTS
2. Be SPECIFIC and actionable, not verbose reasoning
3. DO NOT explain your thought process or reasoning
4. DO NOT list multiple possibilities or options

GOOD EXAMPLES (make actual predictions from your knowledge):
- User: "What's the weather?" → "It's typically around 75-85°F in Houston this time of year."
- User: "Latest AI news?" → "OpenAI and Anthropic have been making major announcements recently."
- User: "Tesla status?" → "Your Model 3 should be fully charged by now based on your usual schedule."
- User: "What's Elon up to?" → "Elon's been focused on SpaceX's Starship program and xAI lately."

BAD EXAMPLES (useless placeholders - NEVER say these):
- "Let me look into that."
- "Let me check on that."
- "Let me search for that."
- "I'll find out for you."

BAD EXAMPLES (too verbose - also never say these):
- "The user is asking about 'Ola' - this could refer to several things..."
- "This is a significant geopolitical topic that would require..."

After your response, on a new line, list any tools you need:
TOOLS: [tool1, tool2, ...]

Available tools:
- get_weather: Current weather data
- web_search: Real-time web search
- check_studio: Calendar and email access
- tesla_vehicle_status: Tesla vehicle data
- get_time: Current time
- service_health_check: Homelab status

If no tools needed, write: TOOLS: []"""

    messages = [
        {"role": "system", "content": system_prompt},
        *conversation_history[-4:],  # Last 4 turns for context
        {"role": "user", "content": user_query}
    ]
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ai_gateway_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "minimax-m2.5",
                    "messages": messages,
                    "max_tokens": 50,  # Force brevity - 1 sentence max
                    "temperature": 0.5,  # Lower temp for more focused responses
                },
                timeout=aiohttp.ClientTimeout(total=10.0),  # Increased for calendar/productivity queries
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Hypothesis generation failed: {resp.status}")
                    return _fallback_hypothesis(user_query)
                
                data = await resp.json()
                response_text = data["choices"][0]["message"]["content"]
                
                # Parse response and tools
                hypothesis_text, tools = _parse_hypothesis_response(response_text)
                
                # If parsing failed (XML, verbose, etc.), use fallback
                if not hypothesis_text:
                    logger.warning("[Hypothesis] Parsing returned empty, using fallback")
                    return _fallback_hypothesis(user_query)
                
                # Estimate confidence based on tool needs
                confidence = 0.9 if not tools else 0.7
                
                logger.info(f"[Hypothesis] Generated: '{hypothesis_text[:60]}...' (tools={tools})")
                return hypothesis_text, confidence, tools
                
    except asyncio.TimeoutError:
        logger.warning("[Hypothesis] Generation timeout, using fallback")
        return _fallback_hypothesis(user_query)
    except Exception as e:
        logger.error(f"[Hypothesis] Generation error: {e}")
        return _fallback_hypothesis(user_query)


def _parse_hypothesis_response(response: str) -> tuple[str, list[str]]:
    """Parse hypothesis text and tool list from LLM response."""
    import re
    
    # CRITICAL: Filter out raw tool call XML that Minimax sometimes outputs
    # This breaks iOS TTS with SSML parse errors
    if "<minimax:tool_call>" in response or "<invoke" in response or "<tool_call>" in response:
        logger.warning(f"[Hypothesis] Filtered raw tool call XML from response")
        return "", []  # Return empty to trigger fallback
    
    # Filter out any XML-like tags that could break TTS
    if re.search(r'<[^>]+>', response):
        logger.warning(f"[Hypothesis] Filtered XML tags from response")
        # Try to extract just the text content
        response = re.sub(r'<[^>]+>', '', response).strip()
        if not response:
            return "", []
    
    lines = response.strip().split("\n")
    
    # Find TOOLS: line
    hypothesis_lines = []
    tools = []
    
    for line in lines:
        if line.startswith("TOOLS:"):
            # Extract tool list
            tools_str = line.replace("TOOLS:", "").strip()
            if tools_str and tools_str != "[]":
                # Parse [tool1, tool2] format
                tools_str = tools_str.strip("[]")
                tools = [t.strip() for t in tools_str.split(",") if t.strip()]
        else:
            hypothesis_lines.append(line)
    
    hypothesis_text = "\n".join(hypothesis_lines).strip()
    
    # Final validation: ensure hypothesis is speakable
    if not hypothesis_text or len(hypothesis_text) < 5:
        return "", tools
    
    # Check for verbose reasoning patterns and reject them
    # Also reject generic "Let me X" placeholders - they add no value
    verbose_patterns = [
        "The user is asking",
        "This could refer to",
        "This is a",
        "I should",
        "Let me think",
        "could be several things",
        "Let me look into",
        "Let me check",
        "Let me search",
        "Let me find",
    ]
    for pattern in verbose_patterns:
        if pattern.lower() in hypothesis_text.lower():
            logger.warning(f"[Hypothesis] Rejected verbose response: {hypothesis_text[:60]}...")
            return "", tools
    
    return hypothesis_text, tools


def _fallback_hypothesis(user_query: str) -> tuple[str, float, list[str]]:
    """
    Return empty hypothesis to skip speaking useless placeholders.
    
    The hypothesis system should only speak when it has ACTUAL useful predictions,
    not generic "Let me look into that" placeholders. If we can't generate a real
    prediction, stay silent and let the actual LLM response come through.
    """
    # Return empty string with 0 confidence - this will skip hypothesis speech
    # and go straight to the real LLM response
    return "", 0.0, []


def should_use_hypothesis_mode(user_query: str) -> bool:
    """
    Determine if hypothesis-validation mode should be used.
    
    Use hypothesis mode for queries that likely need real-time data validation.
    Skip for simple conversational queries.
    """
    query_lower = user_query.lower()
    
    # Use hypothesis mode for data queries
    data_keywords = [
        "weather", "temperature", "forecast",
        "tesla", "car", "vehicle", "charge",
        "email", "calendar", "meeting", "appointment",
        "search", "latest", "news", "current",
        "status", "health", "service",
        "time", "date", "when",
    ]
    
    if any(keyword in query_lower for keyword in data_keywords):
        return True
    
    # Skip for conversational queries
    conversational_patterns = [
        "hello", "hi", "hey", "thanks", "thank you",
        "good morning", "good afternoon", "good evening",
        "how are you", "what's up", "sup",
    ]
    
    if any(pattern in query_lower for pattern in conversational_patterns):
        return False
    
    # Default: use hypothesis mode for questions
    return "?" in user_query or any(q in query_lower for q in ["what", "when", "where", "who", "how", "why"])
