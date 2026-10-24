
import os
import json
import logging
import asyncio
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import Response
from plivo import RestClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mme-plivo")

app = FastAPI()
BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
TOKEN = os.environ["MME_WEBHOOK_TOKEN"]
AUTH_ID = os.environ["PLIVO_AUTH_ID"]
AUTH_TOKEN = os.environ["PLIVO_AUTH_TOKEN"]
PLIVO_NUMBER = os.environ["PLIVO_NUMBER"]

client = RestClient(AUTH_ID, AUTH_TOKEN)

def verify_token(request: Request):
    if request.query_params.get("token") != TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

def call_xml():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Speak language="en-IN">Welcome to M M E. Connecting your AI voice assistant. the system is in under development phase please check after 1 year.</Speak>
  <Stream bidirectional="true"
          keepCallAlive="true"
          contentType="audio/x-mulaw;rate=8000"
          statusCallbackUrl="{BASE_URL}/stream-status?token={TOKEN}">
    {BASE_URL.replace("https://", "wss://")}/stream?token={TOKEN}
  </Stream>
</Response>"""

@app.get("/health")
def health():
    return {"ok": True, "service": "mme-plivo"}

@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    verify_token(request)
    log.info("Answer URL hit: %s", dict(request.query_params))
    return Response(call_xml(), media_type="application/xml")

@app.post("/outbound")
async def outbound(request: Request):
    verify_token(request)
    data = await request.json()
    to_number = data.get("to", "")
    if not to_number.startswith("+") or not to_number[1:].isdigit():
        raise HTTPException(400, "Use destination in E.164 format, e.g. +91XXXXXXXXXX")

    result = await asyncio.to_thread(
        client.calls.create,
        from_=PLIVO_NUMBER,
        to_=to_number,
        answer_url=f"{BASE_URL}/answer?token={TOKEN}",
        answer_method="GET",
    )
    log.info("Outbound call requested: %s", result)
    return {"requested": True, "result": str(result)}

@app.websocket("/stream")
async def stream(ws: WebSocket):
    # Plivo connects to this endpoint after the call's XML is processed.
    # Token is checked before accepting the stream.
    if ws.query_params.get("token") != TOKEN:
        await ws.close(code=1008)
        return

    await ws.accept()
    log.info("Plivo WebSocket connected")
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            kind = event.get("event")

            if kind == "start":
                log.info("Stream started: %s", event.get("start", {}))
            elif kind == "media":
                # Caller audio is in event["media"]["payload"] (base64).
                # TODO: decode audio and pass it to MME STT.
                pass
            elif kind == "dtmf":
                log.info("DTMF: %s", event.get("dtmf", {}))
            elif kind == "stop":
                log.info("Stream stopped")
                break
    except Exception:
        log.exception("Stream closed")
    finally:
        log.info("Plivo WebSocket disconnected")

@app.post("/stream-status")
async def stream_status(request: Request):
    verify_token(request)
    log.info("Stream status: %s", await request.body())
    return {"ok": True}
