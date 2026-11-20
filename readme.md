
# Camera_IoT_Vigilancia

Base arquitectónica para construir un sistema de videovigilancia IoT multicámara utilizando **ESP32-CAM (AI-Thinker)**, diseñado para operar en **redes con aislamiento de clientes (*AP Isolation*)** y computadoras **Windows sin permisos de administrador**.

Este repositorio proporciona una implementación funcional mínima (captura, túnel seguro por puerto `443`, grabación local en H.264 y panel web) concebida como punto de partida modular para integrar posteriormente autenticación, visión artificial en el servidor (YOLO) o inferencia en el borde (TinyML).

---

## Arquitectura del Sistema

```text
[ ESP32-CAM (cam1) ] ──┐                                  ┌──► [ Panel Web (127.0.0.1:8000) ]
                       ├──► (WSS / Puerto 443) ──► [ Cloud Relay ] ──► [ NVR Local (Python) ]
[ ESP32-CAM (cam2) ] ──┘       Render.com           (FastAPI)           ├── Stream en vivo (MJPEG)
                                                                        ├── Grabación H.264 (.mp4)
                                                                        └── Buffer OpenCV (YOLO)

```

Al utilizar conexiones salientes **WebSockets sobre TLS (`wss://`)** en el puerto estándar `443`, tanto las cámaras como la computadora receptora evaden el bloqueo de comunicación LAN local sin requerir VPNs, drivers de red ni contenedores Docker.

---

## Estructura del Repositorio

```text
Camera_IoT_Vigilancia/
├── cloud-relay/              # Servidor intermediario para despliegue en Render
│   ├── main.py               # Enrutador multicámara WebSocket (FastAPI)
│   └── requirements.txt
├── esp32-firmware/           # Proyecto PlatformIO para AI-Thinker ESP32-CAM
│   ├── platformio.ini
│   └── src/
│       └── main.cpp          # Captura OV2640 y cliente WSS nativo (RFC 6455)
├── local-nvr/                # Receptor, grabador y visor web local
│   ├── nvr_local.py          # Autodescubrimiento, ingesta y grabación H.264
│   ├── requirements.txt
│   ├── recordings/           # Grabaciones segmentadas por cámara (<cam_id>/*.mp4)
│   └── static/
│       └── index.html        # Interfaz web multicámara
└── scripts/                  # Lanzadores portables para Windows (sin admin)
    ├── abrir_vscode_pio.bat  # Monta unidad P: y abre VS Code + PlatformIO offline
    └── iniciar_nvr.bat       # Monta unidad P: e inicia el NVR con WinPython

```

---

## Funcionamiento y Explicación del Código

### 1. Firmware del ESP32-CAM (`esp32-firmware/src/main.cpp`)

Implementado en PlatformIO sobre el framework Arduino sin depender de librerías externas de WebSockets para mantener control total sobre la memoria RAM y la negociación TLS con el proxy de Render.

* **Identificación de cámara (`CAMERA_ID`):** Cada placa define un identificador único (ej. `#define CAMERA_ID "cam1"`) que se concatena en la ruta `/ws/cam/cam1`, permitiendo conectar múltiples dispositivos al mismo servidor simultáneamente.
* **Captura optimizada (`initCamera()`):** Configura el sensor OV2640 en formato `PIXFORMAT_JPEG` (`FRAMESIZE_VGA`, `jpeg_quality = 16`) alojando los buffers en la PSRAM (`CAMERA_FB_IN_PSRAM`). El reloj `xclk_freq_hz` se fija en **10 MHz** con `CAMERA_GRAB_WHEN_EMPTY` para evitar saturación del bus DMA y prevenir errores de desbordamiento (`FB-OVF`) mientras opera el Wi-Fi/TLS.
* **Activación del servicio (`wakeRenderService()`):** Antes de abrir el WebSocket, ejecuta una petición `GET /` sobre HTTPS para despertar la instancia gratuita de Render si se encontraba suspendida por inactividad.
* **Cliente WebSocket nativo (`connectWebSocket()` y `sendWsBinaryFrame()`):** Realiza el *handshake* HTTP `101 Switching Protocols` directamente sobre `WiFiClientSecure` y transmite cada fotograma JPEG como una trama binaria RFC 6455 (`0x82`). El enmascaramiento XOR obligatorio del cliente se aplica en bloques de `1024 bytes` para no duplicar el tamaño del fotograma en memoria RAM.

---

### 2. Servidor Intermediario (`cloud-relay/main.py`)

Aplicación ligera en **FastAPI** que actúa como túnel ciego en la nube (Render.com) sin almacenar video en disco.

* **Gestor multicámara (`MultiCameraManager`):** Mantiene dos diccionarios en memoria indexados por `cam_id`:
* `self.cameras`: almacena la conexión WebSocket activa de cada ESP32-CAM (si una cámara se reconecta con el mismo ID, cierra limpiamente la sesión anterior).
* `self.viewers`: almacena el conjunto de clientes suscritos a cada cámara.


