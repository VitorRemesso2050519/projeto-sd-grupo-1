from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import contextlib
import json
import os
from typing import List
import aio_pika

# --- Configuration (env-overridable) ---
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://guest:guest@rabbitmq:5672/")
RABBIT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "events")
RABBIT_QUEUE = os.getenv("RABBIT_QUEUE", "events.gps")

app = FastAPI(title="Trail Backend (WS + RabbitMQ)")

# Allow CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active WebSocket connections
connections: List[WebSocket] = []


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            # Keep the socket alive
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(ValueError):
            connections.remove(websocket)
        print("WebSocket client disconnected")


@app.post("/events")
async def receive_event(event: dict):
    """
    Optional HTTP endpoint for backwards compatibility.
    Broadcasts an incoming event to all connected WS clients.
    """
    await _broadcast(event)
    return {"status": "event sent"}


async def _broadcast(event: dict):
    """Send an event to all currently connected WebSocket clients."""
    disconnected = []
    for ws in list(connections):
        try:
            await ws.send_json(event)
        except Exception as e:
            print(f"Error sending to WebSocket: {e}")
            disconnected.append(ws)
    for ws in disconnected:
        with contextlib.suppress(ValueError):
            connections.remove(ws)


# -----------------------------
# Resilient RabbitMQ consumer
# -----------------------------
async def _consume_loop():
    """
    Robust background loop that keeps trying to connect to RabbitMQ,
    declares exchange/queue/binding, and streams messages to _broadcast.
    Uses exponential backoff on failures and recovers from broker restarts.
    """
    backoff = 1
    while True:
        try:
            # Connect (robust connection handles some failures automatically)
            conn = await aio_pika.connect_robust(RABBIT_URL)
            try:
                channel = await conn.channel()
                exchange = await channel.declare_exchange(
                    RABBIT_EXCHANGE,
                    aio_pika.ExchangeType.FANOUT,
                    durable=True,
                )
                queue = await channel.declare_queue(RABBIT_QUEUE, durable=True)
                await queue.bind(exchange)

                # Reset backoff after a successful (re)connect
                backoff = 1
                print("RabbitMQ consumer connected and ready.")

                async with queue.iterator() as q:
                    async for message in q:
                        async with message.process():
                            try:
                                event = json.loads(message.body)
                                await _broadcast(event)
                            except Exception as e:
                                print(f"Bad message: {e}")
            finally:
                # Ensure we close the connection if we drop out of the consume loop
                with contextlib.suppress(Exception):
                    await conn.close()

        except Exception as e:
            # Connection or channel failure; try again with backoff
            print(f"RabbitMQ connect/consume failed: {e}. Retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)  # cap at 30s


@app.on_event("startup")
async def start_bg_consumer():
    app.state.consumer_task = asyncio.create_task(_consume_loop())


@app.on_event("shutdown")
async def stop_bg_consumer():
    task = getattr(app.state, "consumer_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(Exception):
            await task