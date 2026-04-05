"""
Nova Agent ExoMind Handler

Manages long-running tasks, reminders, and follow-ups via ExoMind.
ExoMind is the specialist agent for background task orchestration.
"""

import os
import aiohttp
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from loguru import logger

ECOSYSTEM_URL = os.environ.get("ECOSYSTEM_URL", "http://localhost:8404")
ECOSYSTEM_USER_ID = os.environ.get("ECOSYSTEM_USER_ID", "eleazar")
INTERNAL_SERVICE_KEY = os.environ.get("INTERNAL_SERVICE_KEY", "")


async def handle_exomind(
    action: str,
    job_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    job_type: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    due_in: Optional[str] = None,
    reminder_at: Optional[str] = None,
    remind_in: Optional[str] = None,
    recurrence: Optional[str] = None,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    status_message: Optional[str] = None,
    context: Optional[str] = None,
    source_note_id: Optional[str] = None,
    limit: Optional[str] = None,
) -> str:
    """
    Handle ExoMind job management operations.
    
    Actions:
    - create: Create a new long-running job/task
    - list: List active jobs (with optional filters)
    - get: Get details of a specific job
    - update: Update job status/progress
    - complete: Mark a job as completed
    - cancel: Cancel a job
    - remind: Set a reminder for follow-up
    """
    base = ECOSYSTEM_URL
    headers = {
        "Content-Type": "application/json",
        "X-User-Id": ECOSYSTEM_USER_ID,
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
    }
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession() as session:
            # CREATE: Create a new job
            if action == "create":
                if not title:
                    return "Error: title is required to create a job."
                
                body: Dict[str, Any] = {
                    "title": title,
                    "description": description or "",
                    "job_type": job_type or "task",
                    "priority": priority or "medium",
                    "notify_on_complete": True,
                }
                
                # Handle relative due dates
                if due_in:
                    body["due_date"] = _parse_relative_time(due_in)
                elif due_date:
                    body["due_date"] = due_date
                
                # Handle relative reminders
                if remind_in:
                    body["reminder_at"] = _parse_relative_time(remind_in)
                elif reminder_at:
                    body["reminder_at"] = reminder_at
                
                if recurrence:
                    body["recurrence"] = recurrence
                if source_note_id:
                    body["source_note_id"] = source_note_id
                if context:
                    try:
                        import json
                        body["context"] = json.loads(context)
                    except:
                        body["context"] = {"raw": context}

                url = f"{base}/api/exomind/jobs"
                async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        logger.error(f"ExoMind API create error: {resp.status} - {text}")
                        return f"Failed to create job (HTTP {resp.status})."
                    
                    data = await resp.json()
                    result = f"✅ Job created: \"{data.get('title')}\"\n"
                    result += f"ID: {data.get('id')[:8]}...\n"
                    result += f"Type: {data.get('job_type')}, Priority: {data.get('priority')}"
                    
                    if data.get("due_date"):
                        result += f"\nDue: {data['due_date'][:10]}"
                    if data.get("reminder_at"):
                        result += f"\nReminder set"
                    
                    return result

            # LIST: List jobs with optional filters
            elif action == "list":
                params: Dict[str, str] = {}
                if job_type:
                    params["job_type"] = job_type
                if priority:
                    params["priority"] = priority
                if status:
                    params["status"] = status
                if limit:
                    params["limit"] = limit

                url = f"{base}/api/exomind/jobs"
                async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        return f"Failed to list jobs (HTTP {resp.status})."
                    
                    data = await resp.json()
                    jobs = data.get("jobs", [])
                    stats = data.get("stats", {})
                    
                    if not jobs:
                        return "No active jobs found."
                    
                    lines = [f"📋 {len(jobs)} active jobs:"]
                    
                    # Show stats summary
                    if stats.get("overdue", 0) > 0:
                        lines.append(f"⚠️ {stats['overdue']} overdue!")
                    
                    for job in jobs[:10]:
                        title_str = job.get("title", "Untitled")[:45]
                        jtype = job.get("job_type", "task")
                        prio = job.get("priority", "medium")
                        status_icon = {
                            "pending": "⏳",
                            "in_progress": "🔄",
                            "waiting_input": "⏸️",
                            "completed": "✅",
                            "failed": "❌",
                        }.get(job.get("status"), "📌")
                        
                        line = f"{status_icon} [{prio[:1].upper()}] {title_str}"
                        if job.get("due_date"):
                            due = job["due_date"][:10]
                            line += f" (due: {due})"
                        lines.append(line)
                    
                    return "\n".join(lines)

            # GET: Get a specific job
            elif action == "get":
                if not job_id:
                    return "Error: job_id is required for get action."
                
                url = f"{base}/api/exomind/jobs/{job_id}"
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Job {job_id} not found."
                    if resp.status != 200:
                        return f"Failed to get job (HTTP {resp.status})."
                    
                    job = await resp.json()
                    lines = [
                        f"📋 {job.get('title', 'Untitled')}",
                        f"Type: {job.get('job_type')} | Priority: {job.get('priority')} | Status: {job.get('status')}",
                    ]
                    
                    if job.get("description"):
                        lines.append(f"\n{job['description'][:300]}")
                    
                    if job.get("due_date"):
                        lines.append(f"Due: {job['due_date'][:16]}")
                    if job.get("progress", 0) > 0:
                        lines.append(f"Progress: {job['progress']}%")
                    if job.get("status_message"):
                        lines.append(f"Status: {job['status_message']}")
                    if job.get("result"):
                        lines.append(f"Result: {str(job['result'])[:200]}")
                    
                    return "\n".join(lines)

            # UPDATE: Update job status/progress
            elif action == "update":
                if not job_id:
                    return "Error: job_id is required for update action."
                
                body = {}
                if title:
                    body["title"] = title
                if description:
                    body["description"] = description
                if status:
                    body["status"] = status
                if progress is not None:
                    body["progress"] = progress
                if status_message:
                    body["status_message"] = status_message
                if priority:
                    body["priority"] = priority
                if due_in:
                    body["due_date"] = _parse_relative_time(due_in)
                elif due_date:
                    body["due_date"] = due_date
                if remind_in:
                    body["reminder_at"] = _parse_relative_time(remind_in)
                elif reminder_at:
                    body["reminder_at"] = reminder_at
                
                if not body:
                    return "Error: No fields to update."
                
                url = f"{base}/api/exomind/jobs/{job_id}"
                async with session.patch(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Job {job_id} not found."
                    if resp.status != 200:
                        return f"Failed to update job (HTTP {resp.status})."
                    
                    data = await resp.json()
                    return f"✅ Job updated: {data.get('title', 'Untitled')} - Status: {data.get('status')}"

            # COMPLETE: Mark job as completed
            elif action == "complete":
                if not job_id:
                    return "Error: job_id is required for complete action."
                
                body = {"status": "completed", "progress": 100}
                if status_message:
                    body["status_message"] = status_message
                
                url = f"{base}/api/exomind/jobs/{job_id}"
                async with session.patch(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Job {job_id} not found."
                    if resp.status != 200:
                        return f"Failed to complete job (HTTP {resp.status})."
                    
                    return f"✅ Job completed! A notification has been sent."

            # CANCEL: Cancel a job
            elif action == "cancel":
                if not job_id:
                    return "Error: job_id is required for cancel action."
                
                url = f"{base}/api/exomind/jobs/{job_id}"
                async with session.delete(url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 404:
                        return f"Job {job_id} not found."
                    if resp.status != 200:
                        return f"Failed to cancel job (HTTP {resp.status})."
                    
                    return f"✅ Job cancelled."

            # REMIND: Create a reminder job
            elif action == "remind":
                if not title:
                    return "Error: title is required for remind action."
                if not remind_in and not reminder_at:
                    return "Error: remind_in or reminder_at is required."
                
                body = {
                    "title": title,
                    "description": description or "",
                    "job_type": "reminder",
                    "priority": priority or "medium",
                    "notify_on_complete": True,
                }
                
                if remind_in:
                    body["reminder_at"] = _parse_relative_time(remind_in)
                    body["due_date"] = body["reminder_at"]
                elif reminder_at:
                    body["reminder_at"] = reminder_at
                    body["due_date"] = reminder_at
                
                if recurrence:
                    body["recurrence"] = recurrence
                if context:
                    try:
                        import json
                        body["context"] = json.loads(context)
                    except:
                        body["context"] = {"raw": context}

                url = f"{base}/api/exomind/jobs"
                async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                    if resp.status not in (200, 201):
                        return f"Failed to create reminder (HTTP {resp.status})."
                    
                    data = await resp.json()
                    remind_time = data.get("reminder_at", "")[:16] if data.get("reminder_at") else "scheduled"
                    return f"⏰ Reminder set: \"{title}\" at {remind_time}"

            else:
                return f"Unknown action: {action}. Available: create, list, get, update, complete, cancel, remind"

    except aiohttp.ClientError as e:
        logger.error(f"ExoMind API connection error: {e}")
        return f"Could not connect to ExoMind service: {str(e)}"
    except Exception as e:
        logger.error(f"ExoMind handler error: {e}")
        return f"ExoMind operation failed: {str(e)}"


def _parse_relative_time(relative: str) -> str:
    """Parse relative time strings like '2 hours', '3 days', 'tomorrow'."""
    now = datetime.now()
    relative = relative.lower().strip()
    
    # Handle common phrases
    if relative == "tomorrow":
        target = now + timedelta(days=1)
        target = target.replace(hour=9, minute=0, second=0, microsecond=0)
        return target.isoformat()
    elif relative == "next week":
        target = now + timedelta(days=7)
        return target.isoformat()
    elif relative == "end of day" or relative == "eod":
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()
    elif relative == "end of week" or relative == "eow":
        days_until_friday = (4 - now.weekday()) % 7
        if days_until_friday == 0 and now.hour >= 17:
            days_until_friday = 7
        target = now + timedelta(days=days_until_friday)
        target = target.replace(hour=17, minute=0, second=0, microsecond=0)
        return target.isoformat()
    
    # Parse "X units" format
    import re
    match = re.match(r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?", relative)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        
        if unit in ("minute", "min"):
            target = now + timedelta(minutes=amount)
        elif unit in ("hour", "hr"):
            target = now + timedelta(hours=amount)
        elif unit == "day":
            target = now + timedelta(days=amount)
        elif unit == "week":
            target = now + timedelta(weeks=amount)
        elif unit == "month":
            target = now + timedelta(days=amount * 30)
        else:
            target = now + timedelta(hours=1)
        
        return target.isoformat()
    
    # Default: 1 hour from now
    return (now + timedelta(hours=1)).isoformat()
