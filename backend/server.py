import os
from fastapi import FastAPI

app = FastAPI()

ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
ASSEMBLYAI_VOICE_AGENT_URL = "wss://api.assemblyai.com/v2/voice-agent"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)