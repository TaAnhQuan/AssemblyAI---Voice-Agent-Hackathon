FROM python:3.12-slim

WORKDIR /app

# System deps: none needed beyond what's in the base slim image — this app
# has no audio/codec native deps (PCM math is done in Python/JS, not via
# ffmpeg or similar).

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

WORKDIR /app/backend

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

# VOICE_MOCK_MODE is intentionally NOT set here — server.py already defaults
# to MOCK whenever ASSEMBLYAI_API_KEY is unset, and to LIVE otherwise. A
# hardcoded ENV default here previously overrode the platform's own
# VOICE_MOCK_MODE=false setting on every deploy (a real bug on Railway that
# silently forced MOCK mode). For local load-testing without burning
# AssemblyAI credits, pass --env VOICE_MOCK_MODE=true explicitly instead.

EXPOSE 8000

# --workers is intentionally 1 by default: uvicorn's asyncio event loop
# handles many concurrent I/O-bound WebSocket connections fine on a single
# process once blocking calls are off the loop (see db.py's asyncio.to_thread
# usage), and multiple worker processes would each open their own SQLite
# connection to the same file, which doesn't scale write throughput the way
# it would with a real multi-writer database. Override WEB_CONCURRENCY to
# compare, but expect diminishing/negative returns without a DB change too.
ENV WEB_CONCURRENCY=1

CMD ["sh", "-c", "uvicorn server:app --host $HOST --port $PORT --workers $WEB_CONCURRENCY"]
