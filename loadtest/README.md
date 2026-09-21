# Load testing the voice support server

Tests whether the server can handle ~200 concurrent users each holding an
open voice-call WebSocket, without spending real AssemblyAI credits (the
Docker image defaults to `VOICE_MOCK_MODE=true`).

## 1. Build and run the server in Docker

From the repo root:

```bash
docker build -t wavelink-support -f backend/Dockerfile .
docker run --rm -p 8000:8000 --name wavelink-support wavelink-support
```

This starts the server with `VOICE_MOCK_MODE=true` and a fresh, empty
`users.db` inside the container (not your local dev database).

To watch logs live in another terminal: `docker logs -f wavelink-support`

## 2. Run the load test

From the repo root, in a separate terminal:

```bash
pip install -r loadtest/requirements.txt
python loadtest/voice_load_test.py --users 200 --host localhost --port 8000
```

Each simulated user: registers an account, creates a ticket, opens the voice
WebSocket exactly like the browser does (same query params), streams ~8
seconds of synthetic silent PCM audio at the real ~85ms chunk cadence the
frontend uses, and reports how long each step took.

Useful flags:
- `--users N` — concurrency level (try 20, 50, 100, 200 to find where it degrades)
- `--call-seconds N` — how long each simulated call stays open
- `--ramp-seconds N` — spreads connection starts over N seconds instead of
  firing all 200 at the exact same instant (more realistic than a single
  thundering herd, but still a heavy sustained-connect-rate test)

## 3. Reading the results

The summary reports register/ticket/WebSocket-connect/time-to-greeting
latency at avg/p50/p95/max, plus a breakdown of any failures by exception
type. What to watch for as `--users` climbs:

- **WS connect latency climbing sharply or connections failing**: the server
  (or its event loop) is saturated — this is what the `asyncio.to_thread`
  fix around `db.py` calls (see `backend/server.py`) targets, since without
  it a single blocking SQLite write stalls every other open call.
- **Register/ticket latency climbing**: SQLite contention. WAL mode is
  already enabled (`db.py`'s `get_db()`), which helps reads not block behind
  writes, but very high concurrent write rates will still eventually queue.
- **Connection refused / OS-level errors at high concurrency**: you may be
  hitting file-descriptor or ephemeral-port limits on the machine running
  the load test itself, not the server — check `ulimit -n` before assuming
  it's a server-side ceiling.

## 4. Testing against the real AssemblyAI API instead

Not recommended for a 200-user run (real credits, real per-account rate
limits that aren't about your server's capacity at all). If you do want to
sanity-check a smaller number of real concurrent calls:

```bash
docker run --rm -p 8000:8000 \
  -e VOICE_MOCK_MODE=false \
  -e ASSEMBLYAI_API_KEY=your_key_here \
  wavelink-support
```

Then run the load test with a small `--users` count and check your
AssemblyAI account's concurrency limits first.