* **Retransmisión no bloqueante (`broadcast()`):** Envía los bytes JPEG recibidos a todos los visores suscritos a ese `cam_id` aplicando un *timeout* de `0.5s` por cliente, evitando que un visor lento congele el flujo de la cámara.
* **Endpoints principales:**
* `WS /ws/cam/{cam_id}`: Punto de entrada de fotogramas desde el ESP32.
* `WS /ws/viewer/{cam_id}`: Punto de suscripción para el NVR local u otros visores.
* `GET /api/cameras`: Devuelve la lista JSON de cámaras conectadas en tiempo real para el autodescubrimiento.



---

### 3. NVR Local e Interfaz Web (`local-nvr/nvr_local.py`)

Servidor local en Python que se ejecuta sobre `127.0.0.1:8000` (sin alertas del Firewall de Windows ni permisos de administrador) para procesar, grabar y mostrar el video.

* **Autodescubrimiento (`discovery_loop()`):** Consulta cada 5 segundos el endpoint `/api/cameras` de Render. Por cada nueva cámara detectada, lanza automáticamente una tarea asíncrona independiente (`ingest_camera(cam_id)`).
* **Ingesta y estado (`CameraState` e `ingest_camera()`):** Recibe los bytes JPEG del Relay, los decodifica con OpenCV (`cv2.imdecode`) y actualiza dos atributos en memoria:
* `latest_jpeg`: servido directamente por `/live/{cam_id}` como flujo MJPEG hacia el navegador.
* `latest_bgr`: matriz `numpy.ndarray` lista para ser consumida por modelos de visión artificial (ej. YOLOv8/ONNX) sin decodificar dos veces.


* **Grabación compatible con navegadores (`H264SegmentRecorder`):** Utiliza el binario portable de FFmpeg provisto por `imageio-ffmpeg` para codificar los fotogramas en **H.264 (`libx264`, `yuv420p`)** con el flag `-movflags +faststart`. Cada bloque (`SEGMENT_SECONDS`) se escribe en un archivo temporal `.tmp` dentro de `recordings/<cam_id>/` y se renombra atómicamente a `.mp4` solo al cerrarse, evitando archivos corruptos o errores de metadatos en Firefox/Chrome.
* **Panel Web (`static/index.html`):** Consulta `/api/cameras` y `/api/recordings/{cam_id}` periódicamente, muestra el estado en línea/desconectado por pestañas y libera el socket MJPEG anterior antes de cambiar de cámara o reproducir un `.mp4`.

---

## Puesta en Marcha

### 1. Desplegar el Relay en Render

1. Crea un **Web Service** en Render conectado a este repositorio.
2. Configura **Root Directory** como `cloud-relay`.
3. **Build Command:** `pip install -r requirements.txt`
4. **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

### 2. Ejecución Portable en Windows (Sin Administrador)

Si utilizas el paquete preconfigurado de la sección **Releases** (`Kit_Vigilancia_IoT.7z`, que incluye `tools/` con WinPython 3.12, VS Code Portable y PlatformIO Core offline):

1. Descomprime el paquete en una carpeta de usuario (Escritorio o Documentos).
2. Ejecuta `scripts\abrir_vscode_pio.bat` para editar `CAMERA_ID`, `WIFI_SSID`, `WIFI_PASS` y `RELAY_HOST` en `esp32-firmware\src\main.cpp` y cargar el firmware.
3. Actualiza `RELAY_HOST` en `local-nvr\nvr_local.py` y ejecuta `scripts\iniciar_nvr.bat`.
4. Abre **`http://127.0.0.1:8000`** en el navegador.

*(Los scripts `.bat` utilizan `subst P:` para mapear la carpeta del kit a una unidad virtual fija y evitar que se rompan las rutas absolutas internas de PlatformIO al mover el proyecto entre computadoras).*

### 3. Instalación Manual (Con Python 3.12 ya instalado)

```bash
cd local-nvr
pip install --only-binary :all: -r requirements.txt
python nvr_local.py

```

---

## Extensiones Previstas sobre esta Base

Al ser una arquitectura base abierta, está estructurada para extenderse en tres direcciones directas:

* **Seguridad:** Añadir validación de tokens (`?token=...`) en `/ws/cam/{cam_id}`, `/ws/viewer/{cam_id}` y `/api/cameras` dentro de `cloud-relay/main.py` mediante variables de entorno en Render.
* **Detección en Servidor (YOLO):** Consumir `cameras[cam_id].latest_bgr` dentro de `local-nvr/nvr_local.py` con `onnxruntime` / `ultralytics` para activar grabaciones únicamente ante eventos positivos.
* **Inferencia en el Borde (TinyML):** Integrar un modelo cuantizado (`INT8`) en el ESP32 para mantener la cámara en bajo consumo y abrir el túnel WebSocket solo al detectar presencia.

