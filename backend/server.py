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
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import Response
from fastapi.staticfiles import StaticFiles
import websockets

from db import (
    authenticate_user, register_user, create_ticket, get_latest_ticket_for_user,
    get_ticket_by_code, get_tickets, update_ticket_status, save_transcript_line,
    get_transcripts_for_ticket, request_human_callback, cancel_human_callback,
    get_account_by_email, get_session_user, delete_session,
)
from support_tools import TOOL_DEFINITIONS, execute_tool

load_dotenv()

app = FastAPI(title="Wavelink Mobile Voice Support Server")

# When the frontend is deployed separately from this backend (e.g. frontend
# on Vercel, backend on Railway), browsers enforce CORS on the REST calls —
# WebSocket connections aren't subject to CORS, but ARE subject to the
# Origin check FastAPI's WebSocket route can do if added later, so this is
# also the single place to widen that if needed. Comma-separated list of
# allowed origins; "*" (the default) is fine for a testing/demo deployment
# but should be narrowed to the actual Vercel URL before wider use.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
ASSEMBLYAI_VOICE_AGENT_URL = os.getenv("ASSEMBLYAI_VOICE_AGENT_URL", "wss://agents.assemblyai.com/v1/ws")
# Runs against the real Voice Agent API whenever a key is configured; force the
# local simulator with VOICE_MOCK_MODE=true (handy for UI work without burning credits).
MOCK_MODE = (not ASSEMBLYAI_API_KEY) or os.getenv("VOICE_MOCK_MODE", "").lower() == "true"

