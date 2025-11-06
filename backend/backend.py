from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio

app = FastAPI()

# Allow CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# List to store active WebSocket connections
connections = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            # Keep the connection alive
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        # Remove the connection when the client disconnects
        connections.remove(websocket)
        print("WebSocket client disconnected")

@app.post("/events")
async def receive_event(event: dict):
    # Send the event to all active WebSocket connections
    disconnected_connections = []
    print(f"Broadcasting event: {event}")
    for connection in connections:
        try:
            await connection.send_json(event)
        except RuntimeError as e:
            # Handle cases where the connection is already closed
            print(f"Error sending to WebSocket: {e}")
            disconnected_connections.append(connection)

    # Remove disconnected WebSocket connections
    for connection in disconnected_connections:
        connections.remove(connection)

    return {"status": "event sent"}

# Testing CI trigger a second time.