import pytest

from nova.turn_orchestrator import (
    TurnIntent,
    TurnState,
    decide_turn,
    execute_turn_plan_result,
)


@pytest.mark.asyncio
async def test_lookup_then_workspace_uses_one_cig_search_and_sets_pending_scribe():
    state = TurnState()
    plan = decide_turn(
        "Find the email from Natalie about World Cup and create workspace advisory pages.",
        state,
    )
    calls = []

    async def dispatch_tool(name, args):
        calls.append((name, args))
        return "matching email context"

    async def send_server_msg(_msg):
        pass

    async def persist_turn(_role, _content):
        pass

    result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

    assert plan.intent == TurnIntent.LOOKUP_THEN_WORKSPACE_CREATION
    assert result.handled is True
    assert result.tools_used == ["query_cig"]
    assert calls == [("query_cig", {"domain": "search", "query": "Find the email from Natalie about World Cup and"})]
    assert state.pending_scribe is True
    assert state.known_context == ["matching email context"]


@pytest.mark.asyncio
async def test_workspace_continuation_delegates_to_scribe_once():
    state = TurnState(active_goal="Create advisory pages", pending_scribe=True, known_context=["prior context"])
    plan = decide_turn("Single page advisories with all of the topics above.", state)
    calls = []

    async def dispatch_tool(name, args):
        calls.append((name, args))
        return "scribe accepted"

    async def send_server_msg(_msg):
        pass

    async def persist_turn(_role, _content):
        pass

    result = await execute_turn_plan_result(plan, state, dispatch_tool, send_server_msg, persist_turn)

    assert plan.intent == TurnIntent.WORKSPACE_CREATION_CONTINUATION
    assert result.handled is True
    assert result.tools_used == ["hub_delegate"]
    assert calls[0][0] == "hub_delegate"
    assert calls[0][1]["agent"] == "scribe"
    assert "prior context" in calls[0][1]["context"]
    assert state.pending_scribe is False


def test_pass_through_for_general_questions():
    state = TurnState()
    plan = decide_turn("What is int64?", state)

    assert plan.intent == TurnIntent.PASS_THROUGH
