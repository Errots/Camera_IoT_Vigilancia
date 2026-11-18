from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

class ConnectionManager:
    def __init__(self):
        self.camera: WebSocket = None
        self.viewers: list[WebSocket] = []

    async def connect_cam(self, websocket: WebSocket):
        await websocket.accept()
        self.camera = websocket
        print("ESP32-CAM conectada al Relay.")

    def disconnect_cam(self):
        self.camera = None
        print("ESP32-CAM desconectada.")

    async def connect_viewer(self, websocket: WebSocket):
        await websocket.accept()
        self.viewers.append(websocket)
        print("Nuevo visor conectado.")

    def disconnect_viewer(self, websocket: WebSocket):
        if websocket in self.viewers:
            self.viewers.remove(websocket)
            print("Visor desconectado.")

    async def broadcast(self, data: bytes):
        for viewer in self.viewers:
            try:
                await viewer.send_bytes(data)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws/cam")
async def cam_endpoint(websocket: WebSocket):
    await manager.connect_cam(websocket)
    try:
        while True:
            data = await websocket.receive_bytes()
            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect_cam()

@app.websocket("/ws/viewer")
async def viewer_endpoint(websocket: WebSocket):
    await manager.connect_viewer(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_viewer(websocket)

if __name__ == "__main__":
    uvicorn.run("main.py", host="0.0.0.0", port=10000)