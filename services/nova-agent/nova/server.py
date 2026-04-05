"""
Nova Agent — FastAPI WebRTC signaling server.

Standalone runner for the Nova agent. Creates a FastAPI app that handles
WebRTC signaling (connect/patch/disconnect/health) and runs alongside
the webhook server.
"""

import asyncio
import json
import os
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequestHandler,
    SmallWebRTCRequest,
    SmallWebRTCPatchRequest,
    IceCandidate,
    ConnectionMode,
)

from nova.webhooks import app as webhook_app, WEBHOOK_PORT
from nova.text_chat import app as text_chat_app, TEXT_CHAT_PORT
from nova.mirror import app as mirror_app, MIRROR_PORT
from nova.tesla_client import start_tesla_client


def create_webrtc_app(run_bot_fn, ai_gateway_url: str, llm_model: str):
    """Create and configure the FastAPI WebRTC signaling app.
    
    Args:
        run_bot_fn: The async run_bot function to call on new connections.
        ai_gateway_url: URL of the AI Gateway for logging.
        llm_model: LLM model name for logging.
    
    Returns:
        Tuple of (webrtc_app, request_handler) for use in main().
    """
    webrtc_app = FastAPI(title="Nova Agent WebRTC")
    webrtc_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @webrtc_app.middleware("http")
    async def log_all_requests(request: Request, call_next):
        """Log all incoming requests for debugging."""
        logger.info(f"INCOMING: {request.method} {request.url.path} from {request.client.host}")
        response = await call_next(request)
        logger.info(f"RESPONSE: {request.method} {request.url.path} -> {response.status_code}")
        return response

    # WebRTC request handler - MULTIPLE mode allows concurrent connections
    # from DIFFERENT users (not multiple connections from same user)
    request_handler = SmallWebRTCRequestHandler(connection_mode=ConnectionMode.MULTIPLE)

    # Session enforcement: one active bot per user_id.
    # Key: user_id, Value: {"task": asyncio.Task, "pc_id": str}
    _active_bots: dict[str, dict] = {}

    @webrtc_app.post("/connect")
    async def connect(request: Request):
        """Handle WebRTC connection requests from Pipecat iOS SDK."""
        try:
            raw_body = await request.body()
            content_type = request.headers.get("content-type", "")
            logger.info(f"WebRTC connect: content-type={content_type}, body_len={len(raw_body)}")
            
            if not raw_body:
                session_id = str(uuid.uuid4())
                logger.info(f"Creating new session: {session_id}")
                return {"sessionId": session_id, "status": "ready"}
            
            body = json.loads(raw_body)
            logger.info(f"WebRTC connect body keys={list(body.keys())}")

            if "sdp" not in body:
                session_id = str(uuid.uuid4())
                logger.info(f"No SDP in request, returning session: {session_id}")
                return {"sessionId": session_id, "status": "ready"}

            # Separate custom app fields from WebRTC-required fields
            WEBRTC_KEYS = {"sdp", "type", "pc_id", "restart_pc", "request_data", "requestData"}
            app_data = {k: v for k, v in body.items() if k not in WEBRTC_KEYS}
            webrtc_body = {k: v for k, v in body.items() if k in WEBRTC_KEYS}

            # Merge app-level fields into request_data so run_bot can read them
            existing_rd = webrtc_body.get("request_data") or webrtc_body.pop("requestData", None) or {}
            if isinstance(existing_rd, dict):
                existing_rd.update(app_data)
            else:
                existing_rd = app_data
            webrtc_body["request_data"] = existing_rd

            webrtc_request = SmallWebRTCRequest.from_dict(webrtc_body)

            async def on_connection(connection):
                rd = webrtc_request.request_data or {}
                user_id = rd.get("user_id", "default")
                audio_mode = rd.get("audio_mode", "native")
                conversation_id = rd.get("conversation_id", "default")
                client_type = rd.get("client_type", "ios")  # "ios" or "dashboard"
                vehicle_id = rd.get("vehicle_id")  # Optional: for Tesla companion session binding

                # Session enforcement: tear down existing bot for this user
                if user_id in _active_bots:
                    prev = _active_bots.pop(user_id)
                    logger.warning(f"Replacing existing session for user={user_id} (old pc_id={prev.get('pc_id')})")
                    old_task = prev.get("task")
                    if old_task and not old_task.done():
                        old_task.cancel()
                    # Disconnect old peer connection so it stops sending frames
                    old_pc_id = prev.get("pc_id")
                    if old_pc_id and old_pc_id in request_handler._pcs_map:
                        try:
                            old_conn = request_handler._pcs_map.pop(old_pc_id)
                            await old_conn.disconnect()
                        except Exception as e:
                            logger.debug(f"Old connection cleanup: {e}")

                logger.info(f"Starting bot: user={user_id}, audio={audio_mode}, client={client_type}, conv={conversation_id}, vehicle={vehicle_id}")

                async def _run_and_cleanup():
                    try:
                        await run_bot_fn(connection, user_id, audio_mode, conversation_id, client_type, vehicle_id)
                    except asyncio.CancelledError:
                        logger.info(f"Bot task cancelled for user={user_id} (replaced by new session)")
                    finally:
                        _active_bots.pop(user_id, None)

                task = asyncio.create_task(_run_and_cleanup())
                pc_id = connection.pc_id if hasattr(connection, 'pc_id') else ""
                _active_bots[user_id] = {"task": task, "pc_id": pc_id}
                logger.info(f"Session registered: user={user_id}, pc_id={pc_id}")

            answer = await request_handler.handle_web_request(webrtc_request, on_connection)
            answer["sessionId"] = answer.get("pc_id", "")
            logger.info(f"WebRTC answer: pc_id={answer.get('pc_id')}")
            return answer
        except json.JSONDecodeError as e:
            logger.error(f"WebRTC connect JSON error: {e}, body={raw_body[:200]}")
            return {"error": "Invalid JSON", "detail": str(e)}
        except Exception as e:
            logger.error(f"WebRTC connect error: {e}")
            raise

    @webrtc_app.patch("/connect")
    async def patch_connect(request: Request):
        """Handle ICE candidate patches (iOS SDK uses PATCH /connect)."""
        try:
            body = await request.json()
            pc_id = body.get("pc_id", "")
            candidates = body.get("candidates", [])
            logger.info(f"ICE candidates for pc_id={pc_id}, count={len(candidates)}")
            
            ice_candidates = [
                IceCandidate(
                    candidate=c.get("candidate", ""),
                    sdp_mid=c.get("sdpMid", ""),
                    sdp_mline_index=c.get("sdpMLineIndex", 0),
                )
                for c in candidates
            ]
            patch_request = SmallWebRTCPatchRequest(pc_id=pc_id, candidates=ice_candidates)
            await request_handler.handle_patch_request(patch_request)
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"ICE candidate error: {e}")
            return {"error": str(e)}

    @webrtc_app.post("/ice-candidate")
    async def ice_candidate(request: Request):
        """Handle ICE candidate patches."""
        body = await request.json()
        candidates = [
            IceCandidate(
                candidate=c.get("candidate", ""),
                sdp_mid=c.get("sdpMid", ""),
                sdp_mline_index=c.get("sdpMLineIndex", 0),
            )
            for c in body.get("candidates", [])
        ]
        patch_request = SmallWebRTCPatchRequest(
            pc_id=body.get("pc_id", ""),
            candidates=candidates,
        )
        await request_handler.handle_patch_request(patch_request)
        return {"status": "ok"}

    @webrtc_app.post("/disconnect")
    async def disconnect(request: Request):
        """Explicitly disconnect a WebRTC session, clearing server-side state."""
        try:
            body = await request.json() if await request.body() else {}
            pc_id = body.get("pc_id", "")
            
            if pc_id and pc_id in request_handler._pcs_map:
                connection = request_handler._pcs_map[pc_id]
                await connection.disconnect()
                request_handler._pcs_map.pop(pc_id, None)
                # Cancel the bot task associated with this pc_id
                for uid, info in list(_active_bots.items()):
                    if info.get("pc_id") == pc_id:
                        task = info.get("task")
                        if task and not task.done():
                            task.cancel()
                        _active_bots.pop(uid, None)
                        logger.info(f"Cancelled bot for user={uid} via disconnect")
                        break
                logger.info(f"Disconnected session: {pc_id}")
                return {"status": "disconnected", "pc_id": pc_id}
            elif not pc_id:
                count = len(request_handler._pcs_map)
                for conn_id, conn in list(request_handler._pcs_map.items()):
                    try:
                        await conn.disconnect()
                    except Exception as e:
                        logger.warning(f"Error disconnecting {conn_id}: {e}")
                request_handler._pcs_map.clear()
                # Cancel all bot tasks
                for uid, info in list(_active_bots.items()):
                    task = info.get("task")
                    if task and not task.done():
                        task.cancel()
                _active_bots.clear()
                logger.info(f"Cleared all {count} connections and bot tasks")
                return {"status": "cleared", "count": count}
            else:
                return {"status": "not_found", "pc_id": pc_id}
        except Exception as e:
            logger.error(f"Disconnect error: {e}")
            return {"error": str(e)}

    @webrtc_app.get("/health")
    async def health():
        active_connections = len(request_handler._pcs_map)
        return {"status": "ok", "service": "nova-agent", "active_connections": active_connections}

    return webrtc_app, request_handler


