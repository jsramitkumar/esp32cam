/*
 * ESP32-CAM UDP Streaming Firmware
 * 
 * Sends JPEG frames over UDP with ACK-based flow control.
 * Supports resolution switching (480p/720p), camera sensor controls,
 * and flash LED control via UDP commands from the streaming server.
 * 
 * Board: AI-Thinker ESP32-CAM
 * 
 * UDP Protocol:
 *   Frame packet (ESP32 -> Server):
 *     [0xCA][0x4D][FrameID:4][ChunkIdx:2][TotalChunks:2][PayloadLen:2][Payload:N]
 *   ACK packet (Server -> ESP32):
 *     [0xAC][0x4B][FrameID:4]
 *   Control packet (Server -> ESP32):
 *     [0xC0][0x4D][CmdType:1][Payload:N]
 */

#include "esp_camera.h"
#include "esp_wifi.h"
#include "WiFi.h"
#include "WiFiUdp.h"
#include "esp_timer.h"
#include "driver/ledc.h"

// ===================== CONFIGURATION =====================

// WiFi credentials - CHANGE THESE
const char* WIFI_SSID     = "Airtel_High Link";
const char* WIFI_PASSWORD = "Shell@1245";

// Streaming server IP and ports - CHANGE THESE
const char* SERVER_IP      = "192.168.1.4";
const uint16_t SERVER_PORT = 9000;    // Server listens for frames
const uint16_t CONTROL_PORT = 9001;   // ESP32 listens for controls
const uint16_t LOCAL_PORT  = 9002;    // ESP32 sends from this port

// UDP chunk size (keep under MTU ~1500)
#define CHUNK_PAYLOAD_SIZE  1400
#define PACKET_HEADER_SIZE  12
#define ACK_TIMEOUT_MS      100       // ACK wait timeout
#define ACK_RETRY_COUNT     2         // Retries before dropping frame
#define FRAME_INTERVAL_MS   33        // ~30fps target

// Flash LED pin (AI-Thinker ESP32-CAM)
#define FLASH_LED_PIN       4
#define FLASH_LED_CHANNEL   LEDC_CHANNEL_7

// ============== AI-Thinker ESP32-CAM Pin Map ==============
#define PWDN_GPIO_NUM       32
#define RESET_GPIO_NUM      -1
#define XCLK_GPIO_NUM       0
#define SIOD_GPIO_NUM       26
#define SIOC_GPIO_NUM       27
#define Y9_GPIO_NUM         35
#define Y8_GPIO_NUM         34
#define Y7_GPIO_NUM         39
#define Y6_GPIO_NUM         36
#define Y5_GPIO_NUM         21
#define Y4_GPIO_NUM         19
#define Y3_GPIO_NUM         18
#define Y2_GPIO_NUM         5
#define VSYNC_GPIO_NUM      25
#define HREF_GPIO_NUM       23
#define PCLK_GPIO_NUM       22

// ==================== PROTOCOL MAGIC ====================
#define MAGIC_FRAME_0       0xCA
#define MAGIC_FRAME_1       0x4D
#define MAGIC_ACK_0         0xAC
#define MAGIC_ACK_1         0x4B
#define MAGIC_CTRL_0        0xC0
#define MAGIC_CTRL_1        0x4D

// ==================== CONTROL COMMANDS ==================
#define CMD_RESOLUTION      0x01
#define CMD_BRIGHTNESS      0x10
#define CMD_CONTRAST        0x11
#define CMD_SATURATION      0x12
#define CMD_SHARPNESS       0x13
#define CMD_AWB             0x20
#define CMD_AWB_GAIN        0x21
#define CMD_WB_MODE         0x22
#define CMD_AEC             0x30
#define CMD_AEC2            0x31
#define CMD_AE_LEVEL        0x32
#define CMD_AEC_VALUE       0x33
#define CMD_AGC             0x40
#define CMD_AGC_GAIN        0x41
#define CMD_GAINCEILING     0x42
#define CMD_BPC             0x50
#define CMD_WPC             0x51
#define CMD_RAW_GMA         0x52
#define CMD_LENC            0x53
#define CMD_HMIRROR         0x60
#define CMD_VFLIP           0x61
#define CMD_DCW             0x62
#define CMD_COLORBAR        0x63
#define CMD_FLASH           0xF0
#define CMD_QUALITY         0x70
#define CMD_SPECIAL_EFFECT  0x71

// ==================== GLOBALS ===========================
WiFiUDP udpSend;       // For sending frames
WiFiUDP udpControl;    // For receiving controls
uint32_t frameId = 0;
uint8_t packetBuffer[PACKET_HEADER_SIZE + CHUNK_PAYLOAD_SIZE];
uint8_t ackBuffer[16];
uint8_t controlBuffer[64];
bool streamActive = true;
uint8_t currentFlashBrightness = 0;

