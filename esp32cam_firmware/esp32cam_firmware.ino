/*
 * ESP32-CAM FPV UDP Streaming Firmware
 * Board  : AI-Thinker ESP32-CAM (OV2640)
 *
 * Frame flow:
 *   ESP32 captures JPEG → chunks over UDP → server sends ACK → next frame
 *   Frame buffer is released BEFORE UDP send (no FB-OVF)
 *   CAMERA_GRAB_LATEST ensures stale frames are auto-discarded
 *
 * Protocols:
 *   Frame  ESP32→Server : [0xCA][0x4D][FrameID:4BE][ChunkIdx:2BE][TotalChunks:2BE][PayloadLen:2BE][Data:N]
 *   ACK    Server→ESP32 : [0xAC][0x4B][FrameID:4BE]
 *   Ctrl   Server→ESP32 : [0xC0][0x4E][CmdID:1][Payload:N]
 *   Telem  ESP32→Server : [0x54][0x45][NameLen:1][Name:N][RSSI:int8][HeapKB:2BE][Uptime:4BE][FPS:1][Res:1]
 */

#include "esp_camera.h"
#include "esp_wifi.h"
#include "WiFi.h"
#include "WiFiUdp.h"
#include "driver/ledc.h"
#include "esp_heap_caps.h"

// ─────────────────── USER CONFIG ───────────────────
#define CAMERA_NAME      "Drone-FPV-1"      // Unique name shown in NVR

const char* WIFI_SSID     = "Airtel_High Link";     // ← change
const char* WIFI_PASSWORD = "Shell@1245"; // ← change
const char* SERVER_IP     = "192.168.1.4";// ← change to server PC IP

const uint16_t SERVER_PORT  = 9000;   // server UDP recv
const uint16_t CONTROL_PORT = 9001;   // ESP32 listens for commands
const uint16_t LOCAL_PORT   = 9002;   // ESP32 send / ACK recv port

// ─────────────────── TUNING ────────────────────────
#define CHUNK_SIZE       1400          // keep under Ethernet MTU 1500
#define HEADER_SIZE      12
#define ACK_TIMEOUT_MS   40            // ms per retry
#define ACK_RETRIES      1             // 1 retry max
#define FRAME_INTERVAL   40            // ms → ~25 fps (slack for WiFi jitter)
#define TELEM_EVERY      30            // frames between telemetry sends
#define WIFI_CHECK_MS    5000          // WiFi watchdog check interval

// ─────────────────── HARDWARE ──────────────────────
#define FLASH_PIN        4
#define FLASH_CHANNEL    LEDC_CHANNEL_7
#define FLASH_TIMER      LEDC_TIMER_3

// AI-Thinker pin map
#define PWDN_GPIO_NUM    32
#define RESET_GPIO_NUM   -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM    26
#define SIOC_GPIO_NUM    27
#define Y9_GPIO_NUM      35
#define Y8_GPIO_NUM      34
#define Y7_GPIO_NUM      39
#define Y6_GPIO_NUM      36
#define Y5_GPIO_NUM      21
#define Y4_GPIO_NUM      19
#define Y3_GPIO_NUM      18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM   25
#define HREF_GPIO_NUM    23
#define PCLK_GPIO_NUM    22

// ─────────────────── PROTOCOL MAGIC ────────────────
#define M_FRAME0  0xCA
#define M_FRAME1  0x4D
#define M_ACK0    0xAC
#define M_ACK1    0x4B
#define M_CTRL0   0xC0
#define M_CTRL1   0x4E
#define M_TELE0   0x54
#define M_TELE1   0x45

// ─────────────────── COMMANDS ──────────────────────
#define CMD_RESOLUTION   0x01
#define CMD_QUALITY      0x02
#define CMD_BRIGHTNESS   0x10
#define CMD_CONTRAST     0x11
#define CMD_SATURATION   0x12
#define CMD_SHARPNESS    0x13
#define CMD_AWB          0x20
#define CMD_AWB_GAIN     0x21
#define CMD_WB_MODE      0x22
#define CMD_AEC          0x30
#define CMD_AEC2         0x31
#define CMD_AE_LEVEL     0x32
#define CMD_AEC_VALUE    0x33  // 2-byte BE
#define CMD_AGC          0x40
#define CMD_AGC_GAIN     0x41
#define CMD_GAINCEILING  0x42
#define CMD_BPC          0x50
#define CMD_WPC          0x51
#define CMD_RAW_GMA      0x52
#define CMD_LENC         0x53
#define CMD_HMIRROR      0x60
#define CMD_VFLIP        0x61
#define CMD_DCW          0x62
#define CMD_SPECIAL      0x63
#define CMD_FLASH        0xF0

