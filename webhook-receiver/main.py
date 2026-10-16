import json
import logging
from collections import deque

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook-receiver")

app = FastAPI(title="webhook-receiver")

received_payloads = deque(maxlen=100)


@app.post("/critical")
async def critical(request: Request):
    payload = await request.json()
    logger.info("CRITICAL alert received: %s", json.dumps(payload)[:500])
    received_payloads.appendleft({"route": "critical", "payload": payload})
    return {"status": "received"}


@app.post("/warning")
async def warning(request: Request):
    payload = await request.json()
    logger.info("WARNING alert received: %s", json.dumps(payload)[:500])
    received_payloads.appendleft({"route": "warning", "payload": payload})
    return {"status": "received"}


@app.get("/received")
def received():
    return list(received_payloads)


@app.get("/health")
def health():
    return {"status": "ok"}
