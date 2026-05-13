from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from loguru import logger


ServerMessageFn = Callable[[dict[str, Any]], Awaitable[None]]
PersistTurnFn = Callable[[str, str], Awaitable[None]]
SyncBackendFn = Callable[..., Awaitable[None]]


@dataclass
class VoiceTurnSnapshot:
    turn_id: str
    conversation_id: str
    session_id: str
    user_id: str
    raw_text: str = ""
    canonical_text: str = ""
    location: str = ""
    mode_policy: str = ""
    phase: str = "idle"
    llm_started: bool = False
    llm_text_chars: int = 0
    llm_error: str = ""
    tools_started: list[str] = field(default_factory=list)
    tools_completed: list[str] = field(default_factory=list)
    tools_failed: list[str] = field(default_factory=list)
    final_response_sent: bool = False
    turn_complete_sent: bool = False
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "raw_text": self.raw_text,
            "canonical_text": self.canonical_text,
            "location": self.location,
            "mode_policy": self.mode_policy,
            "phase": self.phase,
            "llm_started": self.llm_started,
            "llm_text_chars": self.llm_text_chars,
            "llm_error": self.llm_error,
            "tools_started": list(self.tools_started),
            "tools_completed": list(self.tools_completed),
            "tools_failed": list(self.tools_failed),
            "final_response_sent": self.final_response_sent,
            "turn_complete_sent": self.turn_complete_sent,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "latency_ms": int(((self.completed_at or time.time()) - self.started_at) * 1000),
        }