// ─────────────────── GLOBALS ───────────────────────
WiFiUDP udpSend;
WiFiUDP udpCtrl;
uint32_t frameId          = 0;
uint32_t fpsCount         = 0;
unsigned long fpsTimer    = 0;
uint8_t  fpsValue         = 0;
unsigned long lastFrame   = 0;
unsigned long lastWifiChk = 0;
unsigned long framePeriod = FRAME_INTERVAL;
bool streamActive         = true;

// Pre-allocated send buffer – avoids per-frame heap_caps_malloc/free
// which causes PSRAM fragmentation and eventual alloc failure after ~2 min
#define JPEG_BUF_SIZE   (80 * 1024)     // 80 KB covers VGA q=10 worst case
static uint8_t* jpegBuf = nullptr;      // allocated once in setup()

uint8_t pktBuf[HEADER_SIZE + CHUNK_SIZE];
uint8_t ackBuf[16];
uint8_t ctrlBuf[64];

// ═══════════════════ SETUP ═══════════════════════
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n\n=== ESP32-CAM FPV Streamer ===");

  initFlash();
  if (!initCamera()) { Serial.println("CAMERA FAIL – reboot"); delay(1000); ESP.restart(); }
  connectWiFi();

  udpSend.begin(LOCAL_PORT);
  udpCtrl.begin(CONTROL_PORT);

  // Allocate the reusable JPEG staging buffer once.
  // PSRAM is preferred; fall back to DRAM if unavailable.
  jpegBuf = (uint8_t*)heap_caps_malloc(
    JPEG_BUF_SIZE,
    psramFound() ? MALLOC_CAP_SPIRAM : MALLOC_CAP_DEFAULT);
  if (!jpegBuf) {
    Serial.println("WARN: jpegBuf alloc failed, trying DRAM fallback");
    jpegBuf = (uint8_t*)malloc(JPEG_BUF_SIZE);
  }
  if (!jpegBuf) { Serial.println("FATAL: no buffer – reboot"); delay(500); ESP.restart(); }

  Serial.printf("Target: %s:%u\n", SERVER_IP, SERVER_PORT);
  Serial.println("=== Ready ===\n");
}

// ═══════════════════ LOOP ════════════════════════
//
// Design:
//   • fb_get() every tick   → DMA ring always drained → no FB-OVF
//   • pre-alloc jpegBuf     → no heap fragmentation   → no 2-min crash
//   • WiFi watchdog          → auto-reconnect on silent deauth
//
void loop() {
  // ── WiFi watchdog ─────────────────────────────────
  unsigned long now = millis();
  if (now - lastWifiChk >= WIFI_CHECK_MS) {
    lastWifiChk = now;
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi lost – reconnecting…");
      WiFi.disconnect(true);
      delay(100);
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      unsigned long t0 = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
        delay(500); Serial.print(".");
      }
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\nReconnected: %s\n", WiFi.localIP().toString().c_str());
        esp_wifi_set_ps(WIFI_PS_NONE);
        esp_wifi_set_max_tx_power(82);
        udpSend.begin(LOCAL_PORT);
        udpCtrl.begin(CONTROL_PORT);
      } else {
        Serial.println("\nReconnect failed – retrying next cycle");
      }
      return; // skip this tick to let stack settle
    }
  }

  handleControl();

  // Always grab the newest frame – even if we don't send it.
  // This drains the DMA ring continuously and prevents FB-OVF.
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) { delay(1); return; }

  bool shouldSend = ((millis() - lastFrame) >= framePeriod) && streamActive;

  if (!shouldSend) {
    esp_camera_fb_return(fb);   // discard – just keep DMA moving
    return;
  }

  // ── Copy JPEG into pre-allocated buffer & release DMA immediately ──
  size_t jpegLen = fb->len;
  if (jpegLen > JPEG_BUF_SIZE) {
    // Frame too large for buffer (shouldn't happen at VGA q=10)
    // Just skip this frame – don't stall
    esp_camera_fb_return(fb);
    lastFrame = millis();
    Serial.printf("SKIP: frame %u bytes > buf %u\n", jpegLen, JPEG_BUF_SIZE);
    return;
  }
  memcpy(jpegBuf, fb->buf, jpegLen);
  esp_camera_fb_return(fb);    // ← free DMA slot BEFORE any network I/O

  // ── Send ─────────────────────────────────────────
  sendFrame(jpegBuf, jpegLen); // ACK result ignored – always advance timer
  lastFrame = millis();        // always update – prevents tight retry storm

  // ── FPS counter ──────────────────────────────────
  fpsCount++;
  if (millis() - fpsTimer >= 1000) {
    fpsValue  = (uint8_t)min((uint32_t)255, fpsCount);
    fpsCount  = 0;
    fpsTimer  = millis();
  }

  // ── Telemetry ────────────────────────────────────
  if (frameId > 0 && frameId % TELEM_EVERY == 0) sendTelemetry();
}

