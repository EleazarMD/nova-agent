import asyncio
import logging
import uuid
import time
import os
import json
from typing import Dict, List, Any
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn
import aiohttp
import aiosqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("simulator")

app = FastAPI(title="Nova MLOps Simulator", description="Persistent Test Suite and MLOps Control Plane")

NOVA_CHAT_URL = os.environ.get("NOVA_CHAT_URL", "http://127.0.0.1:18803/chat")
DB_PATH = "simulator_mlops.db"

TEST_LIBRARY = {
    "weather": [
        {"name": "Local Weather", "query": "What is the weather right now in Humble, TX?"},
        {"name": "Forecast", "query": "Give me the 3-day forecast for Chicago"}
    ],
    "workspace": [
        {"name": "Search", "query": "Search my workspace for project ideas."},
        {"name": "Create Canvas", "query": "Create a new Pi Workspace page with an image block and a mermaid diagram."}
    ],
    "tesla": [
        {"name": "Status", "query": "Check my Tesla battery status."},
        {"name": "Wake", "query": "Wake up the car."}
    ],
    "homelab": [
        {"name": "Service Check", "query": "Is the ai-gateway running?"},
        {"name": "Orchestrator", "query": "Check the health of the homelab infrastructure."}
    ],
    "adversarial": [
        {"name": "Empty Query", "query": " "},
        {"name": "SQL Injection Attempt", "query": "DROP TABLE users;"},
        {"name": "Long Query", "query": "What is AI? " * 100}
    ],
    "reasoning": [
        {"name": "Logic Puzzle", "query": "If I have 3 apples and eat 1, how many are left? Think step by step."}
    ],
    "stateful_multiturn": [
        {
            "name": "Weather Context Recall", 
            "queries": [
                "What is the weather in Tokyo?",
                "What about in New York?",
                "Which of those two is hotter right now?"
            ]
        },
        {
            "name": "Workspace Multi-step",
            "queries": [
                "Create a new Pi Workspace page called 'Trip Plan'.",
                "Now add a weather card for Miami to that page."
            ]
        }
    ],
    "long_horizon": [
        {
            "name": "Deep Context Journey",
            "queries": [
                "I'm planning a tech conference for homelab enthusiasts.",
                "Can you check if there are any Pi Workspace templates for event planning?",
                "Create a new Pi Workspace page named 'Homelab Con 2026'.",
                "Add a mermaid diagram block showing a 3-tier networking architecture for the event.",
                "What is the weather usually like in Austin, TX in November?",
                "Add that weather information as a text block to our 'Homelab Con 2026' page.",
                "Check the status of my Tesla, I might need to drive there.",
                "Wake the Tesla up if it's asleep.",
                "Remind me, what was the name of the workspace page we just created?",
                "What was the 3-tier architecture diagram about?"
            ]
        },
        {
            "name": "Neural Cache Training Sequence",
            "queries": [
                "Who was the 16th president of the US?",
                "What is the square root of 144?",
                "If I travel 60mph for 2 hours, how far do I go?",
                "Wait, who was the president I asked about earlier?",
                "Create a workspace page called 'Math & History'.",
                "Put the answers to my previous questions into that page.",
                "Check if the ai-gateway is running.",
                "Summarize everything we have talked about in this conversation."
            ]
        }
    ],
    "pcg_retrieval": [
        {"name": "Project Preferences", "query": "Hey Nova, check the PCG and tell me what my preferred tech stack is for new projects."},
        {"name": "Recent Interactions", "query": "Nova, what does my personal context graph say about my recent focus areas?"}
    ],
    "liam_framework": [
        {"name": "LIAM Analysis", "query": "Nova, I want you to analyze my recent homelab work using the LIAM framework. What insights do you have?"},
        {"name": "Strategic Planning", "query": "Based on the LIAM framework, how should I prioritize my upcoming tasks for the ecosystem?"}
    ],
    "research_hypothetical": [
        {"name": "Quantum Crypto", "query": "Hey Nova, can you research the potential effects of quantum computing on modern cryptography and summarize the top three threats?"},
        {"name": "Mars Terraforming", "query": "Nova, what are the current leading theoretical methods for terraforming Mars? Check recent research."}
    ],
    "voice_navigation": [
        {"name": "Navigate to Work", "query": "Hey Nova, send navigation to the nearest Starbucks to my Tesla."},
        {"name": "Waypoints", "query": "Nova, set up a route in the car to Austin, Texas with a stop at a Supercharger."}
    ],
    "voice_weather": [
        {"name": "Voice Weather Casual", "query": "Hey Nova, do I need an umbrella today in downtown Seattle?"},
        {"name": "Voice Weather Complex", "query": "Nova, what's the weather gonna be like in London this weekend, and should I pack a heavy coat?"}
    ],
    "long_form_voice": [
        {
            "name": "Three to Five Minute Voice Session",
            "queries": [
                "Hey Nova, let's do a 3 to 5 minute voice conversation. Keep each answer conversational, spoken, and about 80 to 100 words. Start by helping me think through homelab reliability, observability, and user experience.",
                "Now connect that to a realistic evening workflow with weather, Tesla navigation, PCG memory, and the LIAM framework. Stay concise, around 80 to 100 spoken words.",
                "My priority is fewer silent waits, better trust in factual claims, and more useful proactive help. Give me a warm final recap and a practical 3-step plan, around 120 spoken words."
            ]
        }
    ]
}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                categories TEXT,
                status TEXT,
                total_tests INTEGER,
                completed_tests INTEGER,
                started_at REAL,
                completed_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trials (
                trial_id TEXT PRIMARY KEY,
                run_id TEXT,
                category TEXT,
                test_name TEXT,
                query TEXT,
                status_code INTEGER,
                success BOOLEAN,
                response_text TEXT,
                tools_used INTEGER,
                latency_ms INTEGER,
                timestamp REAL,
                turn_index INTEGER DEFAULT 0,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            )
        """)
        
        # Migration for existing DB
        try:
            await db.execute("ALTER TABLE trials ADD COLUMN turn_index INTEGER DEFAULT 0")
        except Exception:
            pass
            
        await db.commit()

@app.on_event("startup")
async def startup_event():
    await init_db()

async def run_single_test(session: aiohttp.ClientSession, test: dict, run_id: str, category: str):
    queries = test.get("queries")
    if not queries:
        queries = [test.get("query")]
        
    conversation_id = f"sim-{run_id}-{uuid.uuid4().hex[:8]}"
    
    for turn_idx, query in enumerate(queries):
        start_time = time.time()
        payload = {
            "message": query,
            "user_id": "simulator-bot",
            "conversation_id": conversation_id,
            "stream": False
        }
        
        try:
            async with session.post(NOVA_CHAT_URL, json=payload, timeout=45) as resp:
                status = resp.status
                if status == 200:
                    data = await resp.json()
                    success = True
                    response_text = data.get("response", "")
                    tools = data.get("tool_calls") or []
                else:
                    success = False
                    response_text = await resp.text()
                    tools = []
        except Exception as e:
            status = 0
            success = False
            response_text = str(e)
            tools = []

        elapsed = time.time() - start_time
        latency_ms = int(elapsed * 1000)
        
        trial_id = str(uuid.uuid4())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO trials (trial_id, run_id, category, test_name, query, status_code, success, response_text, tools_used, latency_ms, timestamp, turn_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (trial_id, run_id, category, test["name"], query, status, success, response_text[:2000], len(tools), latency_ms, time.time(), turn_idx))
            await db.commit()
            
        await asyncio.sleep(1) # small delay between turns

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE runs SET completed_tests = completed_tests + 1 WHERE run_id = ?
        """, (run_id,))
        await db.commit()

    return True

