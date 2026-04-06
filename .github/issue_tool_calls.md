## Problem

The Pipecat pipeline is emitting tool call syntax (e.g., `[web_search query="..."]`) as raw text to the frontend instead of intercepting and executing these tool calls on the backend.

## Expected Behavior

When the LLM generates a tool call like `[web_search query="latest news"]`, the backend Pipecat pipeline should:
1. Intercept the tool call syntax
2. Execute the actual tool (web_search, get_weather, etc.)
3. Feed the tool results back to the LLM
4. Return the LLM's final response (with tool results incorporated) to the frontend

## Actual Behavior

The raw tool call syntax `[web_search query="..."]` is being sent to the frontend as plain text, where it appears in the UI and is spoken by TTS. The LLM then repeatedly attempts searches without receiving actual results.

## Frontend Logs

```
 Bot transcript: [web_search query="latest news"]
```

The frontend receives this literal string instead of processed search results.

## Root Cause Analysis

The issue appears to be a **Pipecat pipeline configuration gap** where:

1. **Tool definitions are loaded** (`TOOL_DEFINITIONS` in `tools.py` and `PIPECAT_TOOLS` in `bot.py`)
2. **Tool handlers are registered** (`llm.register_function()` calls in `bot.py`)
3. **Tools are set on context** (`context.set_tools(PIPECAT_TOOLS)` at line 373 in `bot.py`)
4. **BUT**: The LLM is still emitting bracket-syntax `[tool_name args]` instead of proper function_call format

This suggests either:
- The MiniMax M2.5 model doesn't natively support OpenAI-style function calling
- The Pipecat `OpenAILLMService` isn't properly configured to use the tools schema
- The LLM needs explicit prompting about tool format (system prompt may need updating)

## Configuration Details

**Current setup in `bot.py`:**
```python
llm = MiniMaxLLMService(
    api_key=AI_GATEWAY_API_KEY,
    base_url=AI_GATEWAY_URL,
    model=LLM_MODEL,
    params=OpenAILLMService.InputParams(
        temperature=0.1,
        max_tokens=8192,
    ),
    function_call_timeout_secs=600.0,
)

# Tools schema built from TOOL_DEFINITIONS
PIPECAT_TOOLS = _build_tools_schema()

# In pipeline setup:
context.set_tools(PIPECAT_TOOLS)

# Individual tool handlers registered:
llm.register_function("web_search", make_tool_handler("web_search"))
# ... (other tools)
```

## Possible Solutions

1. **Fix MiniMax function calling**: Verify if MiniMax M2.5 supports OpenAI-compatible `function_call` API or if it needs a different format

2. **Add tool-use prompting**: Update the system prompt to explicitly tell the LLM to use function_call format, not bracket syntax

3. **Pre-process LLM output**: Add a Pipecat processor that intercepts bracket-syntax `[tool_name(args)]` and converts to proper function calls

4. **Switch to native function calling**: Ensure `tools` parameter is passed to the LLM API call, not just set on context

## Request

Configure the Pipecat pipeline to properly execute tool calls instead of emitting them as raw text. The current configuration appears to have all the pieces (tool definitions, handlers, registration) but they're not being invoked properly.

## Related

- Similar issue mentioned in Pipecat docs: https://docs.pipecat.ai/guides/tools
- MiniMax API docs for function calling: need to verify compatibility

## Environment

- Model: MiniMax M2.5 via AI Gateway
- Pipecat version: (need to check)
- Tool definitions: Loaded from both hardcoded list and SKILL.md files
