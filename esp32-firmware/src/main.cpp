#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include "esp_camera.h"
#include "mbedtls/sha1.h"
#include "mbedtls/base64.h"

// ================= CONFIGURACIÓN =================
#define CAMERA_ID "cam3"
static const char* WIFI_SSID   = "Alumnos";
static const char* WIFI_PASS   = "";

static const char* CAM_TOKEN = "PEGA_AQUI_TU_CAM_TOKEN";

static const char* RELAY_HOST  = "camera-privserver.onrender.com"; // Sin https:// ni rutas
static const uint16_t RELAY_PORT = 443;
static const char* RELAY_PATH    = "/ws/cam/" CAMERA_ID;

static const uint32_t FRAME_DELAY_MS = 200; // ~10 FPS
// =================================================

// Pines AI-Thinker ESP32-CAM
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

static WiFiClientSecure tlsClient;

static String base64Encode(const uint8_t* data, size_t len) {
    size_t outLen = 0;
    mbedtls_base64_encode(nullptr, 0, &outLen, data, len);
    String out;
    out.reserve(outLen + 1);
    char* buf = new char[outLen + 1];
    if (mbedtls_base64_encode((unsigned char*)buf, outLen + 1, &outLen, data, len) == 0) {
        buf[outLen] = '\0';
        out = String(buf);
    }
    delete[] buf;
    return out;
}

static bool initCamera() {
    if (!psramFound()) {
        Serial.println("[ERR] PSRAM no encontrada.");
        return false;
    }

    camera_config_t cfg = {};
    cfg.ledc_channel = LEDC_CHANNEL_0;
    cfg.ledc_timer   = LEDC_TIMER_0;
    cfg.pin_d0       = Y2_GPIO_NUM;
    cfg.pin_d1       = Y3_GPIO_NUM;
    cfg.pin_d2       = Y4_GPIO_NUM;
    cfg.pin_d3       = Y5_GPIO_NUM;
    cfg.pin_d4       = Y6_GPIO_NUM;
    cfg.pin_d5       = Y7_GPIO_NUM;
    cfg.pin_d6       = Y8_GPIO_NUM;
    cfg.pin_d7       = Y9_GPIO_NUM;
    cfg.pin_xclk     = XCLK_GPIO_NUM;
    cfg.pin_pclk     = PCLK_GPIO_NUM;
    cfg.pin_vsync    = VSYNC_GPIO_NUM;
    cfg.pin_href     = HREF_GPIO_NUM;
    cfg.pin_sccb_sda = SIOD_GPIO_NUM;
    cfg.pin_sccb_scl = SIOC_GPIO_NUM;
    cfg.pin_pwdn     = PWDN_GPIO_NUM;
    cfg.pin_reset    = RESET_GPIO_NUM;

    cfg.xclk_freq_hz = 10000000;          // 10 MHz para reducir sobrecarga DMA/FB-OVF
    cfg.pixel_format = PIXFORMAT_JPEG;
    cfg.frame_size   = FRAMESIZE_VGA;     // Usa FRAMESIZE_QVGA si persiste FB-OVF
    cfg.jpeg_quality = 16;                // 10-63 (mayor valor = más compresión)
    cfg.fb_count     = 2;
    cfg.fb_location  = CAMERA_FB_IN_PSRAM;
    cfg.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        Serial.printf("[ERR] esp_camera_init falló: 0x%x\n", err);
        return false;
    }
    return true;
}

static void connectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(WIFI_SSID,WIFI_PASS);

    Serial.printf("[WIFI] Conectando a %s", WIFI_SSID);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[WIFI] IP: %s\n", WiFi.localIP().toString().c_str());
}

// Despierta el servicio gratuito de Render antes de abrir el WebSocket
static void wakeRenderService() {
    WiFiClientSecure wakeClient;
    wakeClient.setInsecure();
    wakeClient.setTimeout(15);

    Serial.println("[RENDER] Verificando estado del servicio...");
    if (wakeClient.connect(RELAY_HOST, RELAY_PORT)) {
        wakeClient.printf("GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", RELAY_HOST);
        uint32_t start = millis();
        while (wakeClient.connected() && millis() - start < 15000) {
            while (wakeClient.available()) {
                wakeClient.read();
            }
            delay(50);
        }
        wakeClient.stop();
    }
}