async def execute_test_suite(run_id: str, categories: List[str] = None):
    tasks_to_run = []
    for cat, tests in TEST_LIBRARY.items():
        if categories and cat not in categories and "all" not in categories:
            continue
        for t in tests:
            tasks_to_run.append((cat, t))
            
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO runs (run_id, categories, status, total_tests, completed_tests, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (run_id, json.dumps(categories), "running", len(tasks_to_run), 0, time.time()))
        await db.commit()

    async with aiohttp.ClientSession() as session:
        for cat, test in tasks_to_run:
            await run_single_test(session, test, run_id, cat)
            await asyncio.sleep(1) # Prevent rate limiting
            
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE runs SET status = 'completed', completed_at = ? WHERE run_id = ?
        """, (time.time(), run_id))
        await db.commit()

async def execute_stress_test(run_id: str, volume: int = 100, concurrency: int = 15):
    """Zero-wait architecture stress test for neural cache training"""
    tasks_to_run = []
    base_tests = []
    
    # Gather all non-mutating/safe tests for high-volume stress testing
    for cat, tests in TEST_LIBRARY.items():
        for t in tests:
            base_tests.append((cat, t))
            
    import random
    for _ in range(volume):
        tasks_to_run.append(random.choice(base_tests))
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO runs (run_id, categories, status, total_tests, completed_tests, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (run_id, json.dumps(["stress_test_zero_wait"]), "running", len(tasks_to_run), 0, time.time()))
        await db.commit()

    semaphore = asyncio.Semaphore(concurrency)
    
    async def _run_bounded(session, test, cat):
        async with semaphore:
            # We don't sleep between turns here to truly stress the architecture
            return await run_single_test(session, test, run_id, cat)

    async with aiohttp.ClientSession() as session:
        coroutines = [_run_bounded(session, t, c) for c, t in tasks_to_run]
        await asyncio.gather(*coroutines)
            
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE runs SET status = 'completed', completed_at = ? WHERE run_id = ?
        """, (time.time(), run_id))
        await db.commit()

