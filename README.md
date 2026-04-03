# ESP32-CAM FPV Streaming System

Real-time FPV streaming from ESP32-CAM over UDP with ACK-based flow control, WebSocket browser streaming, and full camera controls.

## Architecture

```
ESP32-CAM ──UDP frames──► Streaming Server ──WebSocket──► Browser UI
           ◄──UDP ACK────┘                ◄──WS controls──┘
           ◄──UDP control─┘
```

- **ESP32-CAM** captures JPEG frames and sends them as chunked UDP packets
- **Server** reassembles frames, sends ACK, discards stale frames, pushes latest to browser via WebSocket
- **Browser** displays the live stream with full camera sensor controls

## Protocol

| Direction | Format |
|-----------|--------|
| Frame (ESP32→Server) | `[0xCA 0x4D][FrameID:4][ChunkIdx:2][TotalChunks:2][PayloadLen:2][Data]` |
| ACK (Server→ESP32)   | `[0xAC 0x4B][FrameID:4]` |
| Control (Server→ESP32)| `[0xC0 0x4D][CmdType:1][Payload:N]` |

Frames are chunked into ~1400-byte UDP packets to stay under MTU. ESP32 sends all chunks of a frame, then waits for ACK before sending the next frame.

## Setup

### 1. ESP32-CAM Firmware

**Requirements:**
- Arduino IDE or PlatformIO
- ESP32 board package installed
- AI-Thinker ESP32-CAM board

**Steps:**

1. Open `esp32cam_firmware/esp32cam_firmware.ino` in Arduino IDE
2. Edit these settings at the top of the file:
   ```cpp
   const char* WIFI_SSID     = "YOUR_WIFI_SSID";
   const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
   const char* SERVER_IP     = "192.168.1.100";  // Your PC's IP
   ```
3. Select board: **AI Thinker ESP32-CAM**
4. Upload to ESP32-CAM

### 2. Streaming Server

**Requirements:**
- Python 3.10+
- aiohttp

**Steps:**

```bash
cd streaming_server
pip install -r requirements.txt
python server.py
```

**Options:**
```
--host       Web server bind address (default: 0.0.0.0)
--web-port   Web UI port (default: 8080)
--udp-port   UDP frame receive port (default: 9000)
--control-port  ESP32 control listen port (default: 9001)
--esp32-port    ESP32 source port (default: 9002)
```

### 3. Open Browser

Navigate to `http://localhost:8080`

## Features

### Stream
- UDP-based frame transport for low latency
- ACK-based flow control (send frame → wait ACK → send next)
- Stale frame discard — only the latest frame is displayed
- Chunked UDP packets (1400B) for reliable transport under MTU

### Camera Controls (via Web UI)
- **Resolution:** Toggle between 480p (640×480) and 720p (1280×720)
- **Flash LED:** On/Off toggle with brightness slider (0-255 PWM)
- **Image:** Brightness, Contrast, Saturation, Sharpness, Special Effects
- **White Balance:** AWB, AWB Gain, WB Mode (Auto/Sunny/Cloudy/Office/Home)
- **Exposure:** Auto Exposure, AEC DSP, AE Level, Manual Exposure
- **Gain:** Auto Gain, Manual Gain, Gain Ceiling
- **Corrections:** BPC, WPC, Raw GMA, Lens Correction
- **Orientation:** H-Mirror, V-Flip, DCW, Color Bar

### WiFi
- Max TX power (20.5 dBm)
- Power save disabled (`WIFI_PS_NONE`)
- HT40 bandwidth for maximum throughput

## Performance Notes

- At 480p with quality=12, expect ~20-30 FPS depending on WiFi conditions
- At 720p, expect ~10-20 FPS (larger JPEG frames)
- The ACK-wait design adds ~1 RTT latency per frame but ensures no buffer bloat
- Flash LED on GPIO 4 uses PWM for variable brightness (interferes with SD card if used)

## Port Summary

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 9000 | UDP | ESP32 → Server | Frame data |
| 9001 | UDP | Server → ESP32 | Camera controls |
| 9002 | UDP | ESP32 source | Frame sending source port |
| 8080 | HTTP/WS | Browser ↔ Server | Web UI & WebSocket stream |

## Firewall

Ensure UDP ports 9000-9002 and TCP port 8080 are open on your PC's firewall.