// ═══════════════════ CAMERA INIT ═════════════════
bool initCamera() {
  camera_config_t cfg;
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.ledc_timer   = LEDC_TIMER_0;
  cfg.pin_d0 = Y2_GPIO_NUM; cfg.pin_d1 = Y3_GPIO_NUM;
  cfg.pin_d2 = Y4_GPIO_NUM; cfg.pin_d3 = Y5_GPIO_NUM;
  cfg.pin_d4 = Y6_GPIO_NUM; cfg.pin_d5 = Y7_GPIO_NUM;
  cfg.pin_d6 = Y8_GPIO_NUM; cfg.pin_d7 = Y9_GPIO_NUM;
  cfg.pin_xclk     = XCLK_GPIO_NUM;
  cfg.pin_pclk     = PCLK_GPIO_NUM;
  cfg.pin_vsync    = VSYNC_GPIO_NUM;
  cfg.pin_href     = HREF_GPIO_NUM;
  cfg.pin_sccb_sda = SIOD_GPIO_NUM;
  cfg.pin_sccb_scl = SIOC_GPIO_NUM;
  cfg.pin_pwdn     = PWDN_GPIO_NUM;
  cfg.pin_reset    = RESET_GPIO_NUM;
  cfg.xclk_freq_hz = 20000000;
  cfg.pixel_format = PIXFORMAT_JPEG;
  cfg.grab_mode    = CAMERA_GRAB_LATEST; // always newest frame

  if (psramFound()) {
    cfg.frame_size   = FRAMESIZE_VGA;
    cfg.jpeg_quality = 12;          // slightly larger = fewer chunks = faster ACK
    cfg.fb_count     = 3;           // 3 slots: 1 being copied, 2 for camera
    cfg.fb_location  = CAMERA_FB_IN_PSRAM;
    Serial.println("PSRAM → 3 frame buffers");
  } else {
    cfg.frame_size   = FRAMESIZE_VGA;
    cfg.jpeg_quality = 15;
    cfg.fb_count     = 2;           // 2 DRAM slots
    cfg.fb_location  = CAMERA_FB_IN_DRAM;
    Serial.println("DRAM → 2 frame buffers");
  }

  if (esp_camera_init(&cfg) != ESP_OK) return false;

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);
    s->set_dcw(s, 1);
    s->set_gainceiling(s, (gainceiling_t)6);
  }
  return true;
}

// ═══════════════════ WIFI ════════════════════════
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to %s", WIFI_SSID);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500); Serial.print(".");
    if (++tries > 40) { Serial.println("\nWiFi timeout – reboot"); ESP.restart(); }
  }
  Serial.printf("\nIP: %s  RSSI: %d dBm\n",
    WiFi.localIP().toString().c_str(), WiFi.RSSI());

  esp_wifi_set_ps(WIFI_PS_NONE);          // no power save
  esp_wifi_set_max_tx_power(82);          // 20.5 dBm (max)
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT40);
}

// ═══════════════════ FLASH LED ════════════════════
void initFlash() {
  ledc_timer_config_t t{};
  t.duty_resolution = LEDC_TIMER_8_BIT;
  t.freq_hz         = 5000;
  t.speed_mode      = LEDC_LOW_SPEED_MODE;
  t.timer_num       = FLASH_TIMER;
  t.clk_cfg         = LEDC_AUTO_CLK;
  ledc_timer_config(&t);

  ledc_channel_config_t c{};
  c.channel    = FLASH_CHANNEL;
  c.duty       = 0;
  c.gpio_num   = FLASH_PIN;
  c.speed_mode = LEDC_LOW_SPEED_MODE;
  c.hpoint     = 0;
  c.timer_sel  = FLASH_TIMER;
  c.intr_type  = LEDC_INTR_DISABLE;
  ledc_channel_config(&c);
}