@app.get("/api/simulator/library")
async def get_library():
    return TEST_LIBRARY

@app.post("/api/simulator/run")
async def start_run(background_tasks: BackgroundTasks, payload: dict):
    categories = payload.get("categories", ["all"])
    run_id = str(uuid.uuid4())
    background_tasks.add_task(execute_test_suite, run_id, categories)
    return {"run_id": run_id, "status": "started"}

@app.post("/api/simulator/stress")
async def start_stress_run(background_tasks: BackgroundTasks, payload: dict):
    volume = payload.get("volume", 100)
    concurrency = payload.get("concurrency", 15)
    run_id = str(uuid.uuid4())
    background_tasks.add_task(execute_stress_test, run_id, volume, concurrency)
    return {"run_id": run_id, "status": "started", "type": "stress"}

@app.get("/api/simulator/runs")
async def get_all_runs():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 50") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

@app.get("/api/simulator/runs/{run_id}/trials")
async def get_run_trials(run_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trials WHERE run_id = ? ORDER BY timestamp ASC", (run_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

@app.get("/api/simulator/analytics/latency-graph")
async def get_latency_graph():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT category, test_name, AVG(latency_ms) as avg_latency 
            FROM trials 
            WHERE success = 1 
            GROUP BY category, test_name
        """) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

@app.get("/api/simulator/runs/{run_id}/audio-duration")
async def get_run_audio_duration(run_id: str, words_per_minute: int = 155):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT query, response_text, latency_ms
            FROM trials
            WHERE run_id = ?
            ORDER BY timestamp ASC
        """, (run_id,)) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
            
    user_words = sum(len((row.get("query") or "").split()) for row in rows)
    assistant_words = sum(len((row.get("response_text") or "").split()) for row in rows)
    total_words = user_words + assistant_words
    estimated_audio_seconds = round((total_words / max(words_per_minute, 1)) * 60, 1)
    total_backend_seconds = round(sum((row.get("latency_ms") or 0) for row in rows) / 1000, 1)
    
    return {
        "run_id": run_id,
        "turns": len(rows),
        "words_per_minute": words_per_minute,
        "user_words": user_words,
        "assistant_words": assistant_words,
        "total_words": total_words,
        "estimated_audio_seconds": estimated_audio_seconds,
        "estimated_audio_minutes": round(estimated_audio_seconds / 60, 2),
        "total_backend_seconds": total_backend_seconds,
        "target_met": 180 <= estimated_audio_seconds <= 300
    }

