# Wavelink Mobile — AI Voice Support Agent

A customer care portal for a fictional telecom carrier ("Wavelink Mobile") where support tickets are triaged and resolved live by **Maya**, an AI voice agent built on the [AssemblyAI Voice Agent API](https://www.assemblyai.com/). Customers register, file a ticket, and jump straight into a real-time voice call with an AI agent that already knows their issue, can check network status, reset their connection, and hang up gracefully when the issue is resolved.

Built for the AssemblyAI Voice Agent Hackathon.

## How it works

1. **Sign up / log in** — a lightweight SQLite-backed auth system (PBKDF2-hashed passwords).
2. **File a ticket** — pick a category (Billing, Network, Account), priority, subject, and description.
3. **Talk to Maya** — open a live voice call bound to that ticket. Audio streams over a WebSocket relay to AssemblyAI's Voice Agent API and back, with real-time transcription shown in the UI.
4. **Maya resolves the issue** — she can call backend tools (`check_network_status`, `restart_connection`) mid-conversation, and ends the call herself once the issue is resolved, always saying goodbye first.
5. **Full history is kept** — every transcript line is persisted per ticket, so if a customer calls back about the same ticket, Maya picks up the conversation with full context instead of starting from zero.

## Architecture

```
frontend/   Single-page app (Alpine.js + Tailwind) — auth, ticket queue, live voice UI
backend/    FastAPI relay server — REST (auth/tickets) + WebSocket voice bridge
loadtest/   Concurrency/load testing harness for the voice WebSocket relay
```

The backend acts as a relay between the browser and AssemblyAI: it forwards mic audio up, streams agent audio/transcripts back down, executes tool calls against a mock telecom backend (`support_tools.py`), and persists tickets/transcripts to SQLite (`db.py`).

A **mock mode** (`VOICE_MOCK_MODE=true`, or simply no `ASSEMBLYAI_API_KEY` set) simulates the voice agent locally — greeting, a canned tool-call demo, and synthetic audio tones — so the full UI and relay path can be exercised without burning AssemblyAI credits.

## Getting started

### Backend

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # add your ASSEMBLYAI_API_KEY to go live
python server.py
```

Server runs at `http://localhost:8000` and also serves the frontend.

### Frontend

No build step — it's served directly by the backend at `/`. Just open `http://localhost:8000` after starting the server.

## Configuration

| Variable | Description |
|---|---|
| `ASSEMBLYAI_API_KEY` | Your AssemblyAI API key. Omit to run in mock mode. |
| `ASSEMBLYAI_VOICE_AGENT_URL` | Voice Agent WebSocket endpoint (defaults to AssemblyAI's). |
| `VOICE_MOCK_MODE` | Force mock mode even with a key set (`true`/`false`). |

## Load testing

See [loadtest/README.md](loadtest/README.md) for simulating ~200 concurrent voice calls against a Dockerized instance of the server to validate WebSocket and SQLite concurrency handling.

## Tech stack

- **Voice AI**: AssemblyAI Voice Agent API (streaming STT + TTS + turn detection + tool calling)
- **Backend**: FastAPI, WebSockets, SQLite (WAL mode)
- **Frontend**: Alpine.js, Tailwind CSS
