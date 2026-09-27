import asyncio
import os

import httpx
from fastapi import FastAPI, WebSocket

from .call import Call

# An open number is a paid minute for anyone who dials it, so the live calls have a cap.
MAX_CALLS = 10

app = FastAPI()
call_slots = asyncio.Semaphore(MAX_CALLS)


@app.get("/healthz")
async def get_health() -> dict:
    return {"status": "ok"}


@app.websocket("/_calls/{token}")
async def take_call(websocket: WebSocket, token: str) -> None:
    """Run the call of a Twilio stream when Druks picks it up. Druks checks the token and
    Twilio's signature on the handshake."""
    if call_slots.locked():
        await websocket.close(code=1013)
        return
    # The server's one setting: the address where it reaches Druks.
    druks_url = os.environ["DRUKS_URL"]
    async with call_slots, httpx.AsyncClient(base_url=druks_url, timeout=10) as druks:
        call = Call(druks=druks, token=token)
        signature = websocket.headers.get("x-twilio-signature", "")
        answer = await call.post("pickup", signature=signature)
        if answer.is_success:
            await websocket.accept()
            await call.run(websocket=websocket, pickup=answer.json())
            return
        await websocket.close(code=1008)
