"""
server.py — Lightweight Relay Server for OmniPulse / DisputeFlow.
Handles static assets, REST authentication & ticketing, and a single-connection
voice WebSocket (supporting both AssemblyAI and local mock simulation).
"""

import asyncio
import json
import math
import os
from pathlib import Path
import struct
import time
from typing import Any, Dict
from typing import Optional
from model.auth_payload import AuthPayload
from model.ticket_payload import TicketPayload

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi import Response
from fastapi.staticfiles import StaticFiles
import websockets

from db import authenticate_user, register_user,create_ticket, get_latest_ticket_for_user, get_tickets
from ledger import TOOL_DEFINITIONS, execute_tool

app = FastAPI(title="OmniPulse Voice Support Server")

# Configuration
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
ASSEMBLYAI_VOICE_AGENT_URL = "wss://api.assemblyai.com/v2/voice-agent"
MOCK_MODE = True  # Set to False to stream directly to live AssemblyAI

# Paths
SERVER_DIR = Path(__file__).resolve().parent
ROOT_DIR = SERVER_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

# Audio constants (16kHz, 16-bit mono PCM)
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.1  # 100ms
CHUNK_SIZE = int(SAMPLE_RATE * 2 * CHUNK_DURATION)  # 3200 bytes
ENERGY_THRESHOLD = 700
SILENCE_TRIGGER = 1.0

# Mount static frontend assets
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ============================================================================
# 1. REST Endpoints (Auth & Tickets for new UI)
# ============================================================================

@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/auth/register")
async def api_register(payload: AuthPayload):
    res = register_user(payload.email, payload.name or payload.email.split("@")[0], payload.password)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@app.post("/api/auth/login")
async def api_login(payload: AuthPayload):
    res = authenticate_user(payload.email, payload.password)
    if not res["success"]:
        raise HTTPException(status_code=401, detail=res["error"])
    return res


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.post("/api/tickets")
async def api_create_ticket(payload: TicketPayload):
    ticket = create_ticket(
        user_email=payload.user_email,
        subject=payload.subject,
        description=payload.description,
        category=payload.category,
        priority=payload.priority,
    )
    return {"status": "created", **ticket}

@app.get("/api/tickets/list")
async def api_list_tickets(email: Optional[str] = None):
    tickets = get_tickets(email)
    return {"tickets": tickets}

@app.get("/api/tickets/latest")
async def api_get_latest_ticket(email: str):
    ticket = get_latest_ticket_for_user(email)
    return {"ticket": ticket}


# ============================================================================
# 2. Audio & Tool Dispatch Utilities
# ============================================================================

def calculate_rms(pcm_bytes: bytes) -> float:
    samples = struct.unpack(f"<{len(pcm_bytes) // 2}h", pcm_bytes)
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))


def generate_pcm_tone(duration: float = 1.5, freq: float = 240.0) -> bytes:
    """Generates clean 16kHz linear PCM audio for UI buffer testing."""
    samples = []
    for i in range(int(duration * SAMPLE_RATE)):
        envelope = min(1.0, i / 800) * min(1.0, (duration * SAMPLE_RATE - i) / 800)
        val = int(envelope * 12000 * math.sin(2 * math.pi * freq * (i / SAMPLE_RATE)))
        samples.append(val)
    return struct.pack(f"<{len(samples)}h", *samples)


async def handle_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    func = tool_call.get("function", {})
    name = func.get("name")
    args = func.get("arguments", "{}")
    call_id = tool_call.get("id", "call_default")

    result = execute_tool(name, args)
    return {"type": "tool_response", "call_id": call_id, "output": json.dumps(result)}

# ============================================================================
# 3. WebSocket Entrypoint (Mock or Production AssemblyAI)
# ============================================================================

SESSION_CONFIG = {
    "type": "session_init",
    "stt": {"model": "universal-3-pro", "keyterms_prompt": ["DisputeFlow", "Chargeback", "Uber", "TX-8921-AF"]},
    "agent": {
        "system_prompt": "You are Elena, an automated fraud assistant. Keep replies under 2 sentences. Use lookup_transaction before proposing freezes.",
        "tools": TOOL_DEFINITIONS,
    },
}


@app.websocket("/ws/voice")
async def voice_relay(client_ws: WebSocket):
    await client_ws.accept()

    # Production single-connection to AssemblyAI Voice Agent API
    try:
        async with websockets.connect(
            ASSEMBLYAI_VOICE_AGENT_URL, extra_headers={"Authorization": ASSEMBLYAI_API_KEY}
        ) as aai_ws:
            await aai_ws.send(json.dumps(SESSION_CONFIG))

            async def client_to_aai():
                while True:
                    msg = await client_ws.receive()
                    if "bytes" in msg and msg["bytes"]:
                        await aai_ws.send(msg["bytes"])
                    elif "text" in msg and msg["text"]:
                        await aai_ws.send(msg["text"])

            async def aai_to_client():
                async for message in aai_ws:
                    if isinstance(message, bytes):
                        await client_ws.send_bytes(message)
                    else:
                        payload = json.loads(message)
                        if payload.get("type") == "tool_call":
                            resp = await handle_tool_call(payload["tool_call"])
                            await aai_ws.send(json.dumps(resp))
                        await client_ws.send_text(message)

            await asyncio.gather(client_to_aai(), aai_to_client())

    except (WebSocketDisconnect, Exception) as e:
        print(f"[Session Closed]: {e}")
    finally:
        await client_ws.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)