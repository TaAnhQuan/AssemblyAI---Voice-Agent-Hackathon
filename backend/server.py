"""
server.py — Lightweight Relay Server for Wavelink Mobile Customer Care.
Handles static assets, REST authentication & ticketing, and a single-connection
voice WebSocket (supporting both AssemblyAI and local mock simulation).
"""

import asyncio
import base64
import json
import math
import os
from pathlib import Path
import struct
import sys
from typing import Any, Dict
from typing import Optional

# Line-buffer stdout so print() diagnostics (voice config, session errors) show
# up immediately in logs instead of sitting in a full buffer when redirected.
sys.stdout.reconfigure(line_buffering=True)
from model.auth_payload import AuthPayload
from model.ticket_payload import TicketPayload
from model.ticket_status_payload import TicketStatusPayload

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi import Response
from fastapi.staticfiles import StaticFiles
import websockets

from db import (
    authenticate_user, register_user, create_ticket, get_latest_ticket_for_user,
    get_ticket_by_code, get_tickets, update_ticket_status, save_transcript_line,
    get_transcripts_for_ticket,
)
from support_tools import TOOL_DEFINITIONS, execute_tool

load_dotenv()

app = FastAPI(title="Wavelink Mobile Voice Support Server")

# Configuration
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
ASSEMBLYAI_VOICE_AGENT_URL = os.getenv("ASSEMBLYAI_VOICE_AGENT_URL", "wss://agents.assemblyai.com/v1/ws")
# Runs against the real Voice Agent API whenever a key is configured; force the
# local simulator with VOICE_MOCK_MODE=true (handy for UI work without burning credits).
MOCK_MODE = (not ASSEMBLYAI_API_KEY) or os.getenv("VOICE_MOCK_MODE", "").lower() == "true"

print(
    f"[Voice Config] mode={'MOCK' if MOCK_MODE else 'LIVE'} "
    f"url={ASSEMBLYAI_VOICE_AGENT_URL} key_set={bool(ASSEMBLYAI_API_KEY)}"
)

# Paths
SERVER_DIR = Path(__file__).resolve().parent
ROOT_DIR = SERVER_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

# Audio constants — the Voice Agent API's audio/pcm encoding is 24kHz, 16-bit mono PCM
SAMPLE_RATE = 24000
CHUNK_DURATION = 0.1  # 100ms
CHUNK_SIZE = int(SAMPLE_RATE * 2 * CHUNK_DURATION)  # 4800 bytes
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
    # Every db.py call below runs synchronous sqlite3 I/O (and, for auth,
    # CPU-bound PBKDF2 hashing) — offloaded to a worker thread so it can't
    # block the single asyncio event loop that all concurrent WebSocket voice
    # sessions and HTTP requests share. Under concurrent load this is the
    # difference between one slow DB write stalling every open call and it
    # only stalling its own request.
    res = await asyncio.to_thread(
        register_user, payload.email, payload.name or payload.email.split("@")[0], payload.password
    )
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@app.post("/api/auth/login")
async def api_login(payload: AuthPayload):
    res = await asyncio.to_thread(authenticate_user, payload.email, payload.password)
    if not res["success"]:
        raise HTTPException(status_code=401, detail=res["error"])
    return res


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.post("/api/tickets")
async def api_create_ticket(payload: TicketPayload):
    ticket = await asyncio.to_thread(
        create_ticket,
        user_email=payload.user_email,
        subject=payload.subject,
        description=payload.description,
        category=payload.category,
        priority=payload.priority,
    )
    return ticket

@app.get("/api/tickets/list")
async def api_list_tickets(email: Optional[str] = None, status: Optional[str] = None):
    tickets = await asyncio.to_thread(get_tickets, email, status)
    return {"tickets": tickets}


@app.patch("/api/tickets/{ticket_id}/status")
async def api_update_ticket_status(ticket_id: str, payload: TicketStatusPayload):
    try:
        ticket = await asyncio.to_thread(update_ticket_status, ticket_id, payload.status)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    if ticket is None:
        raise HTTPException(status_code=404, detail=f"No ticket found with id '{ticket_id}'.")

    return ticket

@app.get("/api/tickets/latest")
async def api_get_latest_ticket(email: str):
    ticket = await asyncio.to_thread(get_latest_ticket_for_user, email)
    return {"ticket": ticket}