// Frame timing
unsigned long lastFrameTime = 0;
unsigned long frameIntervalMs = FRAME_INTERVAL_MS;

// ==================== SETUP =============================
void setup() {
  Serial.begin(115200);
  Serial.println("\n\n=== ESP32-CAM UDP Streamer ===");
  
  // Initialize flash LED
  setupFlashLED();
  
  // Initialize camera
  if (!initCamera()) {
    Serial.println("Camera init FAILED! Restarting...");
    delay(1000);
    ESP.restart();
  }
  Serial.println("Camera initialized OK");
  
  // Connect WiFi with max power
  connectWiFi();
  
  // Start UDP
  udpSend.begin(LOCAL_PORT);
  udpControl.begin(CONTROL_PORT);
  
  Serial.printf("Streaming to %s:%d\n", SERVER_IP, SERVER_PORT);
  Serial.printf("Listening for controls on port %d\n", CONTROL_PORT);
  Serial.println("=== Streaming Started ===\n");
}

// ==================== MAIN LOOP =========================
void loop() {
  // Handle incoming control commands
  handleControlCommands();
  
  // Frame rate limiting
  unsigned long now = millis();
  if (now - lastFrameTime < frameIntervalMs) {
    return;
  }
  
  if (!streamActive) return;
  
  // Capture frame
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("Frame capture failed");
    return;
  }
  
  // Send frame via UDP with chunking
  bool sent = sendFrame(fb->buf, fb->len);
  
  // Return the frame buffer
  esp_camera_fb_return(fb);
  
  if (sent) {
    lastFrameTime = millis();
  }
}

// ==================== CAMERA INIT =======================
bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_LATEST;  // Always grab latest frame
  
  // Start with 480p (VGA)
  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;  // 640x480
    config.jpeg_quality = 12;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    Serial.println("PSRAM found - using 2 frame buffers");
  } else {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 15;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    Serial.println("No PSRAM - using 1 frame buffer");
  }
  
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init error 0x%x\n", err);
    return false;
  }
  
  // Initial sensor settings for good image quality
  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_brightness(s, 0);
    s->set_contrast(s, 0);
    s->set_saturation(s, 0);
    s->set_sharpness(s, 0);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 1);
    s->set_ae_level(s, 0);
    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_gainceiling(s, (gainceiling_t)6);
    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);
    s->set_hmirror(s, 0);
    s->set_vflip(s, 0);
    s->set_dcw(s, 1);
  }
  
  return true;
}

// ==================== WIFI SETUP ========================
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  
  Serial.printf("Connecting to %s", WIFI_SSID);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    attempts++;
    if (attempts > 40) {
      Serial.println("\nWiFi connect timeout! Restarting...");
      ESP.restart();
    }
  }
  
  Serial.println();
  Serial.printf("Connected! IP: %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
  
  // Set maximum WiFi TX power (20.5 dBm)
  esp_wifi_set_max_tx_power(82);  // 82 = 20.5 dBm (in 0.25 dBm units)
  
  // Disable WiFi power save mode for lowest latency
  esp_wifi_set_ps(WIFI_PS_NONE);
  
  // Set WiFi bandwidth to HT40 for higher throughput
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT40);
  
  Serial.println("WiFi: Max TX power, NO power save, HT40 bandwidth");
}

// ==================== FLASH LED =========================
void setupFlashLED() {
  // Configure LEDC for flash LED PWM control
  ledc_timer_config_t ledc_timer;
  ledc_timer.duty_resolution = LEDC_TIMER_8_BIT;
  ledc_timer.freq_hz = 5000;
  ledc_timer.speed_mode = LEDC_LOW_SPEED_MODE;
  ledc_timer.timer_num = LEDC_TIMER_3;
  ledc_timer.clk_cfg = LEDC_AUTO_CLK;
  ledc_timer_config(&ledc_timer);
  
  ledc_channel_config_t ledc_channel;
  ledc_channel.channel    = FLASH_LED_CHANNEL;
  ledc_channel.duty       = 0;
  ledc_channel.gpio_num   = FLASH_LED_PIN;
  ledc_channel.speed_mode = LEDC_LOW_SPEED_MODE;
  ledc_channel.hpoint     = 0;
  ledc_channel.timer_sel  = LEDC_TIMER_3;
  ledc_channel.intr_type  = LEDC_INTR_DISABLE;
  ledc_channel_config(&ledc_channel);
}

