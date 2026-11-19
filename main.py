import asyncio
import re
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

CAM_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class MultiCameraManager:
    def __init__(self):
        self.cameras: dict[str, WebSocket] = {}
        self.viewers: dict[str, set[WebSocket]] = {}

    async def connect_cam(self, cam_id: str, websocket: WebSocket):
        await websocket.accept()
        old_ws = self.cameras.get(cam_id)
        if old_ws is not None and old_ws is not websocket:
            try:
                await old_ws.close()
            except Exception:
                pass
        self.cameras[cam_id] = websocket
        print(f"[RELAY] Cámara conectada: {cam_id}")

    def disconnect_cam(self, cam_id: str, websocket: WebSocket):
        if self.cameras.get(cam_id) is websocket:
            del self.cameras[cam_id]
            print(f"[RELAY] Cámara desconectada: {cam_id}")

    async def connect_viewer(self, cam_id: str, websocket: WebSocket):
        await websocket.accept()
        self.viewers.setdefault(cam_id, set()).add(websocket)
        print(f"[RELAY] Visor conectado a: {cam_id}")

    def disconnect_viewer(self, cam_id: str, websocket: WebSocket):
        viewers = self.viewers.get(cam_id)
        if viewers:
            viewers.discard(websocket)
            if not viewers:
                del self.viewers[cam_id]

    async def broadcast(self, cam_id: str, data: bytes):
        viewers = self.viewers.get(cam_id)
        if not viewers:
            return
        dead = []
        for viewer in list(viewers):
            try:
                await asyncio.wait_for(viewer.send_bytes(data), timeout=0.5)
            except Exception:
                dead.append(viewer)
        for viewer in dead:
            viewers.discard(viewer)


manager = MultiCameraManager()


@app.get("/")
async def health():
    return {"status": "online", "cameras": sorted(manager.cameras.keys())}


@app.get("/api/cameras")
async def list_cameras():
    return {"cameras": sorted(manager.cameras.keys())}


@app.websocket("/ws/cam/{cam_id}")
async def cam_endpoint(websocket: WebSocket, cam_id: str):
    if not CAM_ID_REGEX.match(cam_id):
        await websocket.close(code=1008)
        return
    await manager.connect_cam(cam_id, websocket)
    try:
        while True:
            data = await websocket.receive_bytes()
            await manager.broadcast(cam_id, data)
    except WebSocketDisconnect:
        manager.disconnect_cam(cam_id, websocket)
    except Exception:
        manager.disconnect_cam(cam_id, websocket)


@app.websocket("/ws/viewer/{cam_id}")
async def viewer_endpoint(websocket: WebSocket, cam_id: str):
    if not CAM_ID_REGEX.match(cam_id):
        await websocket.close(code=1008)
        return
    await manager.connect_viewer(cam_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_viewer(cam_id, websocket)
    except Exception:
        manager.disconnect_viewer(cam_id, websocket)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
