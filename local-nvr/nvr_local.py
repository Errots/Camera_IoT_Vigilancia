import asyncio
import json
import os
import re
import subprocess
import time
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np
import uvicorn
import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ================= CONFIGURACIÓN =================
RELAY_HOST = "vigilancia-focal.onrender.com"
CLOUD_CAMERAS_API = f"https://{RELAY_HOST}/api/cameras"
CLOUD_VIEWER_WSS = f"wss://{RELAY_HOST}/ws/viewer/{{cam_id}}"

NVR_TOKEN = os.getenv("NVR_TOKEN", "")
AUTH_HEADERS = {"X-API-Token": NVR_TOKEN}

RECORDINGS_DIR = Path("recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)
SEGMENT_SECONDS = 30
RECORD_FPS = 10
IDLE_TIMEOUT_SECONDS = 12  # Tiempo sin frames para cerrar grabación y marcar offline
POLL_INTERVAL_SECONDS = 5  # Frecuencia de descubrimiento de nuevas cámaras
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
CAM_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
# =================================================


@dataclass
class CameraState:
    cam_id: str
    latest_jpeg: bytes | None = None
    latest_bgr: np.ndarray | None = None  # Disponible para inferencia YOLO por cámara
    frame_seq: int = 0
    online: bool = False
    last_frame_at: float = 0.0


cameras: dict[str, CameraState] = {}
ingest_tasks: dict[str, asyncio.Task] = {}
remote_online_ids: set[str] = set()


class H264SegmentRecorder:
    """Codifica fotogramas BGR a MP4 H.264 (yuv420p + faststart) en subcarpeta por cámara."""

    def __init__(self, cam_id: str, width: int, height: int, fps: int):
        cam_dir = RECORDINGS_DIR / cam_id
        cam_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("rec_%Y-%m-%d_%H-%M-%S")
        self.cam_id = cam_id
        self.final_path = cam_dir / f"{ts}.mp4"
        self.temp_path = cam_dir / f"{ts}.tmp"
        self.started_at = time.time()
        self.frame_count = 0

        cmd = [
            FFMPEG_EXE,
            "-y",
            "-loglevel", "error",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-profile:v", "baseline",
            "-level", "3.0",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(self.temp_path),
        ]

        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
        print(f"[NVR:{cam_id}] Grabando segmento: {self.temp_path.name}")

    def write(self, frame: np.ndarray) -> bool:
        if self.proc.poll() is not None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(frame.tobytes())
            self.frame_count += 1
            return True
        except BrokenPipeError:
            return False

    def close(self):
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            _, err = self.proc.communicate(timeout=15)
            if self.proc.returncode == 0 and self.temp_path.exists() and self.frame_count > 0:
                self.temp_path.rename(self.final_path)
                print(f"[NVR:{self.cam_id}] Segmento listo: {self.final_path.name}")
            else:
                if err:
                    print(f"[NVR:{self.cam_id}] FFmpeg error: {err.decode(errors='ignore').strip()}")
                self.temp_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"[NVR:{self.cam_id}] Error cerrando grabación: {e}")
            self.proc.kill()
            self.temp_path.unlink(missing_ok=True)
        finally:
            self.proc = None


