import asyncio
import re
import hashlib
import hmac
import os
import secrets
import time
from fastapi import Depends, HTTPException, Request, status, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

app = FastAPI()

CAM_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

CAM_TOKEN = os.getenv("CAM_TOKEN", "")
NVR_TOKEN = os.getenv("NVR_TOKEN", "")
MOBILE_USER = os.getenv("MOBILE_USER", "")
MOBILE_PASS = os.getenv("MOBILE_PASS", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_TTL = 43200  # 12 horas

security = HTTPBasic()

def valid_token(provided: str | None, expected: str) -> bool:
    # Falla cerrado: si la variable no existe en Render, rechaza todo
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


def create_mobile_cookie() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    sig = hmac.new(SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_mobile_cookie(cookie: str | None) -> bool:
    if not SESSION_SECRET or not cookie or "." not in cookie:
        return False
    exp_str, sig = cookie.split(".", 1)
    if not exp_str.isdigit() or int(exp_str) < int(time.time()):
        return False
    expected_sig = hmac.new(SESSION_SECRET.encode(), exp_str.encode(), hashlib.sha256).hexdigest()
    return secrets.compare_digest(sig, expected_sig)


def require_mobile_login(creds: HTTPBasicCredentials = Depends(security)):
    user_ok = valid_token(creds.username, MOBILE_USER)
    pass_ok = valid_token(creds.password, MOBILE_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )

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
    token_ok = valid_token(request.headers.get("x-api-token"), NVR_TOKEN)
    cookie_ok = valid_mobile_cookie(request.cookies.get("mobile_session"))
    if not (token_ok or cookie_ok):
        raise HTTPException(status_code=401, detail="No autorizado")
    return {"cameras": sorted(manager.cameras.keys())}


@app.websocket("/ws/cam/{cam_id}")
async def cam_endpoint(websocket: WebSocket, cam_id: str):
    if not valid_token(websocket.headers.get("x-api-token"), CAM_TOKEN):
        await websocket.close(code=1008)
        return
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

@app.get("/mobile", response_class=HTMLResponse)
async def mobile_viewer(_: None = Depends(require_mobile_login)):
    response = HTMLResponse(content="""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Monitor Móvil IoT</title>
  <style>
    body { margin: 0; background: #0f172a; color: #f8fafc; font-family: system-ui, sans-serif; display: flex; flex-direction: column; height: 100dvh; }
    header { padding: 0.75rem; background: #1e293b; border-bottom: 1px solid #334155; display: flex; gap: 0.5rem; overflow-x: auto; }
    .tab { flex: 0 0 auto; padding: 0.5rem 0.9rem; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #f8fafc; font-weight: 600; font-size: 0.9rem; }
    .tab.active { background: #0284c7; border-color: #38bdf8; }
    main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 0.75rem; gap: 0.75rem; }
    #frame { width: 100%; max-height: 70dvh; object-fit: contain; background: #000; border-radius: 8px; border: 1px solid #334155; }
    .bar { width: 100%; display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; color: #94a3b8; }
    .btn { background: #38bdf8; color: #0f172a; border: none; padding: 0.55rem 1rem; border-radius: 6px; font-weight: 700; }
  </style>
</head>
<body>
  <header id="tabs">Buscando cámaras...</header>
  <main>
    <img id="frame" alt="Esperando señal de video...">
    <div class="bar">
      <span id="status">Desconectado</span>
      <button class="btn" onclick="takeSnapshot()">Guardar Foto</button>
    </div>
  </main>
  <script>
    const img = document.getElementById('frame');
    const tabs = document.getElementById('tabs');
    const statusEl = document.getElementById('status');
    const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
    let currentCam = null, ws = null, lastUrl = null, retryTimer = null;

    function connectCam(camId) {
      currentCam = camId;
      clearTimeout(retryTimer);
      if (ws) { ws.onclose = null; ws.close(); }
      statusEl.textContent = `Conectando a ${camId}...`;
      renderTabs(lastCams);

      ws = new WebSocket(`${wsProto}://${location.host}/ws/viewer/${encodeURIComponent(camId)}`);
      ws.binaryType = 'blob';
      ws.onopen = () => statusEl.textContent = `En vivo: ${camId}`;
      ws.onmessage = (e) => {
        const url = URL.createObjectURL(e.data);
        img.src = url;
        if (lastUrl) URL.revokeObjectURL(lastUrl);
        lastUrl = url;
      };
      ws.onclose = () => {
        statusEl.textContent = `Reconectando ${camId}...`;
        retryTimer = setTimeout(() => { if (currentCam === camId) connectCam(camId); }, 2500);
      };
    }

    let lastCams = [];
    function renderTabs(cams) {
      lastCams = cams;
      tabs.innerHTML = cams.length ? '' : '<span style="color:#94a3b8">Sin cámaras activas</span>';
      cams.forEach(c => {
        const b = document.createElement('button');
        b.className = 'tab' + (c === currentCam ? ' active' : '');
        b.textContent = c;
        b.onclick = () => connectCam(c);
        tabs.appendChild(b);
      });
    }

    async function loadCams() {
      try {
        const r = await fetch('/api/cameras');
        const data = await r.json();
        renderTabs(data.cameras || []);
        if (!currentCam && data.cameras && data.cameras.length > 0) connectCam(data.cameras[0]);
      } catch {}
    }

    function takeSnapshot() {
      if (!lastUrl) return;
      const a = document.createElement('a');
      a.href = lastUrl;
      a.download = `${currentCam || 'cam'}_${Date.now()}.jpg`;
      a.click();
    }

    loadCams();
    setInterval(loadCams, 5000);
  </script>
</body>
</html>""")
    response.set_cookie(
        key="mobile_session",
        value=create_mobile_cookie(),
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=SESSION_TTL,
    )
    return response

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
