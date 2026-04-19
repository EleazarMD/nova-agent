"""
STAAR Tutor Tool — Calls the STAAR Tutor microservice API.

Provides TEKS-aligned problem generation, practice sessions,
answer submission, and student progress tracking.
"""

import os
import json
import httpx
from typing import Optional

STAAR_URL = os.environ.get("STAAR_TUTOR_URL", "http://localhost:8790")
STAAR_API_KEY = os.environ.get("STAAR_API_KEY", "staar-tutor-key-2024")

_HEADERS = {"X-API-Key": STAAR_API_KEY, "Content-Type": "application/json"}


async def handle_staar_tutor(
    action: str,
    grade: int = 4,
    count: int = 5,
    categories: Optional[list] = None,
    teks: Optional[list] = None,
    types: Optional[list] = None,
    student_name: Optional[str] = None,
    session_id: Optional[str] = None,
    problem_id: Optional[str] = None,
    answer: Optional[str] = None,
    difficulty: Optional[str] = None,
    seed: Optional[int] = None,
) -> str:
    """Handle staar_tutor tool calls."""

    if action == "generate":
        payload = {"grade": grade, "count": count}
        if categories:
            payload["categories"] = categories
        if teks:
            payload["teks"] = teks
        if types:
            payload["types"] = types
        if student_name:
            payload["student_name"] = student_name
        if difficulty:
            payload["difficulty"] = difficulty
        if seed is not None:
            payload["seed"] = seed

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{STAAR_URL}/v1/problems/generate",
                headers=_HEADERS,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        problems = data.get("problems", [])
        if not problems:
            return "No problems generated. Try different filters."

        # Format for display
        output_parts = []
        for i, p in enumerate(problems, 1):
            formatted = p.get("formatted", p["question"])
            teks_label = p.get("teks", "")
            skill = p.get("skill", "")
            pts = p.get("points", 1)
            header = f"Problem {i} — {skill} (TEKS {teks_label}"
            if pts > 1:
                header += f", {pts} points"
            header += ")"
            output_parts.append(f"{header}\n{formatted}")

        # Answer key
        answer_key = []
        for i, p in enumerate(problems, 1):
            answer_key.append(f"{i}. {p['correct_answer']}")
        output_parts.append("---\nAnswer Key\n" + "   ".join(answer_key))

        return "\n\n".join(output_parts)

    elif action == "create_session":
        payload = {"grade": grade, "count": count}
        if categories:
            payload["categories"] = categories
        if teks:
            payload["teks"] = teks
        if student_name:
            payload["student_name"] = student_name
        if difficulty:
            payload["difficulty"] = difficulty

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{STAAR_URL}/v1/sessions",
                headers=_HEADERS,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        session_id = data.get("session_id", "unknown")
        problems = data.get("problems", [])
        total_pts = data.get("total_points", 0)

        output_parts = [f"Session created: {session_id}"]
        output_parts.append(f"Student: {student_name or 'Anonymous'} | Grade: {grade} | Total points: {total_pts}")
        output_parts.append("")

        for i, p in enumerate(problems, 1):
            formatted = p.get("formatted", p["question"])
            teks_label = p.get("teks", "")
            skill = p.get("skill", "")
            header = f"Problem {i} — {skill} (TEKS {teks_label})"
            output_parts.append(f"{header}\n{formatted}")

        answer_key = []
        for i, p in enumerate(problems, 1):
            answer_key.append(f"{i}. {p['correct_answer']}")
        output_parts.append("---\nAnswer Key\n" + "   ".join(answer_key))

        return "\n\n".join(output_parts)

    elif action == "submit_answer":
        if not session_id or not problem_id or not answer:
            return "Error: session_id, problem_id, and answer are required for submit_answer"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{STAAR_URL}/v1/sessions/{session_id}/answer",
                headers=_HEADERS,
                json={"problem_id": problem_id, "answer": answer},
            )
            resp.raise_for_status()
            data = resp.json()

        correct = data.get("correct", False)
        correct_answer = data.get("correct_answer", "?")
        correct_value = data.get("correct_value", "")
        score = data.get("score", 0)
        total = data.get("total", 0)

        if correct:
            return f"✅ Correct! Score: {score}/{total}"
        else:
            result = f"❌ Not quite. The correct answer is {correct_answer}"
            if correct_value:
                result += f" ({correct_value})"
            result += f". Score: {score}/{total}"
            return result

    elif action == "get_progress":
        if not student_name:
            return "Error: student_name is required for get_progress"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{STAAR_URL}/v1/progress/{student_name}",
                headers=_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()

        progress = data.get("progress", [])
        if not progress:
            return f"No progress data found for {student_name}."

        output_parts = [f"Progress for {student_name}:"]
        for p in progress:
            teks = p.get("teks", "?")
            cat = p.get("category", "?")
            attempted = p.get("problems_attempted", 0)
            correct = p.get("problems_correct", 0)
            pct = (correct / attempted * 100) if attempted > 0 else 0
            output_parts.append(f"  TEKS {teks} (Cat {cat}): {correct}/{attempted} ({pct:.0f}%)")

        return "\n".join(output_parts)

    elif action == "list_teks":
        params = {}
        if categories:
            params["category"] = categories[0] if len(categories) == 1 else categories[0]

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{STAAR_URL}/v1/teks",
                headers=_HEADERS,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        teks_list = data.get("teks", [])
        if not teks_list:
            return "No TEKS standards found."

        output_parts = [f"TEKS Standards ({len(teks_list)} total):"]
        for t in teks_list:
            readiness = "Readiness" if t.get("readiness") else "Supporting"
            output_parts.append(f"  {t['id']} (Cat {t['category']}, {readiness}): {t['description']}")

        return "\n".join(output_parts)

    elif action == "list_categories":
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{STAAR_URL}/v1/categories",
                headers=_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()

        cats = data.get("categories", {})
        output_parts = ["STAAR Reporting Categories:"]
        for k, v in cats.items():
            output_parts.append(f"  {k}: {v['name']} — {v['description']} (Questions: {v['question_range']}, Points: {v['point_range']})")

        return "\n".join(output_parts)

    else:
        return f"Unknown action: {action}. Valid actions: generate, create_session, submit_answer, get_progress, list_teks, list_categories"
