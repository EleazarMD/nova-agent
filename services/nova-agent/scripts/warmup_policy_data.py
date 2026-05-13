#!/usr/bin/env python3
import asyncio
import json
from nova.turn_orchestrator import TurnState, decide_turn, execute_turn_plan_result

QUERIES = [
    # Weather cluster
    "[User location: Humble, TX] What's the forecast for tomorrow?",
    "[User location: Humble, TX] Is it going to rain later?",
    "[User location: Humble, TX] How hot is it outside right now?",
    "[User location: Humble, TX] Do I need an umbrella today?",
    
    # Email/CIG cluster
    "Did Natalie email me about the project?",
    "Read my latest emails",
    "Find the email regarding the World Cup tickets",
    "Any new messages from work?",
    
    # Tesla cluster
    "Turn on the climate in the Tesla",
    "Lock the car",
    "How much battery does the Tesla have left?",
    "Start charging the Tesla",
    
    # IoT/Home cluster
    "Turn off the office lights",
    "Dim the living room lights to 50%",
    "Set the thermostat to 72 degrees",
    
    # General/Pass-through
    "What is the capital of Australia?",
    "How do you make a good espresso?",
    "Write a quick python script to reverse a string",
    
    # Corrections / Repeats (to trigger negative outcome labels on previous turns)
    "Repeat what you just said",
    "No, I meant the capital of Austria, not Australia",
]

async def mock_dispatch(name, args): 
    return json.dumps({"display": "mocked", "speech": "mocked"})

async def mock_send(msg): 
    pass

async def mock_persist(role, content): 
    pass

async def run_simulation():
    state = TurnState()
    print("Warming up Nova Turn Policy Database...")
    for q in QUERIES:
        print(f"Processing: {q}")
        plan = decide_turn(q, state)
        await execute_turn_plan_result(plan, state, mock_dispatch, mock_send, mock_persist)
        # Give the background embedding task time to complete
        await asyncio.sleep(0.5)
    print("\nWarmup complete!")

if __name__ == "__main__":
    asyncio.run(run_simulation())