@app.get("/api/simulator/export/csv")
async def export_csv():
    """Export all trials as CSV for Jupyter/MLOps ingestion"""
    import io
    import csv
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["trial_id", "run_id", "category", "test_name", "turn_index", "query", "status_code", "success", "tools_used", "latency_ms", "timestamp"])
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT trial_id, run_id, category, test_name, COALESCE(turn_index, 0) as turn_index, query, status_code, success, tools_used, latency_ms, timestamp FROM trials ORDER BY timestamp DESC") as cursor:
            async for row in cursor:
                writer.writerow(row)
                
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trials_export.csv"})

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Nova MLOps Simulator</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body class="bg-gray-900 text-gray-100 p-8 font-sans">
        <div class="max-w-7xl mx-auto">
            <div class="flex justify-between items-center mb-8">
                <div>
                    <h1 class="text-4xl font-bold text-blue-400">Nova MLOps Simulator</h1>
                    <p class="text-gray-400 mt-2">Persistent testing suite, analytics, and data exports.</p>
                </div>
                <a href="/api/simulator/export/csv" class="bg-green-700 hover:bg-green-600 px-4 py-2 rounded text-white font-medium flex items-center gap-2">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
                    Export CSV for Jupyter
                </a>
            </div>
            
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-8 mb-8">
                <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 lg:col-span-1">
                    <h2 class="text-2xl font-semibold mb-4">Control Plane</h2>
                    <div class="flex flex-col gap-3">
                        <button onclick="runTests(['all'])" class="bg-blue-600 hover:bg-blue-700 p-3 rounded text-white font-medium text-left">▶ Run All Categories</button>
                        <button onclick="runTests(['weather'])" class="bg-gray-700 hover:bg-gray-600 p-2 rounded text-white border border-gray-600 text-left">Run Weather Suite</button>
                        <button onclick="runTests(['workspace'])" class="bg-gray-700 hover:bg-gray-600 p-2 rounded text-white border border-gray-600 text-left">Run Workspace Suite</button>
                        <button onclick="runTests(['tesla'])" class="bg-gray-700 hover:bg-gray-600 p-2 rounded text-white border border-gray-600 text-left">Run Tesla Suite</button>
                        <button onclick="runTests(['long_horizon'])" class="bg-indigo-900 hover:bg-indigo-800 p-2 rounded text-indigo-200 border border-indigo-700 text-left">Run Long-Horizon Sequence</button>
                        <button onclick="runTests(['long_form_voice'])" class="bg-cyan-900 hover:bg-cyan-800 p-2 rounded text-cyan-200 border border-cyan-700 text-left">Run 3-5 Min Voice Session</button>
                        <button onclick="runTests(['pcg_retrieval', 'liam_framework', 'research_hypothetical', 'voice_navigation', 'voice_weather'])" class="bg-teal-900 hover:bg-teal-800 p-2 rounded text-teal-200 border border-teal-700 text-left">Run Voice & Research Suite</button>
                        <button onclick="runTests(['stateful_multiturn'])" class="bg-purple-900 hover:bg-purple-800 p-2 rounded text-purple-200 border border-purple-700 text-left">Run Stateful Multi-turn Suite</button>
                        <button onclick="runStressTest()" class="bg-orange-600 hover:bg-orange-500 p-2 rounded text-white font-bold border border-orange-700 text-left mt-2 flex justify-between items-center">
                            <span>🔥 Zero-Wait Stress Test</span>
                            <span class="text-xs font-normal text-orange-200">100 Queries / 15 Threads</span>
                        </button>
                        <button onclick="runTests(['adversarial'])" class="bg-red-900 hover:bg-red-800 p-2 rounded text-red-200 border border-red-700 text-left">Run Adversarial Suite</button>
                    </div>
                </div>

                <div class="bg-gray-800 rounded-lg p-6 border border-gray-700 lg:col-span-2">
                    <h2 class="text-2xl font-semibold mb-4">Latency by Skill Category (ms)</h2>
                    <div class="h-64">
                        <canvas id="latencyChart"></canvas>
                    </div>
                </div>
            </div>

            <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                <h2 class="text-2xl font-semibold mb-4 flex justify-between items-center">
                    <span>Recent Trial Runs</span>
                    <button onclick="fetchRuns()" class="text-sm bg-gray-700 px-3 py-1 rounded hover:bg-gray-600">Refresh Data</button>
                </h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left border-collapse">
                        <thead>
                            <tr class="border-b border-gray-700 text-gray-400">
                                <th class="p-3">Run ID</th>
                                <th class="p-3">Status</th>
                                <th class="p-3">Categories</th>
                                <th class="p-3">Progress</th>
                                <th class="p-3">Date</th>
                            </tr>
                        </thead>
                        <tbody id="runs-table-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            async function runTests(categories) {
                const res = await fetch('/api/simulator/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({categories})
                });
                const data = await res.json();
                fetchRuns();
            }

            async function runStressTest() {
                const res = await fetch('/api/simulator/stress', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({volume: 100, concurrency: 15})
                });
                const data = await res.json();
                fetchRuns();
            }

            async function fetchRuns() {
                const res = await fetch('/api/simulator/runs');
                const runs = await res.json();
                const tbody = document.getElementById('runs-table-body');
                tbody.innerHTML = '';
                
                runs.forEach(run => {
                    const date = new Date(run.started_at * 1000).toLocaleString();
                    const statusColor = run.status === 'completed' ? 'text-green-400' : 'text-yellow-400';
                    const tr = document.createElement('tr');
                    tr.className = 'border-b border-gray-700 hover:bg-gray-750 transition cursor-pointer';
                    tr.innerHTML = `
                        <td class="p-3 font-mono text-sm">${run.run_id.split('-')[0]}...</td>
                        <td class="p-3 font-semibold ${statusColor}">${run.status.toUpperCase()}</td>
                        <td class="p-3 text-sm text-gray-300">${JSON.parse(run.categories).join(', ')}</td>
                        <td class="p-3">
                            <div class="w-full bg-gray-700 rounded-full h-2.5">
                                <div class="bg-blue-500 h-2.5 rounded-full" style="width: ${(run.completed_tests / run.total_tests) * 100}%"></div>
                            </div>
                            <div class="text-xs text-gray-400 mt-1">${run.completed_tests} / ${run.total_tests}</div>
                        </td>
                        <td class="p-3 text-sm text-gray-400">${date}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }

            async function renderChart() {
                const res = await fetch('/api/simulator/analytics/latency-graph');
                const data = await res.json();
                
                const labels = data.map(d => d.test_name);
                const latencies = data.map(d => d.avg_latency);
                
                const ctx = document.getElementById('latencyChart').getContext('2d');
                new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: 'Avg Latency (ms)',
                            data: latencies,
                            backgroundColor: 'rgba(59, 130, 246, 0.6)',
                            borderColor: 'rgba(59, 130, 246, 1)',
                            borderWidth: 1
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {
                            y: { beginAtZero: true, grid: { color: '#374151' }, ticks: { color: '#9CA3AF' } },
                            x: { grid: { display: false }, ticks: { color: '#9CA3AF' } }
                        },
                        plugins: { legend: { labels: { color: '#E5E7EB' } } }
                    }
                });
            }

            fetchRuns();
            renderChart();
            setInterval(fetchRuns, 3000);
        </script>
    </body>
    </html>
    """
    return html_content

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=18804)
