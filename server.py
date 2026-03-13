import os
import uvicorn
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "baseball-bot"}


@app.get("/")
async def root():
    return {"service": "⚾ Baseball Bot", "status": "running"}


async def run_server():
    port = int(os.environ.get("PORT", 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