@app.get("/api/tickets/{ticket_id}/transcripts")
async def api_get_ticket_transcripts(ticket_id: str):
    transcripts = await asyncio.to_thread(get_transcripts_for_ticket, ticket_id)
    return {"transcripts": transcripts}


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


def build_voice_agent_tools(tool_names: Optional[list] = None) -> list:
    """Flattens support_tools.py's OpenAI-style tool schemas into the Voice Agent
    API's flat {type, name, description, parameters} shape. Pass tool_names to
    only expose a subset of what's implemented (e.g. billing tools aren't built
    yet, so Maya's tool_names only lists the network ones)."""
    tools = []
    for tool in TOOL_DEFINITIONS:
        fn = tool["function"]
        if tool_names is not None and fn["name"] not in tool_names:
            continue
        tools.append({
            "type": "function",
            "name": fn["name"],
            "description": fn["description"],
            "parameters": fn["parameters"],
            "execution_mode": "interactive",
            "timeout_seconds": 30,
        })
    return tools


# ============================================================================
# 3. WebSocket Entrypoint (Mock simulator or live AssemblyAI Voice Agent API)
# ============================================================================

# Turn-taking tuned for a natural phone-call feel: a bit snappier than the
# API's defaults (1000ms/3000ms) so the agent doesn't feel sluggish to
# respond, while still tolerant of a caller pausing mid-sentence, and with
# barge-in left on so the caller can always interrupt the agent.
TURN_DETECTION = {
    "vad_threshold": 0.5,
    "min_silence": 700,
    "max_silence": 3000,
    "interrupt_response": True,
}

# Single unified persona for Wavelink Mobile customer care — the product was
# narrowed from a 6-category, 6-persona support portal down to one telecom
# domain specifically to shrink the range of cases the voice agent has to
# handle. All 3 ticket categories route to the same agent; the category still
# exists for ticket organization/desk routing (see db.create_ticket) and
# leaves room to split personas again later if the domain grows.
# Voice IDs are from AssemblyAI's catalog: alba/eve/george/jane/jean/mary/michael
# (American), anna/charles/paul/vera (British).
MAYA_PERSONA = {
    "name": "Maya",
    "voice": "eve",
    "system_prompt": (
        "You are Maya, a friendly customer support agent for Wavelink Mobile. "
        "Keep replies under 2 sentences. Use check_network_status before "
        "troubleshooting a connectivity complaint, and confirm the account "
        "before using restart_connection. "
        "When the issue is resolved, the caller says goodbye, or there is "
        "nothing further you can help with, you must first tell the caller "
        "in that same reply that you're ending the call now (briefly summarize "
        "the resolution and say goodbye), and only then call the end_call tool. "
        "Never call end_call without first notifying the caller in speech."
    ),
    "greeting": "Hi, this is Maya from Wavelink Mobile support. How can I help with your service today?",
    "keyterms": ["SIM", "roaming", "data plan", "outage", "porting", "Wavelink"],
    # Billing/account tools land in a later pass — this starts with the
    # network/connectivity tools since that's the first increment requested.
    "tool_names": ["check_network_status", "restart_connection", "end_call"],
}

PERSONAS = {
    "Billing & Payments": MAYA_PERSONA,
    "Network & Service": MAYA_PERSONA,
    "Account & Plan": MAYA_PERSONA,
}
DEFAULT_PERSONA_CATEGORY = "Network & Service"


def resolve_persona(category: Optional[str]) -> Dict[str, Any]:
    return PERSONAS.get(category, PERSONAS[DEFAULT_PERSONA_CATEGORY])


def format_transcript_history(transcripts: list) -> str:
    """Renders prior transcript rows (role/text from db.get_transcripts_for_ticket)
    as a compact "Caller: ..." / "Maya: ..." log for prompt injection. Caps at
    the most recent 40 lines so a long-running ticket doesn't blow the prompt
    budget — the tail of the conversation is what matters for continuity."""
    if not transcripts:
        return ""
    recent = transcripts[-40:]
    lines = [f"{'Caller' if t['role'] == 'user' else 'Maya'}: {t['text']}" for t in recent]
    return "\n".join(lines)