void setFlashBrightness(uint8_t brightness) {
  currentFlashBrightness = brightness;
  ledc_set_duty(LEDC_LOW_SPEED_MODE, FLASH_LED_CHANNEL, brightness);
  ledc_update_duty(LEDC_LOW_SPEED_MODE, FLASH_LED_CHANNEL);
  Serial.printf("Flash brightness: %d\n", brightness);
}

// ==================== UDP FRAME SENDING =================
bool sendFrame(const uint8_t* data, size_t len) {
  uint32_t currentFrameId = frameId++;
  uint16_t totalChunks = (len + CHUNK_PAYLOAD_SIZE - 1) / CHUNK_PAYLOAD_SIZE;
  
  // Send all chunks
  for (uint16_t i = 0; i < totalChunks; i++) {
    size_t offset = (size_t)i * CHUNK_PAYLOAD_SIZE;
    size_t chunkLen = (i == totalChunks - 1) ? (len - offset) : CHUNK_PAYLOAD_SIZE;
    
    // Build packet header
    packetBuffer[0] = MAGIC_FRAME_0;
    packetBuffer[1] = MAGIC_FRAME_1;
    // Frame ID (big-endian)
    packetBuffer[2] = (currentFrameId >> 24) & 0xFF;
    packetBuffer[3] = (currentFrameId >> 16) & 0xFF;
    packetBuffer[4] = (currentFrameId >> 8) & 0xFF;
    packetBuffer[5] = currentFrameId & 0xFF;
    // Chunk index (big-endian)
    packetBuffer[6] = (i >> 8) & 0xFF;
    packetBuffer[7] = i & 0xFF;
    // Total chunks (big-endian)
    packetBuffer[8] = (totalChunks >> 8) & 0xFF;
    packetBuffer[9] = totalChunks & 0xFF;
    // Payload length (big-endian)
    packetBuffer[10] = (chunkLen >> 8) & 0xFF;
    packetBuffer[11] = chunkLen & 0xFF;
    
    // Copy payload
    memcpy(packetBuffer + PACKET_HEADER_SIZE, data + offset, chunkLen);
    
    // Send UDP packet
    udpSend.beginPacket(SERVER_IP, SERVER_PORT);
    udpSend.write(packetBuffer, PACKET_HEADER_SIZE + chunkLen);
    udpSend.endPacket();
    
    // Small delay between chunks to prevent buffer overflow
    if (totalChunks > 10 && i % 5 == 4) {
      delayMicroseconds(200);
    }
  }
  
  // Wait for ACK
  return waitForAck(currentFrameId);
}

bool waitForAck(uint32_t expectedFrameId) {
  for (int retry = 0; retry < ACK_RETRY_COUNT; retry++) {
    unsigned long startWait = millis();
    
    while (millis() - startWait < ACK_TIMEOUT_MS) {
      int packetSize = udpSend.parsePacket();
      if (packetSize >= 6) {
        int len = udpSend.read(ackBuffer, sizeof(ackBuffer));
        if (len >= 6 && ackBuffer[0] == MAGIC_ACK_0 && ackBuffer[1] == MAGIC_ACK_1) {
          uint32_t ackedFrameId = ((uint32_t)ackBuffer[2] << 24) |
                                   ((uint32_t)ackBuffer[3] << 16) |
                                   ((uint32_t)ackBuffer[4] << 8) |
                                   (uint32_t)ackBuffer[5];
          if (ackedFrameId == expectedFrameId) {
            return true;
          }
          // Got ACK for different frame - if newer, also accept
          if (ackedFrameId > expectedFrameId) {
            return true;
          }
        }
      }
      
      // Also check control port while waiting
      handleControlCommands();
      delayMicroseconds(100);
    }
  }
  
  // ACK timeout - proceed anyway to avoid stalling
  return false;
}