void setFlash(uint8_t v) {
  ledc_set_duty(LEDC_LOW_SPEED_MODE, FLASH_CHANNEL, v);
  ledc_update_duty(LEDC_LOW_SPEED_MODE, FLASH_CHANNEL);
}

// ═══════════════════ TELEMETRY ════════════════════
void sendTelemetry() {
  uint8_t buf[64]; uint8_t pos = 0;
  uint8_t nlen = (uint8_t)min((int)strlen(CAMERA_NAME), 20);
  buf[pos++] = M_TELE0; buf[pos++] = M_TELE1;
  buf[pos++] = nlen;
  memcpy(buf + pos, CAMERA_NAME, nlen); pos += nlen;

  int8_t rssi = (int8_t)constrain(WiFi.RSSI(), -128, 127);
  buf[pos++] = (uint8_t)rssi;

  uint16_t heap = (uint16_t)(ESP.getFreeHeap() / 1024);
  buf[pos++] = (heap >> 8) & 0xFF;
  buf[pos++] =  heap       & 0xFF;

  uint32_t up = millis() / 1000;
  buf[pos++] = (up >> 24) & 0xFF; buf[pos++] = (up >> 16) & 0xFF;
  buf[pos++] = (up >>  8) & 0xFF; buf[pos++] =  up        & 0xFF;

  buf[pos++] = fpsValue;

  sensor_t* s = esp_camera_sensor_get();
  buf[pos++] = (s && s->status.framesize == FRAMESIZE_HD) ? 1 : 0;

  udpSend.beginPacket(SERVER_IP, SERVER_PORT);
  udpSend.write(buf, pos);
  udpSend.endPacket();
}

// ═══════════════════ UDP SEND / ACK ══════════════
bool sendFrame(const uint8_t* data, size_t len) {
  uint32_t fid = frameId++;
  uint16_t total = (uint16_t)((len + CHUNK_SIZE - 1) / CHUNK_SIZE);

  for (uint16_t i = 0; i < total; i++) {
    size_t off      = (size_t)i * CHUNK_SIZE;
    size_t clen     = (i == total - 1) ? (len - off) : CHUNK_SIZE;

    pktBuf[0] = M_FRAME0; pktBuf[1] = M_FRAME1;
    pktBuf[2] = (fid >> 24) & 0xFF; pktBuf[3] = (fid >> 16) & 0xFF;
    pktBuf[4] = (fid >>  8) & 0xFF; pktBuf[5] =  fid        & 0xFF;
    pktBuf[6] = (i   >>  8) & 0xFF; pktBuf[7] =  i          & 0xFF;
    pktBuf[8] = (total >> 8) & 0xFF; pktBuf[9] = total       & 0xFF;
    pktBuf[10]= (clen >>  8) & 0xFF; pktBuf[11]=  clen       & 0xFF;
    memcpy(pktBuf + HEADER_SIZE, data + off, clen);

    udpSend.beginPacket(SERVER_IP, SERVER_PORT);
    udpSend.write(pktBuf, HEADER_SIZE + clen);
    udpSend.endPacket();
    // No per-chunk delay: rely on UDP stack flow control.
    // Delays here block IRQ processing and cause WiFi stack stalls.
  }

  // Drain any stale ACKs from previous frames before waiting for this one.
  // Stale ACKs for old frame IDs would be accepted by waitAck() prematurely.
  while (udpSend.parsePacket() > 0) { udpSend.flush(); }

  return waitAck(fid);
}

bool waitAck(uint32_t expected) {
  for (int r = 0; r < ACK_RETRIES; r++) {
    unsigned long t0 = millis();
    while (millis() - t0 < ACK_TIMEOUT_MS) {
      int sz = udpSend.parsePacket();
      if (sz >= 6) {
        int n = udpSend.read(ackBuf, sizeof(ackBuf));
        if (n >= 6 && ackBuf[0] == M_ACK0 && ackBuf[1] == M_ACK1) {
          uint32_t acked = ((uint32_t)ackBuf[2] << 24) | ((uint32_t)ackBuf[3] << 16)
                         | ((uint32_t)ackBuf[4] <<  8) |  (uint32_t)ackBuf[5];
          if (acked >= expected) return true;
        }
      }
      handleControl();
      delayMicroseconds(200);
    }
  }
  return false; // timeout → proceed, don't stall
}

