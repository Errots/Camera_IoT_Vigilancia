import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

class StreamRelay:
    def __init__(self):
        self.camera: WebSocket | None = None
        self.viewers: set[WebSocket] = set()

    async def set_camera(self, ws: WebSocket):
        await ws.accept()
        if self.camera:
            try:
                await self.camera.close()
            except Exception:
                pass
        self.camera = ws

    async def add_viewer(self, ws: WebSocket):
        await ws.accept()
        self.viewers.add(ws)

    def remove_viewer(self, ws: WebSocket):
        self.viewers.discard(ws)

    async def broadcast(self, frame: bytes):
        if not self.viewers:
            return
        disconnected = []
        for viewer in list(self.viewers):
            try:
                await asyncio.wait_for(viewer.send_bytes(frame), timeout=0.5)
            except Exception:
                disconnected.append(viewer)
        for viewer in disconnected:
            self.viewers.discard(viewer)

relay = StreamRelay()

@app.get("/")
async def status():
    return {
        "status": "online",
        "camera_connected": relay.camera is not None,
        "active_viewers": len(relay.viewers),
    }

@app.websocket("/ws/cam")
async def camera_ws(websocket: WebSocket):
    await relay.set_camera(websocket)
    try:
        while True:
            frame = await websocket.receive_bytes()
            await relay.broadcast(frame)
    except WebSocketDisconnect:
        if relay.camera == websocket:
            relay.camera = None

@app.websocket("/ws/viewer")
async def viewer_ws(websocket: WebSocket):
    await relay.add_viewer(websocket)
    try:
        while True:
            # Mantiene viva la conexión recibiendo pings o mensajes de control
            await websocket.receive_text()
    except WebSocketDisconnect:
        relay.remove_viewer(websocket)