async def ingest_camera(cam_id: str):
    state = cameras.setdefault(cam_id, CameraState(cam_id=cam_id))
    ws_url = CLOUD_VIEWER_WSS.format(cam_id=cam_id)
    recorder: H264SegmentRecorder | None = None

    try:
        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=15,
                    additional_headers=AUTH_HEADERS,  # Usa extra_headers si tu versión de websockets es < 14
                ) as ws:
                    print(f"[NVR:{cam_id}] Suscrito a {ws_url}")
                    while True:
                        try:
                            data = await asyncio.wait_for(ws.recv(), timeout=IDLE_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError:
                            state.online = False
                            if recorder is not None:
                                await asyncio.to_thread(recorder.close)
                                recorder = None
                            # Si la cámara ya no está reportada en Render, libera la tarea
                            if cam_id not in remote_online_ids:
                                print(f"[NVR:{cam_id}] Sin señal y desconectada en Relay; deteniendo ingesta.")
                                return
                            continue

                        if not isinstance(data, bytes):
                            continue

                        arr = np.frombuffer(data, dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame is None:
                            continue

                        now = time.time()
                        state.latest_jpeg = data
                        state.latest_bgr = frame
                        state.frame_seq += 1
                        state.online = True
                        state.last_frame_at = now

                        if recorder is None or (now - recorder.started_at >= SEGMENT_SECONDS):
                            if recorder is not None:
                                old_rec = recorder
                                recorder = None
                                asyncio.create_task(asyncio.to_thread(old_rec.close))
                            h, w = frame.shape[:2]
                            recorder = H264SegmentRecorder(cam_id, w, h, RECORD_FPS)

                        if not recorder.write(frame):
                            old_rec = recorder
                            recorder = None
                            asyncio.create_task(asyncio.to_thread(old_rec.close))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                state.online = False
                if recorder is not None:
                    await asyncio.to_thread(recorder.close)
                    recorder = None
                if cam_id not in remote_online_ids:
                    return
                print(f"[NVR:{cam_id}] Reconectando tras error: {e}")
                await asyncio.sleep(3)
    finally:
        state.online = False
        if recorder is not None:
            recorder.close()
        ingest_tasks.pop(cam_id, None)


def _fetch_remote_cameras_sync() -> list[str]:
    req = urllib.request.Request(
        CLOUD_CAMERAS_API,
        headers={"User-Agent": "NVR-Local/1.0", **AUTH_HEADERS},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        return [c for c in payload.get("cameras", []) if CAM_ID_REGEX.match(c)]


async def discovery_loop():
    global remote_online_ids
    while True:
        try:
            active_list = await asyncio.to_thread(_fetch_remote_cameras_sync)
            remote_online_ids = set(active_list)
            for cam_id in active_list:
                task = ingest_tasks.get(cam_id)
                if task is None or task.done():
                    ingest_tasks[cam_id] = asyncio.create_task(ingest_camera(cam_id))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[DISCOVERY] Error consultando cámaras en Relay: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    disc_task = asyncio.create_task(discovery_loop())
    try:
        yield
    finally:
        disc_task.cancel()
        for task in list(ingest_tasks.values()):
            task.cancel()
        await asyncio.gather(disc_task, *ingest_tasks.values(), return_exceptions=True)


app = FastAPI(lifespan=lifespan)


def validate_cam_id(cam_id: str):
    if not CAM_ID_REGEX.match(cam_id):
        raise HTTPException(status_code=400, detail="ID de cámara inválido")


async def mjpeg_generator(cam_id: str):
    last_seq = -1
    while True:
        state = cameras.get(cam_id)
        if state and state.latest_jpeg is not None and state.frame_seq != last_seq:
            last_seq = state.frame_seq
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + state.latest_jpeg + b"\r\n"
            )
        await asyncio.sleep(0.04)


@app.get("/api/cameras")
async def get_cameras():
    # Combina cámaras activas en memoria + carpetas con grabaciones previas en disco
    known_ids = set(cameras.keys()) | set(remote_online_ids)
    for d in RECORDINGS_DIR.iterdir():
        if d.is_dir() and CAM_ID_REGEX.match(d.name):
            known_ids.add(d.name)

    result = []
    for cid in sorted(known_ids):
        state = cameras.get(cid)
        is_online = bool(state and state.online) or (cid in remote_online_ids)
        rec_dir = RECORDINGS_DIR / cid
        rec_count = len(list(rec_dir.glob("*.mp4"))) if rec_dir.exists() else 0
        result.append({
            "id": cid,
            "online": is_online,
            "recordings_count": rec_count,
        })
    return result


@app.get("/live/{cam_id}")
async def live_stream(cam_id: str):
    validate_cam_id(cam_id)
    return StreamingResponse(
        mjpeg_generator(cam_id), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/recordings/{cam_id}")
async def list_recordings(cam_id: str):
    validate_cam_id(cam_id)
    cam_dir = RECORDINGS_DIR / cam_id
    if not cam_dir.exists():
        return []
    files = sorted(cam_dir.glob("*.mp4"), reverse=True)
    return [
        {
            "name": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "url": f"/recordings/{cam_id}/{f.name}",
        }
        for f in files
    ]


app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)