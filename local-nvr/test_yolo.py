import time
import cv2
from pathlib import Path
from ultralytics import YOLO

# 1. Carga modelo preentrenado (descarga ~5 MB la primera vez)
model = YOLO("yolo26n.pt")

# 2. Toma automáticamente un frame del primer .mp4 grabado (o usa cualquier .jpg local)
videos = list(Path("recordings").rglob("*.mp4"))
if not videos:
    raise FileNotFoundError("No hay archivos .mp4 en recordings/. Graba unos segundos o indica una imagen.")

cap = cv2.VideoCapture(str(videos[0]))
ret, frame = cap.read()
cap.release()
if not ret:
    raise RuntimeError("No se pudo leer el fotograma del video.")

# 3. Inferencia de prueba (imgsz=320 es ideal para VGA del ESP32-CAM en CPU)
t0 = time.perf_counter()
results = model.predict(frame, imgsz=320, conf=0.4, device="cpu", verbose=False)
ms = (time.perf_counter() - t0) * 1000

res = results[0]
print(f"Tiempo de inferencia CPU: {ms:.1f} ms")
for box in res.boxes:
    cls_name = model.names[int(box.cls[0])]
    conf = float(box.conf[0])
    print(f" - Detectado: {cls_name} ({conf:.2f})")

# 4. Guarda la imagen anotada para verificación visual
cv2.imwrite("test_resultado_yolo.jpg", res.plot())
print("Imagen guardada en: test_resultado_yolo.jpg")