// ═══════════════════ CONTROL ══════════════════════
void handleControl() {
  int sz = udpCtrl.parsePacket();
  if (sz < 3) return;
  int n = udpCtrl.read(ctrlBuf, sizeof(ctrlBuf));
  if (n < 3 || ctrlBuf[0] != M_CTRL0 || ctrlBuf[1] != M_CTRL1) return;

  uint8_t cmd = ctrlBuf[2];
  sensor_t* s = esp_camera_sensor_get();

  switch (cmd) {
    case CMD_RESOLUTION:
      if (n < 4 || !s) break;
      s->set_framesize(s, ctrlBuf[3] == 1 ? FRAMESIZE_HD : FRAMESIZE_VGA);
      Serial.printf("Res → %s\n", ctrlBuf[3] == 1 ? "720p" : "480p");
      break;
    case CMD_QUALITY:
      if (n < 4 || !s) break; s->set_quality(s, ctrlBuf[3]); break;
    case CMD_BRIGHTNESS:
      if (n < 4 || !s) break; s->set_brightness(s, (int8_t)ctrlBuf[3]); break;
    case CMD_CONTRAST:
      if (n < 4 || !s) break; s->set_contrast(s, (int8_t)ctrlBuf[3]); break;
    case CMD_SATURATION:
      if (n < 4 || !s) break; s->set_saturation(s, (int8_t)ctrlBuf[3]); break;
    case CMD_SHARPNESS:
      if (n < 4 || !s) break; s->set_sharpness(s, (int8_t)ctrlBuf[3]); break;
    case CMD_AWB:
      if (n < 4 || !s) break; s->set_whitebal(s, ctrlBuf[3]); break;
    case CMD_AWB_GAIN:
      if (n < 4 || !s) break; s->set_awb_gain(s, ctrlBuf[3]); break;
    case CMD_WB_MODE:
      if (n < 4 || !s) break; s->set_wb_mode(s, ctrlBuf[3]); break;
    case CMD_AEC:
      if (n < 4 || !s) break; s->set_exposure_ctrl(s, ctrlBuf[3]); break;
    case CMD_AEC2:
      if (n < 4 || !s) break; s->set_aec2(s, ctrlBuf[3]); break;
    case CMD_AE_LEVEL:
      if (n < 4 || !s) break; s->set_ae_level(s, (int8_t)ctrlBuf[3]); break;
    case CMD_AEC_VALUE:
      if (n < 5 || !s) break;
      s->set_aec_value(s, ((uint16_t)ctrlBuf[3] << 8) | ctrlBuf[4]); break;
    case CMD_AGC:
      if (n < 4 || !s) break; s->set_gain_ctrl(s, ctrlBuf[3]); break;
    case CMD_AGC_GAIN:
      if (n < 4 || !s) break; s->set_agc_gain(s, ctrlBuf[3]); break;
    case CMD_GAINCEILING:
      if (n < 4 || !s) break;
      s->set_gainceiling(s, (gainceiling_t)ctrlBuf[3]); break;
    case CMD_BPC:
      if (n < 4 || !s) break; s->set_bpc(s, ctrlBuf[3]); break;
    case CMD_WPC:
      if (n < 4 || !s) break; s->set_wpc(s, ctrlBuf[3]); break;
    case CMD_RAW_GMA:
      if (n < 4 || !s) break; s->set_raw_gma(s, ctrlBuf[3]); break;
    case CMD_LENC:
      if (n < 4 || !s) break; s->set_lenc(s, ctrlBuf[3]); break;
    case CMD_HMIRROR:
      if (n < 4 || !s) break; s->set_hmirror(s, ctrlBuf[3]); break;
    case CMD_VFLIP:
      if (n < 4 || !s) break; s->set_vflip(s, ctrlBuf[3]); break;
    case CMD_DCW:
      if (n < 4 || !s) break; s->set_dcw(s, ctrlBuf[3]); break;
    case CMD_SPECIAL:
      if (n < 4 || !s) break; s->set_special_effect(s, ctrlBuf[3]); break;
    case CMD_FLASH:
      if (n < 4) break; setFlash(ctrlBuf[3]);
      Serial.printf("Flash → %d\n", ctrlBuf[3]); break;
    default: break;
  }
}