// ==================== CONTROL HANDLING ==================
void handleControlCommands() {
  int packetSize = udpControl.parsePacket();
  if (packetSize < 3) return;
  
  int len = udpControl.read(controlBuffer, sizeof(controlBuffer));
  if (len < 3) return;
  
  // Validate magic
  if (controlBuffer[0] != MAGIC_CTRL_0 || controlBuffer[1] != MAGIC_CTRL_1) return;
  
  uint8_t cmd = controlBuffer[2];
  sensor_t* s = esp_camera_sensor_get();
  if (!s && cmd != CMD_FLASH) return;
  
  switch (cmd) {
    case CMD_RESOLUTION: {
      if (len < 4) break;
      uint8_t res = controlBuffer[3];
      framesize_t newSize;
      if (res == 0) {
        newSize = FRAMESIZE_VGA;    // 640x480 (480p)
        Serial.println("Resolution: 480p (VGA)");
      } else if (res == 1) {
        newSize = FRAMESIZE_HD;     // 1280x720 (720p)
        Serial.println("Resolution: 720p (HD)");
      } else {
        break;
      }
      s->set_framesize(s, newSize);
      break;
    }
    
    case CMD_BRIGHTNESS: {
      if (len < 4) break;
      int8_t val = (int8_t)controlBuffer[3];
      s->set_brightness(s, val);
      Serial.printf("Brightness: %d\n", val);
      break;
    }
    
    case CMD_CONTRAST: {
      if (len < 4) break;
      int8_t val = (int8_t)controlBuffer[3];
      s->set_contrast(s, val);
      Serial.printf("Contrast: %d\n", val);
      break;
    }
    
    case CMD_SATURATION: {
      if (len < 4) break;
      int8_t val = (int8_t)controlBuffer[3];
      s->set_saturation(s, val);
      Serial.printf("Saturation: %d\n", val);
      break;
    }
    
    case CMD_SHARPNESS: {
      if (len < 4) break;
      int8_t val = (int8_t)controlBuffer[3];
      s->set_sharpness(s, val);
      Serial.printf("Sharpness: %d\n", val);
      break;
    }
    
    case CMD_AWB: {
      if (len < 4) break;
      s->set_whitebal(s, controlBuffer[3]);
      Serial.printf("AWB: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_AWB_GAIN: {
      if (len < 4) break;
      s->set_awb_gain(s, controlBuffer[3]);
      Serial.printf("AWB Gain: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_WB_MODE: {
      if (len < 4) break;
      s->set_wb_mode(s, controlBuffer[3]);
      Serial.printf("WB Mode: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_AEC: {
      if (len < 4) break;
      s->set_exposure_ctrl(s, controlBuffer[3]);
      Serial.printf("AEC: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_AEC2: {
      if (len < 4) break;
      s->set_aec2(s, controlBuffer[3]);
      Serial.printf("AEC2 (DSP): %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_AE_LEVEL: {
      if (len < 4) break;
      int8_t val = (int8_t)controlBuffer[3];
      s->set_ae_level(s, val);
      Serial.printf("AE Level: %d\n", val);
      break;
    }
    
    case CMD_AEC_VALUE: {
      if (len < 5) break;
      uint16_t val = ((uint16_t)controlBuffer[3] << 8) | controlBuffer[4];
      s->set_aec_value(s, val);
      Serial.printf("AEC Value: %d\n", val);
      break;
    }
    
    case CMD_AGC: {
      if (len < 4) break;
      s->set_gain_ctrl(s, controlBuffer[3]);
      Serial.printf("AGC: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_AGC_GAIN: {
      if (len < 4) break;
      s->set_agc_gain(s, controlBuffer[3]);
      Serial.printf("AGC Gain: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_GAINCEILING: {
      if (len < 4) break;
      s->set_gainceiling(s, (gainceiling_t)controlBuffer[3]);
      Serial.printf("Gain Ceiling: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_BPC: {
      if (len < 4) break;
      s->set_bpc(s, controlBuffer[3]);
      Serial.printf("BPC: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_WPC: {
      if (len < 4) break;
      s->set_wpc(s, controlBuffer[3]);
      Serial.printf("WPC: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_RAW_GMA: {
      if (len < 4) break;
      s->set_raw_gma(s, controlBuffer[3]);
      Serial.printf("Raw GMA: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_LENC: {
      if (len < 4) break;
      s->set_lenc(s, controlBuffer[3]);
      Serial.printf("Lens Correction: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_HMIRROR: {
      if (len < 4) break;
      s->set_hmirror(s, controlBuffer[3]);
      Serial.printf("H-Mirror: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_VFLIP: {
      if (len < 4) break;
      s->set_vflip(s, controlBuffer[3]);
      Serial.printf("V-Flip: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_DCW: {
      if (len < 4) break;
      s->set_dcw(s, controlBuffer[3]);
      Serial.printf("DCW: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_COLORBAR: {
      if (len < 4) break;
      s->set_colorbar(s, controlBuffer[3]);
      Serial.printf("Color Bar: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_QUALITY: {
      if (len < 4) break;
      uint8_t q = controlBuffer[3];
      if (q < 4) q = 4;
      if (q > 63) q = 63;
      s->set_quality(s, q);
      Serial.printf("JPEG Quality: %d\n", q);
      break;
    }
    
    case CMD_SPECIAL_EFFECT: {
      if (len < 4) break;
      s->set_special_effect(s, controlBuffer[3]);
      Serial.printf("Special Effect: %d\n", controlBuffer[3]);
      break;
    }
    
    case CMD_FLASH: {
      if (len < 4) break;
      setFlashBrightness(controlBuffer[3]);
      break;
    }
    
    default:
      Serial.printf("Unknown command: 0x%02X\n", cmd);
      break;
  }
}