async def run_server(run_bot_fn, ai_gateway_url: str, llm_model: str):
    """Run the Nova Agent WebRTC + Webhook + Text Chat servers."""
    from nova.store import init_db
    from nova.push import register_push_fallback

    logger.info(f"Nova Agent starting — LLM: {llm_model} via {ai_gateway_url}")
    await init_db()
    logger.info("Conversation store initialized (PIC for memory)")
    register_push_fallback()

    # Start Tesla SSE client for real-time vehicle location
    await start_tesla_client()
    logger.info("Tesla SSE client started (vehicle location tracking)")

    webrtc_app, _ = create_webrtc_app(run_bot_fn, ai_gateway_url, llm_model)
    webrtc_port = int(os.environ.get("NOVA_PORT", "18800"))

    webrtc_config = uvicorn.Config(
        webrtc_app,
        host="0.0.0.0",
        port=webrtc_port,
        log_level="info",
    )
    webrtc_server = uvicorn.Server(webrtc_config)

    webhook_config = uvicorn.Config(
        webhook_app,
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        log_level="info",
    )
    webhook_server = uvicorn.Server(webhook_config)

    text_chat_config = uvicorn.Config(
        text_chat_app,
        host="0.0.0.0",
        port=TEXT_CHAT_PORT,
        log_level="info",
    )
    text_chat_server = uvicorn.Server(text_chat_config)

    mirror_config = uvicorn.Config(
        mirror_app,
        host="0.0.0.0",
        port=MIRROR_PORT,
        log_level="info",
    )
    mirror_server = uvicorn.Server(mirror_config)

    logger.info(f"WebRTC: :{webrtc_port} | Webhooks: :{WEBHOOK_PORT} | Text Chat: :{TEXT_CHAT_PORT} | Mirror: :{MIRROR_PORT}")

    await asyncio.gather(
        webrtc_server.serve(),
        webhook_server.serve(),
        text_chat_server.serve(),
        mirror_server.serve(),
    )