def build_session_update(
    persona: Dict[str, Any],
    ticket: Optional[Dict[str, Any]] = None,
    prior_transcripts: Optional[list] = None,
) -> Dict[str, Any]:
    """Injects the caller's actual ticket (subject/description) and, if this
    isn't the first call on it, the prior conversation history into the
    persona's prompt and greeting — so Maya opens by restating the reported
    problem (first call) or picking up where the last call left off (repeat
    call), instead of starting from zero every time."""
    system_prompt = persona["system_prompt"]
    greeting = persona["greeting"]

    history_text = format_transcript_history(prior_transcripts or [])

    if history_text:
        subject = (ticket or {}).get("subject") or "their issue"

        system_prompt = (
            f"{system_prompt}\n\n"
            f"The caller has called before about this same ticket (subject: \"{subject}\"). "
            f"Here is the transcript of the previous conversation(s), oldest first:\n"
            f"---\n{history_text}\n---\n"
            f"Use this history as your memory of what was already discussed, tried, or "
            f"promised — do not ask the caller to repeat information already in it. Your "
            f"very first turn must greet the caller, briefly acknowledge you're following up "
            f"on the previous conversation, and ask if the issue is still happening or if "
            f"anything has changed, before continuing troubleshooting."
        )
        greeting = (
            f"Hi, this is {persona['name']} from Wavelink Mobile support, following up on "
            f"your earlier call about \"{subject}\". Is everything working now, or is the "
            f"issue still happening?"
        )
    elif ticket and (ticket.get("subject") or ticket.get("description")):
        subject = ticket.get("subject") or "an issue"
        description = ticket.get("description") or ""

        system_prompt = (
            f"{system_prompt}\n\n"
            f"The caller already submitted a support ticket before this call. "
            f"Ticket subject: \"{subject}\". "
            f"Ticket description: \"{description}\". "
            f"Your very first turn must greet the caller AND restate this problem in your "
            f"own words, then ask them to confirm it's still accurate or if anything has "
            f"changed. Do not start troubleshooting until they confirm."
        )
        greeting = (
            f"Hi, this is {persona['name']} from Wavelink Mobile support. "
            f"I see you reported: \"{subject}\" — {description}. "
            f"Is that still the issue you're having, or has anything changed?"
        )

    return {
        "type": "session.update",
        "session": {
            "system_prompt": system_prompt,
            "greeting": greeting,
            "input": {
                "format": {"encoding": "audio/pcm"},
                "keyterms": persona.get("keyterms", []),
                "turn_detection": TURN_DETECTION,
            },
            "output": {
                "voice": persona["voice"],
                "format": {"encoding": "audio/pcm"},
            },
            "tools": build_voice_agent_tools(persona["tool_names"]),
        },
    }


async def _next_client_audio_chunk(client_ws: WebSocket) -> Optional[bytes]:
    """Waits for the next raw PCM chunk from the browser. Returns None on disconnect."""
    msg = await client_ws.receive()
    if msg["type"] == "websocket.disconnect":
        return None
    return msg.get("bytes")