class VoiceTurnRuntime:
    def __init__(
        self,
        *,
        turn_id: str,
        conversation_id: str,
        session_id: str,
        user_id: str,
        send_server_msg: ServerMessageFn,
        persist_turn: PersistTurnFn,
        sync_backend: SyncBackendFn,
        model: str,
    ):
        self.snapshot = VoiceTurnSnapshot(
            turn_id=turn_id,
            conversation_id=conversation_id,
            session_id=session_id,
            user_id=user_id,
        )
        self._send_server_msg = send_server_msg
        self._persist_turn = persist_turn
        self._sync_backend = sync_backend
        self._model = model
        self._watchdog_task: Optional[asyncio.Task] = None

    async def emit_status(self, phase: str, message: str = "", *, tool: str = "", severity: str = "info") -> None:
        self.snapshot.phase = phase
        payload: dict[str, Any] = {
            "type": "turn_status",
            "turn_id": self.snapshot.turn_id,
            "phase": phase,
            "message": message,
            "severity": severity,
        }
        if tool:
            payload["tool"] = tool
        await self._send_server_msg(payload)

    async def heard_user(self, *, raw_text: str, canonical_text: str, location: str = "", mode_policy: str = "") -> None:
        self.snapshot.raw_text = raw_text
        self.snapshot.canonical_text = canonical_text
        self.snapshot.location = location
        self.snapshot.mode_policy = mode_policy
        await self.emit_status("heard_user", "I heard you.")
        await self.emit_status("understanding", "Understanding your request.")

    async def routed(self, route: str, tools_count: int = 0) -> None:
        await self.emit_status("routing", f"Routing as {route}; {tools_count} tools available.")

    async def tool_started(self, tool_name: str, message: str = "") -> None:
        self.snapshot.tools_started.append(tool_name)
        await self.emit_status("tool_selected", message or f"Using {tool_name}.", tool=tool_name)

    async def tool_running(self, tool_name: str, message: str) -> None:
        await self.emit_status("tool_running", message, tool=tool_name)

    async def tool_completed(self, tool_name: str, message: str = "") -> None:
        self.snapshot.tools_completed.append(tool_name)
        await self.emit_status("tool_completed", message or f"{tool_name} completed.", tool=tool_name)

    async def tool_failed(self, tool_name: str, message: str, *, severity: str = "error") -> None:
        self.snapshot.tools_failed.append(tool_name)
        await self.emit_status("tool_failed", message, tool=tool_name, severity=severity)

    def start_watchdog(self) -> None:
        self.cancel_watchdog()
        turn_id = self.snapshot.turn_id

        async def _watch() -> None:
            try:
                await asyncio.sleep(3)
                if self.snapshot.turn_id == turn_id and not self.snapshot.llm_text_chars and not self.snapshot.final_response_sent:
                    await self.emit_status("waiting_for_model", "I heard you. I’m waiting on the model to start responding.")
                await asyncio.sleep(5)
                if self.snapshot.turn_id == turn_id and not self.snapshot.llm_text_chars and not self.snapshot.final_response_sent:
                    await self.emit_status("model_slow", "This is taking longer than normal. I’m still working on it.", severity="warning")
                await asyncio.sleep(12)
                if self.snapshot.turn_id == turn_id and not self.snapshot.llm_text_chars and not self.snapshot.final_response_sent:
                    await self.emit_status("model_stalled", "The model is still delayed. I’ll keep the connection alive and finish when it returns.", severity="warning")
            except asyncio.CancelledError:
                pass

        self._watchdog_task = asyncio.create_task(_watch())

    def cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def llm_started(self) -> None:
        self.snapshot.llm_started = True
        await self.emit_status("model_started", "The model started responding.")

    async def append_llm_text(self, text: str) -> None:
        if not text:
            return
        first_text = self.snapshot.llm_text_chars == 0
        self.snapshot.llm_text_chars += len(text)
        if first_text:
            self.cancel_watchdog()
            await self.emit_status("responding", "Responding now.")

    async def llm_failed(self, error: str) -> None:
        self.cancel_watchdog()
        self.snapshot.llm_error = error or "Unknown model error"
        await self.emit_status("model_failed", "The model service failed before returning a response.", severity="error")

    async def complete_with_text(self, text: str, *, speech_text: str = "", result: str = "direct", suppress_speech: bool = True) -> None:
        if self.snapshot.final_response_sent:
            logger.warning(f"NOVA_VOICE_DUPLICATE_FINAL_SUPPRESSED | {self.snapshot.to_dict()}")
            return
        clean = (text or "").strip()
        if not clean:
            await self.complete_with_error("I heard you, but I did not receive a usable response. Please try that again.")
            return
        if self._looks_like_unfinished_progress_promise(clean):
            await self.complete_with_error(
                "I started to describe work I was going to do, but I do not have evidence that the lookup or action completed. I won't pretend that is done."
            )
            logger.warning(f"NOVA_VOICE_TURN_INCOMPLETE_PROMISE | {self.snapshot.to_dict()} text={clean[:300]!r}")
            return
        self.cancel_watchdog()
        self.snapshot.final_response_sent = True
        await self._persist_turn("assistant", clean)
        asyncio.create_task(self._sync_backend(
            self.snapshot.conversation_id,
            self.snapshot.user_id,
            "assistant",
            clean,
            model=self._model,
        ))
        await self._send_server_msg({
            "type": "validated",
            "text": clean,
            "speechText": speech_text or clean,
            "result": result,
            "suppressSpeech": suppress_speech,
        })
        await self.complete_turn()

    def _looks_like_unfinished_progress_promise(self, text: str) -> bool:
        normalized = " ".join((text or "").lower().split())
        if not normalized:
            return False
        if self.snapshot.tools_completed or self.snapshot.tools_failed:
            return False
        if len(normalized) > 520:
            return False
        result_claim_patterns = (
            r"\bfound (it|the|that|those)\b",
            r"\bi found\b",
            r"\bgot results\b",
            r"\bthe search confirms\b",
            r"\bsearch confirms\b",
            r"\bi pulled (up|the)\b",
            r"\bpulled (up|the)\b",
            r"\bthe logs show\b",
            r"\baccording to (your|our|the) (previous|prior|earlier)\b",
            r"\b(previous|prior|earlier) (conversation|thread)\b",
        )
        if any(re.search(pattern, normalized) for pattern in result_claim_patterns):
            return True
        promise_patterns = (
            r"\blet me (pull|check|get|look|search|find|open|create|update|build|draft|write|review)\b",
            r"\bi(?:'|')ll (pull|check|get|look|search|find|open|create|update|build|draft|write|review)\b",
            r"\bi will (pull|check|get|look|search|find|open|create|update|build|draft|write|review)\b",
            r"\bi(?:'|')m going to (pull|check|get|look|search|find|open|create|update|build|draft|write|review)\b",
            r"\bi am going to (pull|check|get|look|search|find|open|create|update|build|draft|write|review)\b",
        )
        if not any(re.search(pattern, normalized) for pattern in promise_patterns):
            return False
        completion_markers = (
            "here is",
            "here's",
            "i found",
            "i created",
            "i updated",
            "completed",
            "done",
            "result",
        )
        return not any(marker in normalized for marker in completion_markers)

    async def complete_with_structured_response(self, display_text: str, speech_text: str, *, result: str) -> None:
        if self.snapshot.final_response_sent:
            logger.warning(f"NOVA_VOICE_DUPLICATE_STRUCTURED_FINAL_SUPPRESSED | {self.snapshot.to_dict()}")
            return
        clean = (display_text or "").strip()
        self.cancel_watchdog()
        self.snapshot.final_response_sent = True
        if clean:
            await self._persist_turn("assistant", clean)
            asyncio.create_task(self._sync_backend(
                self.snapshot.conversation_id,
                self.snapshot.user_id,
                "assistant",
                clean,
                model=self._model,
            ))
        await self._send_server_msg({
            "type": "validated",
            "text": display_text,
            "speechText": speech_text or display_text,
            "result": result,
            "suppressSpeech": False,
        })
        await self.complete_turn()

    async def complete_with_error(self, message: str) -> None:
        if self.snapshot.final_response_sent:
            logger.warning(f"NOVA_VOICE_DUPLICATE_ERROR_FINAL_SUPPRESSED | {self.snapshot.to_dict()}")
            return
        fallback = message or "I heard you, but something failed before I could answer. Please try that again."
        self.cancel_watchdog()
        self.snapshot.final_response_sent = True
        await self._persist_turn("assistant", fallback)
        asyncio.create_task(self._sync_backend(
            self.snapshot.conversation_id,
            self.snapshot.user_id,
            "assistant",
            fallback,
            model=self._model,
            metadata={"source": "voice_turn_error_fallback", "llm_error": self.snapshot.llm_error},
        ))
        await self._send_server_msg({
            "type": "validated",
            "text": fallback,
            "speechText": fallback,
            "result": "turn_error",
            "suppressSpeech": False,
        })
        await self.complete_turn()

    async def complete_turn(self) -> None:
        if self.snapshot.turn_complete_sent:
            return
        self.cancel_watchdog()
        self.snapshot.turn_complete_sent = True
        self.snapshot.completed_at = time.time()
        await self._send_server_msg({"type": "turn_complete"})
        await self.emit_status("done", "Turn complete.")
        await self._send_server_msg({"phase": "done"})
        logger.info(f"NOVA_VOICE_TURN_TRACE | {self.snapshot.to_dict()}")

    async def emit_final_from_orchestrator(
        self,
        *,
        display_text: str,
        speech_text: str = "",
        result_label: str = "turn_orchestrator",
        suppress_speech: bool = False,
        card: dict | None = None,
    ) -> None:
        """Emit the single transport final for an orchestrator-handled turn.

        The orchestrator already persisted the assistant turn; this path
        does NOT persist again. It is the only authorized emitter of
        `validated` for orchestrator-handled turns.
        """
        if self.snapshot.final_response_sent:
            logger.warning(f"NOVA_VOICE_DUPLICATE_ORCHESTRATOR_FINAL_SUPPRESSED | {self.snapshot.to_dict()}")
            return
        clean = (display_text or "").strip()
        self.cancel_watchdog()
        self.snapshot.final_response_sent = True
        
        if card:
            try:
                await self._send_server_msg({
                    "type": "card",
                    "kind": card.get("kind", "generic"),
                    "tool": result_label,
                    "data": card,
                })
                logger.info(f"Emitted card ({card.get('kind')}) for orchestrator turn {result_label}")
            except Exception as e:
                logger.error(f"Failed to emit card from orchestrator: {e}")

        msg = {
            "type": "validated",
            "text": display_text,
            "speechText": speech_text or display_text,
            "result": result_label,
            "suppressSpeech": suppress_speech,
        }
        if card:
            msg["card"] = card
            msg["kind"] = card.get("kind", "generic")
        await self._send_server_msg(msg)
        if not clean:
            logger.warning(f"NOVA_VOICE_ORCHESTRATOR_FINAL_EMPTY | {self.snapshot.to_dict()}")
        await self.complete_turn()