static bool connectWebSocket() {
    tlsClient.stop();
    tlsClient.setInsecure();
    tlsClient.setTimeout(10);

    if (!tlsClient.connect(RELAY_HOST, RELAY_PORT)) {
        Serial.println("[WSS] Fallo de conexión TCP/TLS al host.");
        return false;
    }

    uint8_t nonce[16];
    for (int i = 0; i < 16; i++) {
        nonce[i] = (uint8_t)esp_random();
    }
    String wsKey = base64Encode(nonce, sizeof(nonce));

    tlsClient.printf(
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "X-API-Token: %s\r\n\r\n",
        RELAY_PATH, RELAY_HOST, wsKey.c_str(), CAM_TOKEN
    );

    uint32_t start = millis();
    String response;
    bool got101 = false;

    while (millis() - start < 10000) {
        while (tlsClient.available()) {
            String line = tlsClient.readStringUntil('\n');
            response += line + "\n";
            if (line.startsWith("HTTP/1.1 101") || line.startsWith("HTTP/1.0 101")) {
                got101 = true;
            }
            if (line == "\r" || line.length() == 0) {
                if (got101) {
                    Serial.println("[WSS] Handshake 101 Switching Protocols completado.");
                    return true;
                }
                Serial.println("[WSS] Respuesta HTTP inesperada:");
                Serial.println(response);
                tlsClient.stop();
                return false;
            }
        }
        delay(10);
    }

    Serial.println("[WSS] Timeout esperando respuesta 101 de Render.");
    tlsClient.stop();
    return false;
}

// Envía una trama binaria WebSocket enmascarada (RFC 6455)
static bool sendWsBinaryFrame(const uint8_t* payload, size_t len) {
    if (!tlsClient.connected() || payload == nullptr || len == 0) {
        return false;
    }

    uint8_t header[14];
    size_t hIdx = 0;

    header[hIdx++] = 0x82; // FIN = 1, Opcode = 0x2 (Binary frame)

    uint8_t maskBit = 0x80; // Cliente siempre debe enmascarar
    if (len < 126) {
        header[hIdx++] = maskBit | (uint8_t)len;
    } else if (len <= 0xFFFF) {
        header[hIdx++] = maskBit | 126;
        header[hIdx++] = (uint8_t)((len >> 8) & 0xFF);
        header[hIdx++] = (uint8_t)(len & 0xFF);
    } else {
        header[hIdx++] = maskBit | 127;
        for (int i = 7; i >= 0; i--) {
            header[hIdx++] = (uint8_t)((len >> (i * 8)) & 0xFF);
        }
    }

    uint8_t maskKey[4];
    for (int i = 0; i < 4; i++) {
        maskKey[i] = (uint8_t)esp_random();
        header[hIdx++] = maskKey[i];
    }

    if (tlsClient.write(header, hIdx) != hIdx) {
        return false;
    }

    // Enviar payload en bloques aplicando máscara XOR sin duplicar todo el frame en RAM
    const size_t CHUNK_SIZE = 1024;
    uint8_t chunkBuf[CHUNK_SIZE];

    size_t sent = 0;
    while (sent < len) {
        size_t toSend = (len - sent > CHUNK_SIZE) ? CHUNK_SIZE : (len - sent);
        for (size_t i = 0; i < toSend; i++) {
            chunkBuf[i] = payload[sent + i] ^ maskKey[(sent + i) % 4];
        }
        size_t written = tlsClient.write(chunkBuf, toSend);
        if (written == 0) {
            return false;
        }
        sent += written;
    }

    return true;
}

void setup() {
    Serial.begin(115200);
    delay(300);

    if (!initCamera()) {
        while (true) {
            delay(1000);
        }
    }

    connectWiFi();
    wakeRenderService();
}

void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        connectWiFi();
    }

    if (!tlsClient.connected()) {
        if (!connectWebSocket()) {
            delay(3000);
            return;
        }
    }

    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
        delay(20);
        return;
    }

    bool ok = sendWsBinaryFrame(fb->buf, fb->len);
    esp_camera_fb_return(fb);

    if (!ok) {
        Serial.println("[WSS] Error enviando frame; reconectando...");
        tlsClient.stop();
        delay(1000);
        return;
    }

    delay(FRAME_DELAY_MS);
}