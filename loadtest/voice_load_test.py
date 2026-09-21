"""
loadtest/voice_load_test.py — Simulates N concurrent end users against a
running instance of the Wavelink Mobile server: register/login, create a
ticket, open the voice WebSocket (mock mode), stream synthetic mic audio for
a while, then hang up. Reports connect latency, time-to-first-greeting,
error counts, and simple throughput stats.

Requires the server running with VOICE_MOCK_MODE=true (the Docker image's
default) so this doesn't consume real AssemblyAI credits — it exercises the
same WebSocket relay, audio path, and SQLite transcript writes a real call
does, just with a canned mock reply instead of a live LLM.

Usage:
    pip install -r loadtest/requirements.txt
    python loadtest/voice_load_test.py --users 200 --host localhost --port 8000
"""

import argparse
import asyncio
import json
import random
import statistics
import string
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import websockets

SILENCE_CHUNK = b"\x00\x00" * 480  # ~10ms of digital silence @ 24kHz mono 16-bit


@dataclass
class UserResult:
    user_id: int
    ok: bool = False
    error: Optional[str] = None
    register_ms: Optional[float] = None
    ticket_ms: Optional[float] = None
    ws_connect_ms: Optional[float] = None
    time_to_greeting_ms: Optional[float] = None
    audio_chunks_sent: int = 0
    call_duration_ms: Optional[float] = None


def rand_suffix(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


async def simulate_one_user(
    user_id: int,
    base_url: str,
    ws_base: str,
    call_seconds: float,
    http_client: httpx.AsyncClient,
) -> UserResult:
    res = UserResult(user_id=user_id)
    email = f"loadtest_{user_id}_{rand_suffix()}@example.com"
    password = "LoadTest123!"

    try:
        # 1. Register (also logs the user in server-side, mirroring the UI's
        # real signup flow rather than a separate seeded fixture).
        t0 = time.monotonic()
        reg_res = await http_client.post(
            f"{base_url}/api/auth/register",
            json={"email": email, "password": password, "name": f"Load Test {user_id}"},
        )
        reg_res.raise_for_status()
        res.register_ms = (time.monotonic() - t0) * 1000

        # 2. Create a ticket — this is the "problem description" the AI will
        # confirm on the greeting, same as a real caller escalating from the
        # ticket queue.
        t0 = time.monotonic()
        ticket_res = await http_client.post(
            f"{base_url}/api/tickets",
            json={
                "user_email": email,
                "subject": "No signal in Downtown area",
                "description": "My phone has had no bars since this morning, can't make calls.",
                "category": "Network & Service",
                "priority": "HIGH",
                "escalateVoice": True,
            },
        )
        ticket_res.raise_for_status()
        ticket = ticket_res.json()
        res.ticket_ms = (time.monotonic() - t0) * 1000
        ticket_code = ticket["ticket_id"].lstrip("#")

        # 3. Open the voice WebSocket, same query params app.js sends.
        ws_url = (
            f"{ws_base}/ws/voice?category=Network%20%26%20Service"
            f"&ticket={ticket_code}&email={email}"
        )

        t0 = time.monotonic()
        async with websockets.connect(ws_url, open_timeout=20) as ws:
            res.ws_connect_ms = (time.monotonic() - t0) * 1000

            # Wait for persona_info, then the first agent transcript (greeting).
            t_greeting_start = time.monotonic()
            got_greeting = False
            call_start = time.monotonic()
            call_deadline = call_start + call_seconds

            async def audio_pump():
                # Streams silence continuously like a real open mic would,
                # at the same ~85ms cadence audio.js's ScriptProcessorNode
                # uses (2048 samples @ 24kHz), so this exercises the relay's
                # steady-state per-chunk overhead under load.
                while time.monotonic() < call_deadline:
                    await ws.send(SILENCE_CHUNK * 4)  # ~2048 samples/frame
                    res.audio_chunks_sent += 1
                    await asyncio.sleep(0.085)

            async def receive_loop():
                nonlocal got_greeting
                while time.monotonic() < call_deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break
                    if isinstance(msg, (bytes, bytearray)):
                        continue
                    try:
                        data = json.loads(msg)
                    except (TypeError, ValueError):
                        continue
                    if not got_greeting and data.get("type") == "transcript" and data.get("role") == "agent":
                        got_greeting = True
                        res.time_to_greeting_ms = (time.monotonic() - t_greeting_start) * 1000

            await asyncio.gather(audio_pump(), receive_loop())
            res.call_duration_ms = (time.monotonic() - call_start) * 1000

        res.ok = True
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"

    return res


def summarize(results: list[UserResult]) -> None:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print(f"\n{'=' * 60}")
    print(f"Simulated users:      {len(results)}")
    print(f"Succeeded:            {len(ok)}")
    print(f"Failed:               {len(failed)}")
    if failed:
        by_error: dict[str, int] = {}
        for r in failed:
            by_error[r.error] = by_error.get(r.error, 0) + 1
        print("\nFailure breakdown:")
        for err, count in sorted(by_error.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}x  {err}")

    def pctl(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        idx = min(len(s) - 1, int(len(s) * p))
        return s[idx]

    def report(label, vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            print(f"{label:<28} n/a")
            return
        print(
            f"{label:<28} avg={statistics.mean(vals):8.1f}ms  "
            f"p50={pctl(vals, 0.5):8.1f}ms  p95={pctl(vals, 0.95):8.1f}ms  max={max(vals):8.1f}ms"
        )

    print()
    report("Register latency:", [r.register_ms for r in ok])
    report("Create ticket latency:", [r.ticket_ms for r in ok])
    report("WS connect latency:", [r.ws_connect_ms for r in ok])
    report("Time to greeting:", [r.time_to_greeting_ms for r in ok])
    print(f"{'=' * 60}\n")


async def main():
    parser = argparse.ArgumentParser(description="Load test the voice support server.")
    parser.add_argument("--users", type=int, default=200, help="Number of concurrent simulated users.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--scheme", default="http", choices=["http", "https"])
    parser.add_argument("--call-seconds", type=float, default=8.0, help="How long each simulated call streams audio.")
    parser.add_argument("--ramp-seconds", type=float, default=5.0, help="Spread user starts over this window instead of all at once.")
    args = parser.parse_args()

    base_url = f"{args.scheme}://{args.host}:{args.port}"
    ws_scheme = "wss" if args.scheme == "https" else "ws"
    ws_base = f"{ws_scheme}://{args.host}:{args.port}"

    print(f"Target: {base_url}  |  simulated users: {args.users}  |  call length: {args.call_seconds}s")
    print(f"Ramping starts over {args.ramp_seconds}s to avoid an artificial instant-thundering-herd spike...\n")

    limits = httpx.Limits(max_connections=args.users + 10, max_keepalive_connections=args.users)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:

        async def delayed_start(user_id: int) -> UserResult:
            delay = (user_id / max(1, args.users)) * args.ramp_seconds
            await asyncio.sleep(delay)
            return await simulate_one_user(user_id, base_url, ws_base, args.call_seconds, client)

        t_start = time.monotonic()
        results = await asyncio.gather(*(delayed_start(i) for i in range(args.users)))
        total_wall_s = time.monotonic() - t_start

    print(f"Total wall time: {total_wall_s:.1f}s")
    summarize(list(results))


if __name__ == "__main__":
    asyncio.run(main())