async def _handle_aai_event(
    raw: Any,
    client_ws: WebSocket,
    pending_tool_results: Dict[str, Any],
    aai_ws: Any,
    ticket_code: Optional[str],
    user_email: Optional[str],
    call_state: Dict[str, Any],
) -> None:
    """Translates one AssemblyAI Voice Agent event into the simplified wire
    format app.js understands. Every branch is defensive on purpose: a single
    malformed or unexpected-shape event must never take down the whole call —
    that used to force-close the browser socket the instant the agent started
    replying (i.e. right after the caller stopped talking), because a bare
    event["field"] lookup would raise KeyError and crash the relay task."""
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        return

    etype = event.get("type")

    try:
        if etype == "reply.audio":
            data = event.get("data")
            if data:
                await client_ws.send_bytes(base64.b64decode(data))

        elif etype == "transcript.user":
            text = event.get("text")
            if text:
                await client_ws.send_text(json.dumps({"type": "transcript", "role": "user", "text": text}))
                if ticket_code and user_email:
                    await asyncio.to_thread(save_transcript_line, ticket_code, user_email, "user", text)

        elif etype == "transcript.agent":
            text = event.get("text")
            if text:
                await client_ws.send_text(json.dumps({"type": "transcript", "role": "agent", "text": text}))
                if ticket_code and user_email:
                    await asyncio.to_thread(save_transcript_line, ticket_code, user_email, "agent", text)
            if event.get("interrupted"):
                await client_ws.send_text(json.dumps({"type": "interruption"}))

        elif etype == "tool.call":
            call_id = event.get("call_id")
            name = event.get("name")
            arguments = event.get("arguments", {})
            if not call_id or not name:
                return

            await client_ws.send_text(json.dumps({
                "type": "tool_call",
                "tool_call": {"id": call_id, "function": {"name": name, "arguments": arguments}},
            }))

            result = execute_tool(name, arguments)
            pending_tool_results[call_id] = result
            await client_ws.send_text(json.dumps({
                "type": "tool_response", "call_id": call_id, "output": result,
            }))

            # The agent must have already spoken its goodbye in this same
            # turn before calling end_call (enforced via the persona's
            # system_prompt) — the actual hangup only happens once this
            # turn's reply.done fires below, so that speech has fully
            # reached the browser first instead of being cut off.
            if name == "end_call":
                call_state["hangup_pending"] = True

        elif etype == "reply.done":
            # Voice Agent API requires tool.result to be sent only once the
            # turn that raised the tool.call has finished replying.
            if event.get("status") == "interrupted":
                pending_tool_results.clear()
                call_state["hangup_pending"] = False
            else:
                for call_id, result in pending_tool_results.items():
                    await aai_ws.send(json.dumps({
                        "type": "tool.result",
                        "call_id": call_id,
                        "result": json.dumps(result),
                    }))
                pending_tool_results.clear()

                if call_state.get("hangup_pending"):
                    call_state["should_close"] = True

        elif etype in ("session.error", "error"):
            print(f"[AssemblyAI Voice Agent Error]: {event}")
            await client_ws.send_text(json.dumps({
                "type": "voice_warning",
                "message": event.get("message") or event.get("code") or "AssemblyAI reported an error.",
            }))

        elif etype == "session.ended":
            print(f"[AssemblyAI Session Ended]: {event}")

    except Exception as exc:
        print(f"[Voice Event Handling Error] type={etype!r}: {exc}")


async def run_live_voice_agent(
    client_ws: WebSocket,
    persona: Dict[str, Any],
    ticket_code: Optional[str],
    user_email: Optional[str],
    ticket: Optional[Dict[str, Any]] = None,
    prior_transcripts: Optional[list] = None,
) -> None:
    """Relays audio/events between the browser and AssemblyAI's Voice Agent API,
    translating the wire protocol into the simpler shape app.js understands."""
    headers = {"Authorization": f"Bearer {ASSEMBLYAI_API_KEY}"}

    async with websockets.connect(ASSEMBLYAI_VOICE_AGENT_URL, additional_headers=headers) as aai_ws:
        await aai_ws.send(json.dumps(build_session_update(persona, ticket, prior_transcripts)))

        pending_tool_results: Dict[str, Any] = {}
        # Shared with _handle_aai_event: set when the agent calls end_call,
        # and flipped to should_close once that turn's reply.done confirms
        # the goodbye has fully been relayed to the browser.
        call_state: Dict[str, Any] = {"hangup_pending": False, "should_close": False}

        async def client_to_aai():
            while True:
                chunk = await _next_client_audio_chunk(client_ws)
                if chunk is None:
                    return  # browser hung up
                try:
                    await aai_ws.send(json.dumps({
                        "type": "input.audio",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }))
                except websockets.exceptions.ConnectionClosed:
                    return  # AssemblyAI ended the session from its side

        async def aai_to_client():
            agent_hung_up = False
            try:
                async for raw in aai_ws:
                    await _handle_aai_event(raw, client_ws, pending_tool_results, aai_ws, ticket_code, user_email, call_state)
                    if call_state.get("should_close"):
                        agent_hung_up = True
                        break
            except websockets.exceptions.ConnectionClosed:
                pass

            try:
                if agent_hung_up:
                    await client_ws.send_text(json.dumps({
                        "type": "session_ended",
                        "reason": "agent_hangup",
                    }))
                    # Actively close now instead of just returning — client_to_aai
                    # is still blocked awaiting more mic audio from the browser
                    # (which has no reason to stop sending on its own), so without
                    # this the call would hang open after the agent's goodbye.
                    await client_ws.close()
                else:
                    # AssemblyAI closed its side — tell the browser plainly
                    # instead of letting the socket just go dead with no
                    # explanation.
                    await client_ws.send_text(json.dumps({
                        "type": "session_ended",
                        "reason": "The AssemblyAI voice session ended.",
                    }))
            except Exception:
                pass

        await asyncio.gather(client_to_aai(), aai_to_client())


