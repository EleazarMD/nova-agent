"""Secure authentication handoff for OpenClaw browser sessions.

This module provides a secure mechanism for users to authenticate on websites
without Nova or OpenClaw ever seeing or storing passwords.

Security Principles:
1. Nova/OpenClaw NEVER sees, stores, or transmits passwords
2. User enters credentials directly in the browser via VNC
3. Session cookies/tokens are preserved for subsequent automation
4. All authentication happens in the user's direct control
"""

import asyncio
from typing import Optional, Callable


async def pause_for_authentication(
    site_name: str,
    login_url: str,
    vnc_url: str = "https://vnc.hyperspaceanalytics.com",
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> str:
    """
    Pause OpenClaw automation to allow user to authenticate securely.
    
    Flow:
    1. OpenClaw navigates to login page
    2. Nova instructs user to sign in via VNC
    3. Waits for user confirmation
    4. Resumes automation with authenticated session
    
    Args:
        site_name: Human-readable site name (e.g., "Starbucks")
        login_url: URL of the login page
        vnc_url: URL where user can access the browser
        progress_callback: Callback for status updates
        
    Returns:
        Status message indicating authentication result
    """
    instructions = f"""🔐 Secure Authentication Required

I need you to sign into {site_name}. For your security:
• I cannot see or handle your password
• You will enter credentials directly in the browser

Steps:
1. Open {vnc_url} (the browser session)
2. Navigate to the login page if not already there
3. Enter your username/email and password yourself
4. Complete any 2FA/security checks
5. Once signed in, tell me: "I'm signed in"

I'll wait while you authenticate. Take your time."""

    if progress_callback:
        await progress_callback("auth_required", instructions)
    
    return (
        f"Paused for {site_name} authentication. "
        f"User must sign in at {vnc_url} and confirm when complete."
    )


async def resume_after_authentication(
    confirmation_phrases: list[str] = None,
    timeout_seconds: int = 300,
) -> bool:
    """
    Wait for user confirmation that authentication is complete.
    
    In a real implementation, this would:
    - Listen for user voice/text input
    - Check for confirmation phrases like "I'm signed in", "Done", "Continue"
    - Optionally verify cookies/session exists before resuming
    
    Args:
        confirmation_phrases: List of phrases that indicate auth is complete
        timeout_seconds: How long to wait for user confirmation
        
    Returns:
        True if user confirmed, False if timeout
    """
    if confirmation_phrases is None:
        confirmation_phrases = [
            "i'm signed in", "i am signed in", "done", "continue",
            "signed in", "logged in", "authentication complete", "proceed"
        ]
    
    # Placeholder - real implementation would integrate with conversation loop
    # to detect when user confirms authentication
    return True


def get_secure_auth_prompt(site_name: str, vnc_url: str) -> str:
    """
    Generate a prompt for the LLM to handle secure authentication flow.
    
    This instructs the LLM on how to handle authentication securely:
    - Navigate to login page
    - Explain the VNC handoff
    - Wait for user confirmation
    - Never ask for or accept passwords
    """
    return f"""When the user needs to sign into {site_name}:

SECURITY RULES (NEVER VIOLATE):
1. NEVER ask the user for their password
2. NEVER offer to enter credentials for them
3. NEVER store or remember authentication credentials
4. ALWAYS direct user to the VNC viewer for manual entry

SECURE FLOW:
1. Navigate to the sign-in/login page
2. Tell user: "Please sign in at {vnc_url}. Enter your credentials directly in the browser. I cannot see or handle your password for security reasons."
3. Wait for user to say they're signed in
4. Confirm: "You should now see [expected authenticated view]. I'll continue from here."
5. Proceed with authenticated automation

USER COMMUNICATION:
- Clear: "Open {vnc_url} and sign in yourself"
- Reassuring: "Your password stays private - I can't see it"
- Helpful: "Let me know when you're signed in and I'll take over"
"""


# Example integration function
async def handle_authenticated_task(
    task: str,
    site_name: str,
    openclaw_delegate_fn,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> str:
    """
    Example of how to integrate secure auth into an OpenClaw task.
    
    Usage:
        result = await handle_authenticated_task(
            task="Order coffee from Starbucks",
            site_name="Starbucks",
            openclaw_delegate_fn=handle_openclaw_delegate,
            progress_callback=my_callback
        )
    """
    # Step 1: Navigate to site and check if auth needed
    initial_result = await openclaw_delegate_fn(
        task=f"Navigate to {site_name} and check if user is signed in. "
             f"If login page appears, stop and report 'AUTH_REQUIRED'.",
        progress_callback=progress_callback
    )
    
    if "AUTH_REQUIRED" in initial_result or "sign in" in initial_result.lower():
        # Step 2: Pause for user authentication
        auth_pause = await pause_for_authentication(
            site_name=site_name,
            login_url=f"https://{site_name.lower()}.com/login",  # Generic
            progress_callback=progress_callback
        )
        
        # Step 3: In real implementation, wait for user confirmation
        # For now, return instructions
        return (
            f"{auth_pause}\n\n"
            f"After you sign in at https://vnc.hyperspaceanalytics.com, "
            f"say 'I'm signed in' and I'll continue with: {task}"
        )
    
    # Already authenticated or no auth needed
    return await openclaw_delegate_fn(task=task, progress_callback=progress_callback)