print(
    f"[Voice Config] mode={'MOCK' if MOCK_MODE else 'LIVE'} "
    f"url={ASSEMBLYAI_VOICE_AGENT_URL} key_set={bool(ASSEMBLYAI_API_KEY)} "
    # Raw repr of whatever this process's environment actually has for
    # VOICE_MOCK_MODE — makes a platform env-var mismatch (wrong
    # service/environment, stale deploy, unexpected value) provable from
    # the boot log instead of inferred from MOCK_MODE alone.
    f"raw_VOICE_MOCK_MODE={os.getenv('VOICE_MOCK_MODE')!r}"
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


async def get_current_user(authorization: Optional[str] = Header(None)) -> str:
    """Resolves the Authorization: Bearer <token> header to the user_email it
    was issued for (db.create_session at login/register). Every endpoint that
    reads or mutates a specific user's data depends on this instead of
    trusting a client-supplied email/ticket_id query param directly."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    token = authorization.split(" ", 1)[1].strip()
    user_email = await asyncio.to_thread(get_session_user, token)
    if not user_email:
        raise HTTPException(status_code=401, detail="Session expired or invalid — please log in again.")
    return user_email


async def _require_own_ticket(ticket_id: str, current_user: str) -> Dict[str, Any]:
    """Loads a ticket by code and 403s unless it belongs to current_user.
    Shared by every ticket-scoped endpoint below so status changes, human
    callback requests, and transcript reads can't be performed against
    someone else's ticket just by knowing/guessing its (sequential) code."""
    ticket = await asyncio.to_thread(get_ticket_by_code, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"No ticket found with id '{ticket_id}'.")
    if ticket.get("user_email", "").strip().lower() != current_user:
        raise HTTPException(status_code=403, detail="This ticket does not belong to the current user.")
    return ticket


@app.post("/api/auth/register")
async def api_register(payload: AuthPayload):
    # Every db.py call below runs synchronous sqlite3 I/O (and, for auth,
    # CPU-bound PBKDF2 hashing) — offloaded to a worker thread so it can't
    # block the single asyncio event loop that all concurrent WebSocket voice
    # sessions and HTTP requests share. Under concurrent load this is the
    # difference between one slow DB write stalling every open call and it
    # only stalling its own request.
    res = await asyncio.to_thread(
        register_user, payload.email, payload.name or payload.email.split("@")[0], payload.password, payload.phone_number
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


@app.post("/api/auth/logout")
async def api_logout(authorization: Optional[str] = Header(None)):
    # Best-effort: accepts a missing/already-invalid token without erroring
    # so the client can always clear its local session state.
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        await asyncio.to_thread(delete_session, token)
    return {"success": True}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.post("/api/tickets")
async def api_create_ticket(payload: TicketPayload, current_user: str = Depends(get_current_user)):
    # user_email is derived from the authenticated session, never trusted
    # from the request body, so a ticket can't be filed under someone else's
    # account by passing a different user_email in the payload.
    ticket = await asyncio.to_thread(
        create_ticket,
        user_email=current_user,
        subject=payload.subject,
        description=payload.description,
        category=payload.category,
        priority=payload.priority,
    )
    return ticket

@app.get("/api/tickets/list")
async def api_list_tickets(status: Optional[str] = None, current_user: str = Depends(get_current_user)):
    tickets = await asyncio.to_thread(get_tickets, current_user, status)
    return {"tickets": tickets}


@app.patch("/api/tickets/{ticket_id}/status")
async def api_update_ticket_status(ticket_id: str, payload: TicketStatusPayload, current_user: str = Depends(get_current_user)):
    await _require_own_ticket(ticket_id, current_user)
    try:
        ticket = await asyncio.to_thread(update_ticket_status, ticket_id, payload.status)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    if ticket is None:
        raise HTTPException(status_code=404, detail=f"No ticket found with id '{ticket_id}'.")

    return ticket

@app.post("/api/tickets/{ticket_id}/request-human")
async def api_request_human_callback(ticket_id: str, current_user: str = Depends(get_current_user)):
    await _require_own_ticket(ticket_id, current_user)
    ticket = await asyncio.to_thread(request_human_callback, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"No ticket found with id '{ticket_id}'.")
    return ticket

@app.post("/api/tickets/{ticket_id}/cancel-human")
async def api_cancel_human_callback(ticket_id: str, current_user: str = Depends(get_current_user)):
    await _require_own_ticket(ticket_id, current_user)
    ticket = await asyncio.to_thread(cancel_human_callback, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"No ticket found with id '{ticket_id}'.")
    return ticket


@app.get("/api/tickets/latest")
async def api_get_latest_ticket(current_user: str = Depends(get_current_user)):
    ticket = await asyncio.to_thread(get_latest_ticket_for_user, current_user)
    return {"ticket": ticket}


@app.get("/api/tickets/{ticket_id}/transcripts")
async def api_get_ticket_transcripts(ticket_id: str, current_user: str = Depends(get_current_user)):
    await _require_own_ticket(ticket_id, current_user)
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
        "Keep replies under 2 sentences.\n\n"
        "RULE — ask once, then act: ask for any single piece of information or "
        "confirmation at most once per call. The instant the caller answers it, "
        "treat it as settled and act — call the relevant tool in that same turn, "
        "never repeat the question, and never say you are about to do something "
        "without actually doing it in that same turn.\n\n"
        "Tools:\n"
        "- check_network_status: use for a connectivity complaint happening right now.\n"
        "- check_incident_history: use when the caller asks about a past/earlier outage "
        "(e.g. 'yesterday's outage') instead of a live issue — if it's already resolved, "
        "say so plainly and share the summary.\n"
        "- restart_connection: needs the caller's phone number. If you already have it "
        "(given as context below, or stated earlier this call), call the tool immediately — "
        "do not ask for it again. Otherwise ask for it once, then call the tool as soon as "
        "they answer.\n"
        "- end_call: only after you have already told the caller, in that same reply, that "
        "the issue is resolved (or you can't help further) and said goodbye — never call it "
        "silently."
    ),
    "greeting": "Hi, this is Maya from Wavelink Mobile support. How can I help with your service today?",
    "keyterms": ["SIM", "roaming", "data plan", "outage", "porting", "Wavelink"],
    # Billing/account tools land in a later pass — this starts with the
    # network/connectivity tools since that's the first increment requested.
    "tool_names": ["check_network_status", "check_incident_history", "restart_connection", "end_call"],
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
    account: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Injects the caller's actual ticket (subject/description) and, if this
    isn't the first call on it, the prior conversation history into the
    persona's prompt and greeting — so Maya opens by restating the reported
    problem (first call) or picking up where the last call left off (repeat
    call), instead of starting from zero every time. Also injects the
    caller's phone number/account (when known) so Maya already has it for
    restart_connection instead of having to ask for it on every call."""
    system_prompt = persona["system_prompt"]
    greeting = persona["greeting"]

    # Context blocks below state facts for Maya to use — the "ask once, then
    # act" rule already lives once in the base persona prompt above, so
    # these deliberately don't restate it; piling redundant copies of the
    # same rule into the prompt bloats it and risks the model getting stuck
    # rather than following it (observed empty replies from AssemblyAI when
    # the prompt got large and repetitive during earlier iterations of this
    # function).
    if account and account.get("phone_number"):
        location_note = (
            f" Their line is associated with {account['location']}."
            if account.get("location") else ""
        )
        system_prompt = (
            f"{system_prompt}\n\n"
            f"CONTEXT — caller's account: phone {account['phone_number']}, "
            f"name {account.get('customer_name', 'unknown')}, plan {account.get('plan', 'unknown')}, "
            f"5G enabled: {account.get('5g_enabled')}.{location_note} "
            f"This is already on file — do not ask the caller for their phone number."
        )

    history_text = format_transcript_history(prior_transcripts or [])

    if history_text:
        subject = (ticket or {}).get("subject") or "their issue"

        system_prompt = (
            f"{system_prompt}\n\n"
            f"CONTEXT — this caller has called before about ticket \"{subject}\". "
            f"Prior conversation (oldest first), your memory of what was already "
            f"discussed or promised — do not ask the caller to repeat anything in it:\n"
            f"---\n{history_text}\n---\n"
            f"Your first turn: greet them, mention you're following up, and ask if the "
            f"issue is still happening or anything's changed. As soon as they answer that "
            f"(e.g. 'yes it's still happening' or 'can you check it'), immediately act in "
            f"that same reply — call check_network_status for their area if you don't "
            f"already know the status, or move straight to troubleshooting. Do not just "
            f"acknowledge their answer; always follow it with a concrete next step or tool "
            f"call in the same turn."
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
            f"CONTEXT — caller's filed ticket: subject \"{subject}\", description "
            f"\"{description}\". Your first turn: greet them, restate this problem in "
            f"your own words, and ask them to confirm it's still accurate or if "
            f"anything's changed. As soon as they confirm, immediately act in that same "
            f"reply — call the relevant tool (check_network_status, restart_connection, "
            f"etc.) rather than just acknowledging their answer. Never leave a confirmed "
            f"problem without a concrete next step or tool call in the same turn."
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


# Bounded retries for the stuck-turn safety net below — high enough to
# recover from an occasional dropped turn, low enough that a genuinely
# broken session gives up and tells the caller instead of nudging forever.
STUCK_TURN_MAX_RETRIES = 2


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
        if etype == "reply.started":
            call_state["reply_had_content"] = False
            call_state["tool_called_this_turn"] = False

        elif etype in ("reply.audio", "transcript.agent.delta"):
            call_state["reply_had_content"] = True
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
            call_state["tool_called_this_turn"] = True
            call_id = event.get("call_id")
            name = event.get("name")
            arguments = event.get("arguments", {})
            print(f"[Tool Call] name={name!r} call_id={call_id!r} arguments={arguments!r}")
            if not call_id or not name:
                print(f"[Tool Call] dropped — missing call_id or name in event: {event!r}")
                return

            await client_ws.send_text(json.dumps({
                "type": "tool_call",
                "tool_call": {"id": call_id, "function": {"name": name, "arguments": arguments}},
            }))

            result = execute_tool(name, arguments)
            pending_tool_results[call_id] = result
            print(f"[Tool Call] executed name={name!r} call_id={call_id!r} result={result!r}")
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
            had_content = call_state.get("reply_had_content", True)
            print(
                f"[Reply Done] status={event.get('status')!r} "
                f"pending_tool_results={list(pending_tool_results.keys())!r} had_content={had_content}"
            )
            # Stuck-turn safety net: AssemblyAI's Voice Agent API exposes no
            # tool_choice/forced-tool-use or model-tuning knobs (confirmed
            # against their docs), so a turn that completes with no speech,
            # no audio, and no tool call can't be prevented client-side —
            # only recovered from. Rather than pattern-matching specific
            # phrases the model might say (a losing game — any new stuck
            # scenario needs its own detector), this generically nudges the
            # model to continue on ANY empty turn, with a bounded retry count
            # so a genuinely broken session degrades to a clear apology
            # instead of nudging forever.
            is_stuck_turn = (
                event.get("status") != "interrupted"
                and not had_content
                and not pending_tool_results
                and not call_state.get("tool_called_this_turn")
            )

            if is_stuck_turn:
                call_state["stuck_turn_count"] = call_state.get("stuck_turn_count", 0) + 1
                stuck_count = call_state["stuck_turn_count"]
                print(f"[Reply Done] WARNING: empty reply detected (no content, no tool call) — stuck_turn_count={stuck_count}.")

                if stuck_count <= STUCK_TURN_MAX_RETRIES:
                    print(f"[Safety Net] Nudging agent to continue (attempt {stuck_count}/{STUCK_TURN_MAX_RETRIES}).")
                    await aai_ws.send(json.dumps({
                        "type": "reply.create",
                        "instructions": (
                            "Your last turn produced no response. Continue the conversation now: "
                            "respond to what the caller just said, and take the appropriate next "
                            "action — call a tool if one applies — instead of staying silent."
                        ),
                    }))
                else:
                    # Repeated nudges didn't help — this session is stuck for
                    # a reason a prompt nudge can't fix. Say so plainly and
                    # stop nudging, rather than looping forever.
                    print(f"[Safety Net] Exceeded {STUCK_TURN_MAX_RETRIES} retries — giving up on nudging, forcing a spoken apology.")
                    await aai_ws.send(json.dumps({
                        "type": "reply.create",
                        "instructions": (
                            "Apologize briefly for the delay, and ask the caller to repeat "
                            "what they need help with."
                        ),
                    }))
                    call_state["stuck_turn_count"] = 0  # give the retry budget back for the next issue
            else:
                call_state["stuck_turn_count"] = 0

            if event.get("status") == "interrupted":
                pending_tool_results.clear()
                call_state["hangup_pending"] = False
            else:
                for call_id, result in pending_tool_results.items():
                    payload = {
                        "type": "tool.result",
                        "call_id": call_id,
                        "result": json.dumps(result),
                    }
                    print(f"[Tool Result] sending to AssemblyAI: {payload!r}")
                    await aai_ws.send(json.dumps(payload))
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

        else:
            # Not necessarily a problem — the Voice Agent API sends other
            # event types (e.g. reply.started) this relay doesn't need to
            # act on — but logging them makes a stuck-call investigation
            # possible instead of guessing blind at what AssemblyAI sent.
            print(f"[AssemblyAI Event] unhandled type={etype!r} event={event!r}")

    except RuntimeError as exc:
        # Starlette raises this specific RuntimeError from client_ws.send_*
        # once the browser side has disconnected (close frame already sent).
        # Once that's happened, EVERY subsequent AssemblyAI event will fail
        # the same way — re-raise so aai_to_client()'s loop notices and stops
        # relaying entirely, instead of catching this per-event and retrying
        # forever until AssemblyAI's own session eventually ends (each
        # failure logging a full traceback in the meantime).
        if "close message has been sent" in str(exc):
            raise
        print(f"[Voice Event Handling Error] type={etype!r}: {exc}")
        try:
            await client_ws.send_text(json.dumps({
                "type": "voice_warning",
                "message": f"Voice session error while handling '{etype}': {exc}",
            }))
        except Exception:
            pass

    except Exception as exc:
        import traceback
        print(f"[Voice Event Handling Error] type={etype!r}: {exc}")
        traceback.print_exc()
        # Without this, a failure here (e.g. sending tool.result after
        # AssemblyAI already closed its side) was swallowed silently —
        # the call would just go dead with no audio/transcripts and no
        # indication to the caller of what happened.
        try:
            await client_ws.send_text(json.dumps({
                "type": "voice_warning",
                "message": f"Voice session error while handling '{etype}': {exc}",
            }))
        except Exception:
            pass


async def run_live_voice_agent(
    client_ws: WebSocket,
    persona: Dict[str, Any],
    ticket_code: Optional[str],
    user_email: Optional[str],
    ticket: Optional[Dict[str, Any]] = None,
    prior_transcripts: Optional[list] = None,
    account: Optional[Dict[str, Any]] = None,
) -> None:
    """Relays audio/events between the browser and AssemblyAI's Voice Agent API,
    translating the wire protocol into the simpler shape app.js understands."""
    headers = {"Authorization": f"Bearer {ASSEMBLYAI_API_KEY}"}

    async with websockets.connect(ASSEMBLYAI_VOICE_AGENT_URL, additional_headers=headers) as aai_ws:
        session_update = build_session_update(persona, ticket, prior_transcripts, account)
        resolved_prompt = session_update["session"]["system_prompt"]
        print(
            f"[Voice Session] resolved system_prompt "
            f"({len(resolved_prompt)} chars, {len(resolved_prompt.split())} words):\n{resolved_prompt}"
        )
        await aai_ws.send(json.dumps(session_update))

        pending_tool_results: Dict[str, Any] = {}
        # Shared with _handle_aai_event: set when the agent calls end_call,
        # and flipped to should_close once that turn's reply.done confirms
        # the goodbye has fully been relayed to the browser. "account" is
        # carried here (rather than as its own function param everywhere)
        # so the stuck-turn safety net below can look up the caller's phone
        # number without needing a wider signature change.
        call_state: Dict[str, Any] = {
            "hangup_pending": False,
            "should_close": False,
            "account": account,
            "tool_called_this_turn": False,
            # Consecutive stuck (empty, no-tool-call) turns — bounded so a
            # model that's truly stuck (not just needing one nudge) doesn't
            # loop the reply.create nudge forever; see STUCK_TURN_MAX_RETRIES.
            "stuck_turn_count": 0,
        }

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
            client_disconnected = False
            try:
                async for raw in aai_ws:
                    await _handle_aai_event(raw, client_ws, pending_tool_results, aai_ws, ticket_code, user_email, call_state)
                    if call_state.get("should_close"):
                        agent_hung_up = True
                        break
            except websockets.exceptions.ConnectionClosed:
                pass
            except RuntimeError as exc:
                # Re-raised from _handle_aai_event once the browser side has
                # already closed — stop relaying immediately instead of
                # continuing to iterate aai_ws and failing on every single
                # subsequent event (audio chunks included) for the rest of
                # that AssemblyAI turn.
                if "close message has been sent" not in str(exc):
                    raise
                client_disconnected = True
                print("[Voice Session] client_ws already closed — stopping aai_to_client relay.")

            if client_disconnected:
                return  # nothing left to notify — the browser is already gone

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
    account: Optional[Dict[str, Any]] = None,
) -> None:
    """Local simulator so the UI can be exercised without an AssemblyAI key.
    Greets the caller, then fires one canned tool-call/response cycle the first
    time it detects real microphone energy in the incoming audio (only for
    personas that actually have check_network_status)."""
    base_greeting = build_session_update(persona, ticket, prior_transcripts, account)["session"]["greeting"]
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

    # Browsers can't set custom headers on a WebSocket handshake, so the
    # session token travels as a query param here instead of an Authorization
    # header. user_email is derived from it (never trusted from a client-
    # supplied ?email= — that would let anyone impersonate any caller and
    # have Maya read out that caller's real phone number/plan as context).
    token = client_ws.query_params.get("token")
    user_email = await asyncio.to_thread(get_session_user, token) if token else None
    if not user_email:
        await client_ws.send_text(json.dumps({
            "type": "session_ended",
            "reason": "Missing or invalid session — please log in again.",
        }))
        await client_ws.close()
        return

    category = client_ws.query_params.get("category")
    ticket_code = client_ws.query_params.get("ticket")
    persona = resolve_persona(category)

    # Pull the actual ticket (subject/description) so the greeting/prompt can
    # reference the caller's real reported problem instead of a generic line.
    ticket = await asyncio.to_thread(get_ticket_by_code, ticket_code) if ticket_code else None
    # A ticket_code was supplied but doesn't belong to this session's user —
    # refuse to attach it rather than leaking another caller's ticket/account
    # context (subject, description, phone number, prior transcript) to Maya.
    if ticket and ticket.get("user_email", "").strip().lower() != user_email:
        await client_ws.send_text(json.dumps({
            "type": "session_ended",
            "reason": "This ticket does not belong to the current user.",
        }))
        await client_ws.close()
        return
    # Pull every transcript line ever recorded against this ticket — from
    # this call's prior sessions — so the agent picks up with full context
    # instead of starting cold on a repeat call.
    prior_transcripts = await asyncio.to_thread(get_transcripts_for_ticket, ticket_code) if ticket_code else []
    # Pull the caller's linked account (phone number/plan) so Maya has it as
    # context for tools like restart_connection instead of asking for it —
    # None for guests or accounts registered before phone numbers existed.
    account = await asyncio.to_thread(get_account_by_email, user_email) if user_email else None
    print(
        f"[Voice Session] category={category!r} ticket={ticket_code!r} "
        f"has_ticket_context={bool(ticket)} prior_lines={len(prior_transcripts)} "
        f"has_account={bool(account)} "
        f"-> persona={persona['name']!r} voice={persona['voice']!r}"
    )

    try:
        await client_ws.send_text(json.dumps({
            "type": "persona_info", "name": persona["name"], "voice": persona["voice"],
        }))

        if MOCK_MODE:
            print("[Voice Session] MOCK mode active (no ASSEMBLYAI_API_KEY or VOICE_MOCK_MODE=true).")
            await run_mock_voice_agent(client_ws, persona, ticket_code, user_email, ticket, prior_transcripts, account)
        else:
            await run_live_voice_agent(client_ws, persona, ticket_code, user_email, ticket, prior_transcripts, account)
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