async def run_mock_voice_agent(
    client_ws: WebSocket,
    persona: Dict[str, Any],
    ticket_code: Optional[str],
    user_email: Optional[str],
    ticket: Optional[Dict[str, Any]] = None,
    prior_transcripts: Optional[list] = None,
) -> None:
    """Local simulator so the UI can be exercised without an AssemblyAI key.
    Greets the caller, then fires one canned tool-call/response cycle the first
    time it detects real microphone energy in the incoming audio (only for
    personas that actually have check_network_status)."""
    base_greeting = build_session_update(persona, ticket, prior_transcripts)["session"]["greeting"]
    greeting_text = f"{base_greeting} (mock mode — set ASSEMBLYAI_API_KEY to go live)"
    await client_ws.send_text(json.dumps({
        "type": "transcript",
        "role": "agent",
        "text": greeting_text,
    }))
    if ticket_code and user_email:
        await asyncio.to_thread(save_transcript_line, ticket_code, user_email, "agent", greeting_text)
    await client_ws.send_bytes(generate_pcm_tone())

    has_demo_tool = "check_network_status" in persona["tool_names"]
    demo_fired = False
    while True:
        chunk = await _next_client_audio_chunk(client_ws)
        if chunk is None:
            break
        if demo_fired or not has_demo_tool or not chunk:
            continue

        if calculate_rms(chunk) > ENERGY_THRESHOLD:
            demo_fired = True
            call_id = "mock-call-1"
            args = {"location": "Downtown"}

            await asyncio.sleep(0.4)
            await client_ws.send_text(json.dumps({
                "type": "tool_call",
                "tool_call": {"id": call_id, "function": {"name": "check_network_status", "arguments": args}},
            }))

            result = execute_tool("check_network_status", args)
            await asyncio.sleep(0.6)
            await client_ws.send_text(json.dumps({"type": "tool_response", "call_id": call_id, "output": result}))

            reply_text = "There's a known outage affecting voice and data in Downtown, ETA 45 minutes. Want me to text you when it's resolved?"
            await client_ws.send_text(json.dumps({
                "type": "transcript",
                "role": "agent",
                "text": reply_text,
            }))
            if ticket_code and user_email:
                await asyncio.to_thread(save_transcript_line, ticket_code, user_email, "agent", reply_text)
            await client_ws.send_bytes(generate_pcm_tone(duration=1.2, freq=300.0))


@app.websocket("/ws/voice")
async def voice_relay(client_ws: WebSocket):
    await client_ws.accept()

    category = client_ws.query_params.get("category")
    ticket_code = client_ws.query_params.get("ticket")
    user_email = client_ws.query_params.get("email")
    persona = resolve_persona(category)

    # Pull the actual ticket (subject/description) so the greeting/prompt can
    # reference the caller's real reported problem instead of a generic line.
    ticket = await asyncio.to_thread(get_ticket_by_code, ticket_code) if ticket_code else None
    # Pull every transcript line ever recorded against this ticket — from
    # this call's prior sessions — so the agent picks up with full context
    # instead of starting cold on a repeat call.
    prior_transcripts = await asyncio.to_thread(get_transcripts_for_ticket, ticket_code) if ticket_code else []
    print(
        f"[Voice Session] category={category!r} ticket={ticket_code!r} "
        f"has_ticket_context={bool(ticket)} prior_lines={len(prior_transcripts)} "
        f"-> persona={persona['name']!r} voice={persona['voice']!r}"
    )

    try:
        await client_ws.send_text(json.dumps({
            "type": "persona_info", "name": persona["name"], "voice": persona["voice"],
        }))

        if MOCK_MODE:
            print("[Voice Session] MOCK mode active (no ASSEMBLYAI_API_KEY or VOICE_MOCK_MODE=true).")
            await run_mock_voice_agent(client_ws, persona, ticket_code, user_email, ticket, prior_transcripts)
        else:
            await run_live_voice_agent(client_ws, persona, ticket_code, user_email, ticket, prior_transcripts)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[Voice Session Error]: {e}")
        try:
            await client_ws.send_text(json.dumps({
                "type": "session_ended",
                "reason": f"Voice session failed: {e}",
            }))
        except Exception:
            pass
    finally:
        try:
            await client_ws.close()
        except RuntimeError:
            pass  # already closed


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)