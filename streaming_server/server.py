"""
<<<<<<< HEAD
ESP32-CAM Multi-Camera Streaming Server
=======================================
Ports:
  UDP  :9000  – receives frames from ESP32-CAMs
  UDP  :9002  – local port (ACK + control sends originate here)
  TCP  :8080  – HTTP + WebSocket web UI

WebSocket binary frame format  server → browser:
  [cam_id:1][jpeg_data:N]

WebSocket JSON messages:
  server → browser : cam_list | cam_joined | cam_removed | cam_status | stats
  browser → server : {action:"control", cam_id:N, param:"...", value:N}
=======
ESP32-CAM Multi-Camera UDP Streaming Server

Supports multiple ESP32-CAM devices simultaneously, each auto-discovered
by source IP. Streams to browser via WebSocket with per-camera multiplexing.
Receives telemetry (RSSI, heap, uptime, FPS) from each camera.

Binary WS frame format:  [cam_id: 1 byte][JPEG data: N bytes]
Text WS messages: JSON with "type" field

Usage:
    python server.py [--host 0.0.0.0] [--web-port 8080] [--udp-port 9000]
                     [--control-port 9001] [--local-port 9002]
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
"""

import asyncio
import argparse
import struct
import time
import json
import logging
import os
import io
<<<<<<< HEAD
import base64
=======
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
import zipfile
import datetime
from collections import defaultdict

from aiohttp import web

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nvr")

# ──────────────────────────────────────────────────────────────────────────────
# Protocol constants  (must match firmware)
# ──────────────────────────────────────────────────────────────────────────────
MAGIC_FRAME  = (0xCA, 0x4D)
MAGIC_ACK    = (0xAC, 0x4B)
MAGIC_CTRL   = (0xC0, 0x4E)
MAGIC_TELE   = (0x54, 0x45)
FRAME_HDR    = 12        # bytes
UDP_RECV     = 9000
UDP_LOCAL    = 9002
UDP_CTRL_ESP = 9001

<<<<<<< HEAD
# Watchdog
CAM_TIMEOUT   = 5.0    # secs without a frame → mark offline
CAM_REMOVE    = 60.0   # secs offline → remove from session
=======
MAGIC_FRAME = bytes([0xCA, 0x4D])
MAGIC_ACK   = bytes([0xAC, 0x4B])
MAGIC_CTRL  = bytes([0xC0, 0x4D])
MAGIC_TELE  = bytes([0x54, 0x45])   # Telemetry (TE)

HEADER_SIZE = 12  # magic:2 + frame_id:4 + chunk_idx:2 + total:2 + payload_len:2

# Camera lifecycle timeouts
CAM_TIMEOUT_SECS = 5.0
CAM_REMOVE_SECS  = 60.0

# Command IDs (must match firmware)
CMD_RESOLUTION     = 0x01
CMD_BRIGHTNESS     = 0x10
CMD_CONTRAST       = 0x11
CMD_SATURATION     = 0x12
CMD_SHARPNESS      = 0x13
CMD_AWB            = 0x20
CMD_AWB_GAIN       = 0x21
CMD_WB_MODE        = 0x22
CMD_AEC            = 0x30
CMD_AEC2           = 0x31
CMD_AE_LEVEL       = 0x32
CMD_AEC_VALUE      = 0x33
CMD_AGC            = 0x40
CMD_AGC_GAIN       = 0x41
CMD_GAINCEILING    = 0x42
CMD_BPC            = 0x50
CMD_WPC            = 0x51
CMD_RAW_GMA        = 0x52
CMD_LENC           = 0x53
CMD_HMIRROR        = 0x60
CMD_VFLIP          = 0x61
CMD_DCW            = 0x62
CMD_COLORBAR       = 0x63
CMD_FLASH          = 0xF0
CMD_QUALITY        = 0x70
CMD_SPECIAL_EFFECT = 0x71
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc

# Command ID map (browser param name → firmware CMD byte)
CMD_MAP = {
    "resolution":   0x01, "quality":      0x02,
    "brightness":   0x10, "contrast":     0x11,
    "saturation":   0x12, "sharpness":    0x13,
    "awb":          0x20, "awb_gain":     0x21, "wb_mode":     0x22,
    "aec":          0x30, "aec2":         0x31, "ae_level":    0x32,
    "aec_value":    0x33, "agc":          0x40, "agc_gain":    0x41,
    "gainceiling":  0x42, "bpc":          0x50, "wpc":         0x51,
    "raw_gma":      0x52, "lenc":         0x53, "hmirror":     0x60,
    "vflip":        0x61, "dcw":          0x62, "special":     0x63,
    "flash":        0xF0,
}


<<<<<<< HEAD
# ──────────────────────────────────────────────────────────────────────────────
# Per-camera session state
# ──────────────────────────────────────────────────────────────────────────────
class CameraSession:
    _id_counter = 0

    def __init__(self, ip: str):
        CameraSession._id_counter += 1
        self.cam_id: int = CameraSession._id_counter - 1
        self.ip: str = ip
        self.name: str = f"CAM-{self.cam_id}"
        self.connected: bool = True
        self.last_seen: float = time.monotonic()

        # Reassembly
        self.frame_chunks: dict[int, dict[int, bytes]] = {}
        self.frame_totals: dict[int, int] = {}
        self.latest_frame_id: int = -1
        self.last_broadcast_id: int = -2   # deliberately different

        # Live frame (bytes)
        self.latest_frame: bytes | None = None

        # Stats
        self.frames_rx:  int = 0
        self.frames_drop: int = 0
        self.bytes_rx:   int = 0
        self._bytes_window: float = 0.0
        self._bps_ts: float = time.monotonic()
        self.kbps: float = 0.0

        # Telemetry
        self.rssi: int = 0
        self.free_heap_kb: int = 0
        self.uptime_secs: int = 0
        self.esp_fps: int = 0
        self.resolution: str = "VGA (480p)"

        # Recording
        self.recording: bool = False
        self.rec_frames: list[bytes] = []
        self.rec_ts: list[float] = []
        self.rec_duration: float = 0.0

    # ── telemetry update ──
    def apply_telem(self, rssi, heap_kb, uptime, fps, res_code):
        self.rssi         = rssi
        self.free_heap_kb = heap_kb
        self.uptime_secs  = uptime
        self.esp_fps      = fps
        self.resolution   = "HD (720p)" if res_code == 1 else "VGA (480p)"

    # ── byte accounting ──
    def on_bytes(self, n: int):
        self.bytes_rx   += n
        self._bytes_window += n
        now = time.monotonic()
        if now - self._bps_ts >= 1.0:
            self.kbps = (self._bytes_window * 8) / (1000 * (now - self._bps_ts))
            self._bytes_window = 0.0
            self._bps_ts = now

    def on_frame(self):
        self.frames_rx += 1
        self.last_seen  = time.monotonic()
        self.connected  = True

    def to_dict(self) -> dict:
        return {
            "cam_id":      self.cam_id,
            "name":        self.name,
            "ip":          self.ip,
            "connected":   self.connected,
            "rssi":        self.rssi,
            "free_heap_kb": self.free_heap_kb,
            "uptime_secs": self.uptime_secs,
            "esp_fps":     self.esp_fps,
            "resolution":  self.resolution,
            "kbps":        round(self.kbps, 1),
            "frames":      self.frames_rx,
            "dropped":     self.frames_drop,
            "recording":   self.recording,
            "rec_frames":  len(self.rec_frames),
            "rec_duration": round(self.rec_duration, 1),
        }


# ──────────────────────────────────────────────────────────────────────────────
# UDP transports
# ──────────────────────────────────────────────────────────────────────────────
class FrameProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "NVRServer"):
=======
# ====================== CAMERA SESSION ====================

class CameraSession:
    """Holds all state for one connected ESP32-CAM."""

    def __init__(self, cam_id: int, ip: str, control_port: int):
        self.cam_id       = cam_id
        self.ip           = ip
        self.control_port = control_port
        self.name         = f"CAM-{cam_id}"

        # Frame reassembly
        self.frame_chunks:       dict[int, dict[int, bytes]] = defaultdict(dict)
        self.frame_total_chunks: dict[int, int] = {}

        # Latest complete frame (set by process_packet, read by broadcaster)
        self.latest_frame:    bytes | None = None
        self.latest_frame_id: int = -1
        self.last_broadcast_id: int = -1

        # Running stats
        self.frames_received = 0
        self.frames_dropped  = 0
        self.bytes_received  = 0
        self.fps   = 0.0
        self.kbps  = 0.0
        self._fps_count  = 0
        self._fps_time   = time.monotonic()
        self._kbps_bytes = 0
        self._kbps_time  = time.monotonic()

        # Telemetry (updated by incoming telemetry packets)
        self.rssi         = 0
        self.free_heap_kb = 0
        self.uptime_secs  = 0
        self.esp_fps      = 0
        self.resolution   = "VGA (480p)"

        # Lifecycle
        self.connected = True
        self.last_seen = time.monotonic()
        self.joined_at = time.monotonic()

        # Recording
        self.recording         = False
        self.record_frames:    list[bytes] = []
        self.record_start_ts:  float = 0.0
        self.record_frame_ts:  list[float] = []  # monotonic timestamps per frame

    def on_bytes(self, n: int):
        self.bytes_received += n
        self._kbps_bytes += n
        now = time.monotonic()
        elapsed = now - self._kbps_time
        if elapsed >= 1.0:
            self.kbps = round((self._kbps_bytes * 8) / elapsed / 1000, 1)
            self._kbps_bytes = 0
            self._kbps_time  = now

    def on_frame(self):
        self.frames_received += 1
        self._fps_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_time
        if elapsed >= 1.0:
            self.fps = round(self._fps_count / elapsed, 1)
            self._fps_count = 0
            self._fps_time  = now
        self.last_seen = now
        self.connected = True

    def to_dict(self) -> dict:
        return {
            "cam_id":       self.cam_id,
            "name":         self.name,
            "ip":           self.ip,
            "connected":    self.connected,
            "fps":          self.fps,
            "kbps":         self.kbps,
            "frames":       self.frames_received,
            "dropped":      self.frames_dropped,
            "bytes":        self.bytes_received,
            "rssi":         self.rssi,
            "free_heap_kb": self.free_heap_kb,
            "uptime_secs":  self.uptime_secs,
            "esp_fps":      self.esp_fps,
            "resolution":   self.resolution,
            "recording":    self.recording,
            "rec_frames":   len(self.record_frames),
            "rec_duration": round(time.monotonic() - self.record_start_ts, 1) if self.recording else 0,
        }


# ===================== MULTI-CAM SERVER ===================

class MultiCamServer:
    def __init__(self, udp_port: int, control_port: int, local_port: int):
        self.udp_port     = udp_port
        self.control_port = control_port  # ESP32 listens here for controls
        self.local_port   = local_port    # server sends from here

        self.cameras_by_ip: dict[str, CameraSession] = {}
        self.cameras_by_id: dict[int, CameraSession] = {}
        self._next_cam_id = 0

        self.frame_transport:   asyncio.DatagramTransport | None = None
        self.control_transport: asyncio.DatagramTransport | None = None

        self.ws_clients: set[web.WebSocketResponse] = set()
        self.any_new_frame = asyncio.Event()

    # ---- Session management ----

    def get_or_create(self, ip: str) -> tuple[CameraSession, bool]:
        """Returns (session, changed) where changed=True means new or reconnected."""
        if ip in self.cameras_by_ip:
            cam = self.cameras_by_ip[ip]
            was_disconnected = not cam.connected
            cam.last_seen = time.monotonic()
            cam.connected = True
            return cam, was_disconnected
        cam = CameraSession(self._next_cam_id, ip, self.control_port)
        self._next_cam_id += 1
        self.cameras_by_ip[ip] = cam
        self.cameras_by_id[cam.cam_id] = cam
        log.info(f"New camera: {cam.name} @ {ip}")
        return cam, True

    # ---- Packet dispatch ----

    def process_packet(self, data: bytes, addr: tuple[str, int]):
        if len(data) < 2:
            return
        magic = data[0:2]
        if magic == MAGIC_FRAME:
            self._process_frame(data, addr)
        elif magic == MAGIC_TELE:
            self._process_telemetry(data, addr)

    def _process_frame(self, data: bytes, addr: tuple[str, int]):
        if len(data) < HEADER_SIZE:
            return

        frame_id     = struct.unpack(">I", data[2:6])[0]
        chunk_idx    = struct.unpack(">H", data[6:8])[0]
        total_chunks = struct.unpack(">H", data[8:10])[0]
        payload_len  = struct.unpack(">H", data[10:12])[0]

        if len(data) < HEADER_SIZE + payload_len:
            return

        payload = data[HEADER_SIZE: HEADER_SIZE + payload_len]
        ip = addr[0]

        cam, changed = self.get_or_create(ip)
        cam.on_bytes(len(data))

        if changed:
            asyncio.get_event_loop().call_soon(self._notify, cam, "cam_joined")

        # Discard frames older than latest
        if frame_id < cam.latest_frame_id:
            return

        # Purge stale partial reassembly
        stale = [fid for fid in cam.frame_chunks if fid < frame_id]
        for fid in stale:
            cam.frame_chunks.pop(fid, None)
            cam.frame_total_chunks.pop(fid, None)
            cam.frames_dropped += 1

        cam.frame_chunks[frame_id][chunk_idx] = payload
        cam.frame_total_chunks[frame_id] = total_chunks

        # Check frame complete
        if len(cam.frame_chunks[frame_id]) == total_chunks:
            chunks = cam.frame_chunks[frame_id]
            frame_data = b"".join(chunks[i] for i in range(total_chunks) if i in chunks)

            if len(frame_data) >= 2 and frame_data[0] == 0xFF and frame_data[1] == 0xD8:
                cam.latest_frame    = frame_data
                cam.latest_frame_id = frame_id
                cam.on_frame()
                self.any_new_frame.set()
                if cam.recording:
                    cam.record_frames.append(frame_data)
                    cam.record_frame_ts.append(time.monotonic())

            cam.frame_chunks.pop(frame_id, None)
            cam.frame_total_chunks.pop(frame_id, None)
            self._send_ack(cam, frame_id)

    def _process_telemetry(self, data: bytes, addr: tuple[str, int]):
        # [TELE:2][name_len:1][name:N][rssi:int8][heap_kb:uint16 BE][uptime:uint32 BE][fps:uint8][res:uint8]
        pos = 2
        if len(data) < pos + 1:
            return
        name_len = data[pos]; pos += 1
        if len(data) < pos + name_len + 8:
            return
        name    = data[pos: pos + name_len].decode("ascii", errors="replace").strip("\x00")
        pos    += name_len
        rssi    = struct.unpack("b", bytes([data[pos]]))[0]; pos += 1
        heap_kb = struct.unpack(">H", data[pos: pos+2])[0]; pos += 2
        uptime  = struct.unpack(">I", data[pos: pos+4])[0]; pos += 4
        fps     = data[pos]; pos += 1
        res     = data[pos]; pos += 1

        cam, changed = self.get_or_create(addr[0])
        if name:
            cam.name = name
        cam.rssi         = rssi
        cam.free_heap_kb = heap_kb
        cam.uptime_secs  = uptime
        cam.esp_fps      = fps
        cam.resolution   = "HD (720p)" if res == 1 else "VGA (480p)"
        if changed:
            asyncio.get_event_loop().call_soon(self._notify, cam, "cam_joined")

    # ---- UDP helpers ----

    def _send_ack(self, cam: CameraSession, frame_id: int):
        if not self.frame_transport:
            return
        ack = MAGIC_ACK + struct.pack(">I", frame_id)
        self.frame_transport.sendto(ack, (cam.ip, self.local_port))

    def send_control(self, cam_id: int, cmd_id: int, payload: bytes):
        cam = self.cameras_by_id.get(cam_id)
        if not cam:
            log.warning(f"Control: cam_id {cam_id} not found")
            return
        if not self.control_transport:
            log.warning("Control: no transport")
            return
        packet = MAGIC_CTRL + bytes([cmd_id]) + payload
        self.control_transport.sendto(packet, (cam.ip, cam.control_port))
        log.info(f"→ {cam.name} cmd=0x{cmd_id:02x} payload={payload.hex()}")

    # ---- WS notification (fire-and-forget) ----

    def _notify(self, cam: CameraSession, msg_type: str):
        msg = json.dumps({
            "type":   msg_type,
            "cam_id": cam.cam_id,
            "data":   cam.to_dict(),
        })
        dead = set()
        for ws in self.ws_clients:
            if ws.closed:
                dead.add(ws)
            else:
                asyncio.ensure_future(ws.send_str(msg))
        self.ws_clients -= dead


# ==================== UDP PROTOCOLS =======================

class FrameReceiverProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: MultiCamServer):
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
<<<<<<< HEAD
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        if len(data) < 2:
            return
        if data[0] == MAGIC_FRAME[0] and data[1] == MAGIC_FRAME[1]:
            self.server.on_frame_packet(data, addr)
        elif data[0] == MAGIC_TELE[0] and data[1] == MAGIC_TELE[1]:
            self.server.on_telemetry(data, addr)
=======
        self.server.frame_transport = transport
        log.info(f"UDP frame receiver on :{self.server.udp_port}")

    def datagram_received(self, data: bytes, addr):
        self.server.process_packet(data, addr)
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc

    def error_received(self, exc):
        log.error(f"UDP frame error: {exc}")

<<<<<<< HEAD
class ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        pass  # control port is outbound-only
=======

class ControlTransportProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: MultiCamServer):
        self.server = server

    def connection_made(self, transport):
        self.server.control_transport = transport
        log.info(f"UDP control transport on :{self.server.local_port}")

    def datagram_received(self, data, addr):
        pass

    def error_received(self, exc):
        log.error(f"UDP control error: {exc}")
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc


# ──────────────────────────────────────────────────────────────────────────────
# NVR Server
# ──────────────────────────────────────────────────────────────────────────────
class NVRServer:
    def __init__(self):
        self.cameras_by_ip: dict[str, CameraSession] = {}
        self.cameras_by_id: dict[int, CameraSession] = {}
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.any_new_frame: asyncio.Event = asyncio.Event()

        # MJPEG/SSE subscribers: cam_id → set of asyncio.Queue
        self.stream_subs: dict[int, set[asyncio.Queue]] = {}

        self._frame_proto: FrameProtocol | None = None
        self._ctrl_transport: asyncio.DatagramTransport | None = None

    # ── camera lookup / creation ─────────────────────────────────────────────
    def get_or_create(self, ip: str) -> tuple[CameraSession, bool]:
        if ip in self.cameras_by_ip:
            return self.cameras_by_ip[ip], False
        cam = CameraSession(ip)
        self.cameras_by_ip[ip] = cam
        self.cameras_by_id[cam.cam_id] = cam
        log.info(f"New camera: {cam.name} @ {ip}")
        # Schedule notification (can't await here)
        asyncio.get_event_loop().call_soon(self._notify_joined, cam)
        return cam, True

    # ── frame reassembly ─────────────────────────────────────────────────────
    def on_frame_packet(self, data: bytes, addr: tuple):
        if len(data) < FRAME_HDR + 1:
            return

        frame_id    = struct.unpack_from(">I", data, 2)[0]
        chunk_idx   = struct.unpack_from(">H", data, 6)[0]
        total_chunks= struct.unpack_from(">H", data, 8)[0]
        payload_len = struct.unpack_from(">H", data, 10)[0]

        if len(data) < FRAME_HDR + payload_len:
            return

        ip = addr[0]
        cam, _ = self.get_or_create(ip)
        cam.on_bytes(len(data))

        # Drop frames older than what we already have
        if frame_id < cam.latest_frame_id:
            return

        # Purge stale partial frames
        for fid in [k for k in cam.frame_chunks if k < frame_id]:
            cam.frame_chunks.pop(fid)
            cam.frame_totals.pop(fid, None)
            cam.frames_drop += 1

        cam.frame_chunks.setdefault(frame_id, {})[chunk_idx] = \
            data[FRAME_HDR: FRAME_HDR + payload_len]
        cam.frame_totals[frame_id] = total_chunks

        # Complete frame?
        if len(cam.frame_chunks[frame_id]) == total_chunks:
            chunks = cam.frame_chunks.pop(frame_id)
            cam.frame_totals.pop(frame_id, None)
            frame_bytes = b"".join(chunks[i] for i in range(total_chunks) if i in chunks)

            # Validate JPEG SOI marker
            if len(frame_bytes) >= 2 and frame_bytes[0] == 0xFF and frame_bytes[1] == 0xD8:
                cam.latest_frame    = frame_bytes
                cam.latest_frame_id = frame_id
                cam.on_frame()
                self.any_new_frame.set()

                if cam.recording:
                    cam.rec_frames.append(frame_bytes)
                    cam.rec_ts.append(time.monotonic())
                    if len(cam.rec_ts) >= 2:
                        cam.rec_duration = cam.rec_ts[-1] - cam.rec_ts[0]

                # Push to MJPEG/SSE queues (non-blocking, drop if full)
                for q in list(self.stream_subs.get(cam.cam_id, set())):
                    if not q.full():
                        q.put_nowait(frame_bytes)

            # Send ACK regardless of JPEG validity
            self._send_ack(cam, frame_id)

    def _send_ack(self, cam: CameraSession, frame_id: int):
        if self._frame_proto and self._frame_proto.transport:
            ack = bytes([MAGIC_ACK[0], MAGIC_ACK[1],
                         (frame_id >> 24) & 0xFF, (frame_id >> 16) & 0xFF,
                         (frame_id >>  8) & 0xFF,  frame_id        & 0xFF])
            try:
                self._frame_proto.transport.sendto(ack, (cam.ip, UDP_CTRL_ESP))
            except Exception:
                pass

    # ── telemetry ────────────────────────────────────────────────────────────
    def on_telemetry(self, data: bytes, addr: tuple):
        pos = 2
        if len(data) < pos + 1:
            return
        nlen = data[pos]; pos += 1
        if len(data) < pos + nlen + 8:
            return
        name    = data[pos:pos + nlen].decode("utf-8", errors="replace"); pos += nlen
        rssi    = struct.unpack_from("b", data, pos)[0];   pos += 1
        heap_kb = struct.unpack_from(">H", data, pos)[0];  pos += 2
        uptime  = struct.unpack_from(">I", data, pos)[0];  pos += 4
        fps     = data[pos]; pos += 1
        res     = data[pos] if len(data) > pos else 0

        ip = addr[0]
        if ip in self.cameras_by_ip:
            cam = self.cameras_by_ip[ip]
            cam.name = name
            cam.apply_telem(rssi, heap_kb, uptime, fps, res)

    # ── control send ─────────────────────────────────────────────────────────
    def send_control(self, cam_id: int, cmd: int, payload: bytes):
        cam = self.cameras_by_id.get(cam_id)
        if not cam or not self._ctrl_transport:
            return
        msg = bytes([MAGIC_CTRL[0], MAGIC_CTRL[1], cmd]) + payload
        try:
            self._ctrl_transport.sendto(msg, (cam.ip, UDP_CTRL_ESP))
        except Exception as e:
            log.warning(f"Control send error: {e}")

    # ── WS notification helpers ───────────────────────────────────────────────
    def _notify_joined(self, cam: CameraSession):
        self._broadcast_json({"type": "cam_joined", "cam_id": cam.cam_id,
                               "data": cam.to_dict()})

    def notify_status(self, cam: CameraSession):
        self._broadcast_json({"type": "cam_status", "cam_id": cam.cam_id,
                               "data": cam.to_dict()})

    def _broadcast_json(self, obj: dict):
        if not self.ws_clients:
            return
        msg = json.dumps(obj)
        for ws in list(self.ws_clients):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(msg))


# ──────────────────────────────────────────────────────────────────────────────
# AVI builder (pure Python, no ffmpeg)
# ──────────────────────────────────────────────────────────────────────────────
def _le16(v): return struct.pack("<H", v)
def _le32(v): return struct.pack("<I", v)

def _riff(fourcc: str, data: bytes) -> bytes:
    return fourcc.encode() + _le32(len(data)) + data

def _list_blk(listtype: str, data: bytes) -> bytes:
    return b"LIST" + _le32(4 + len(data)) + listtype.encode() + data

def _jpeg_wh(data: bytes) -> tuple[int, int]:
    i = 2
    while i + 4 < len(data):
        if data[i] != 0xFF:
            break
        mk = data[i + 1]
        if mk in (0xC0, 0xC2):
            return struct.unpack_from(">HH", data, i + 5)
        if mk in (0xD8, 0xD9):
            i += 2
        else:
            i += 2 + struct.unpack_from(">H", data, i + 2)[0]
    return 640, 480

def build_avi(frames: list[bytes], timestamps: list[float]) -> bytes:
    if not frames:
        return b""

    fps = 25
    if len(timestamps) >= 2:
        elapsed = timestamps[-1] - timestamps[0]
        fps = max(1, round(len(frames) / max(elapsed, 0.1)))

    w, h = _jpeg_wh(frames[0])

    # Build movi chunk  ── idx1 index in parallel
    movi_data = b""
    idx1_data = b""
    offset = 4  # movi FourCC already counted
    for f in frames:
        tag = b"00dc"
        movi_data += tag + _le32(len(f)) + f
        if len(f) & 1:
            movi_data += b"\x00"  # RIFF padding
        idx1_data += tag + _le32(0x10) + _le32(offset) + _le32(len(f))
        offset += 8 + len(f) + (len(f) & 1)

    movi = _list_blk("movi", movi_data)

    # Stream header (strh)
    us_per_frame = 1_000_000 // fps
    strh = (b"vids" + b"MJPG" +
            _le32(0) + _le16(0) + _le16(0) +
            _le32(0) + _le32(1) + _le32(fps) +
            _le32(0) + _le32(len(frames)) +
            _le32(0) + _le32(0) +
            _le16(0) + _le16(0) +
            _le16(w) + _le16(h))

    # Stream format (strf = BITMAPINFOHEADER)
    strf = (_le32(40) + _le32(w) + _le32(h) +
            _le16(1) + _le16(24) + b"MJPG" +
            _le32(w * h * 3) + _le32(0) + _le32(0) +
            _le32(0) + _le32(0))

    strl = _list_blk("strl", _riff("strh", strh) + _riff("strf", strf))

    # AVI main header (avih)
    avih = (_le32(us_per_frame) + _le32(w * h * 3 * fps) +
            _le32(0) + _le32(0x0110) +
            _le32(len(frames)) + _le32(0) + _le32(1) +
            _le32(0) + _le32(w) + _le32(h) +
            _le32(0) * 4)

    hdrl = _list_blk("hdrl", _riff("avih", avih) + strl)
    avi_data = hdrl + movi + _riff("idx1", idx1_data)
    avi = _list_blk("AVI ", avi_data)
    return b"RIFF" + _le32(4 + len(avi_data)) + b"AVI " + avi_data


# ──────────────────────────────────────────────────────────────────────────────
# HTTP / WebSocket handlers
# ──────────────────────────────────────────────────────────────────────────────
async def index_handler(request: web.Request) -> web.Response:
<<<<<<< HEAD
    path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(path, "r", encoding="utf-8") as f:
=======
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
<<<<<<< HEAD
    server: NVRServer = request.app["server"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    log.info(f"WS connecting from {request.remote}")

    # ── handshake: send cam_list + replay latest frames BEFORE joining pool ──
    try:
        await ws.send_str(json.dumps({
            "type":    "cam_list",
            "cameras": {str(c.cam_id): c.to_dict()
                        for c in server.cameras_by_id.values()},
        }))
        for cam in server.cameras_by_id.values():
            if cam.latest_frame is not None:
                await ws.send_bytes(bytes([cam.cam_id & 0xFF]) + cam.latest_frame)
    except Exception as e:
        log.warning(f"WS handshake failed: {e}")
        return ws

    server.ws_clients.add(ws)
    log.info(f"WS ready ({len(server.ws_clients)} clients)")
=======
    server: MultiCamServer = request.app["server"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    server.ws_clients.add(ws)
    log.info(f"WS connected from {request.remote} ({len(server.ws_clients)} total)")

    # Send current camera list on connect
    await ws.send_str(json.dumps({
        "type":    "cam_list",
        "cameras": {str(cid): cam.to_dict() for cid, cam in server.cameras_by_id.items()},
    }))
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
<<<<<<< HEAD
                    _handle_control(server, json.loads(msg.data))
                except Exception as e:
                    log.debug(f"Bad WS msg: {e}")
=======
                    _handle_cmd(server, json.loads(msg.data))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                    log.debug(f"Bad WS cmd: {e}")
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
            elif msg.type == web.WSMsgType.ERROR:
                log.error(f"WS error: {ws.exception()}")
    finally:
        server.ws_clients.discard(ws)
<<<<<<< HEAD
        log.info(f"WS {request.remote} left ({len(server.ws_clients)} clients)")
    return ws


def _handle_control(server: NVRServer, cmd: dict):
    if cmd.get("action") != "control":
        return
    cam_id = int(cmd["cam_id"])
    param  = str(cmd["param"])
    value  = cmd["value"]
    cid    = CMD_MAP.get(param)
    if cid is None:
        return
    # Encode payload
=======
        log.info(f"WS {request.remote} disconnected ({len(server.ws_clients)} total)")
    return ws


def _handle_cmd(server: MultiCamServer, cmd: dict):
    if cmd.get("action") != "control":
        return
    cam_id = int(cmd["cam_id"])
    param  = cmd["param"]
    value  = cmd["value"]
    cmd_id = CMD_MAP.get(param)
    if cmd_id is None:
        return
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
    if param == "aec_value":
        payload = struct.pack(">H", max(0, min(1200, int(value))))
    elif param in ("brightness", "contrast", "saturation", "sharpness", "ae_level"):
        payload = struct.pack("b", max(-2, min(2, int(value))))
    else:
        payload = bytes([max(0, min(255, int(value)))])
<<<<<<< HEAD
    server.send_control(cam_id, cid, payload)
=======
    server.send_control(cam_id, cmd_id, payload)
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc


# ── Stats API ─────────────────────────────────────────────────────────────────
async def stats_handler(request: web.Request) -> web.Response:
<<<<<<< HEAD
    server: NVRServer = request.app["server"]
    data = {str(c.cam_id): c.to_dict() for c in server.cameras_by_id.values()}
    return web.json_response({"cameras": data})


# ── Snapshot API ──────────────────────────────────────────────────────────────
async def snapshot_handler(request: web.Request) -> web.Response:
    server: NVRServer = request.app["server"]
    cam_id = int(request.rel_url.query.get("cam_id", -1))
    cam = server.cameras_by_id.get(cam_id)
    if not cam or cam.latest_frame is None:
        return web.Response(status=404, text="No frame")
    ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return web.Response(
        body=cam.latest_frame,
        content_type="image/jpeg",
        headers={"Content-Disposition":
                 f'attachment; filename="snap_{cam.name}_{ts}.jpg"'},
    )


# ── Recording API ─────────────────────────────────────────────────────────────
async def rec_start_handler(request: web.Request) -> web.Response:
    server: NVRServer = request.app["server"]
    cam_id = int(request.rel_url.query.get("cam_id", -1))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.Response(status=404, text="Camera not found")
    cam.recording  = True
    cam.rec_frames = []
    cam.rec_ts     = []
    cam.rec_duration = 0.0
    return web.json_response({"recording": True})


async def rec_stop_handler(request: web.Request) -> web.Response:
    server: NVRServer = request.app["server"]
    cam_id = int(request.rel_url.query.get("cam_id", -1))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.Response(status=404, text="Camera not found")
    cam.recording = False
    frames, timestamps = cam.rec_frames[:], cam.rec_ts[:]
    cam.rec_frames = []; cam.rec_ts = []; cam.rec_duration = 0.0
    if not frames:
        return web.Response(status=204, text="No frames recorded")
    avi_data = await asyncio.get_event_loop().run_in_executor(
        None, build_avi, frames, timestamps)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return web.Response(
        body=avi_data,
        content_type="video/avi",
        headers={"Content-Disposition":
                 f'attachment; filename="{cam.name}_{ts}.avi"'},
    )


# ── MJPEG stream endpoint ─────────────────────────────────────────────────────
async def mjpeg_handler(request: web.Request) -> web.StreamResponse:
    server: NVRServer = request.app["server"]
    cam_id = int(request.match_info.get("cam_id", -1))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.Response(status=404, text="Camera not found")

    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace;boundary=esp32nvr",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    server.stream_subs.setdefault(cam_id, set()).add(q)
    try:
        # send latest on subscribe
        if cam.latest_frame:
            await resp.write(
                b"--esp32nvr\r\nContent-Type: image/jpeg\r\n\r\n"
                + cam.latest_frame + b"\r\n")
        while True:
            try:
                frame = await asyncio.wait_for(q.get(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    await resp.write(b"--esp32nvr\r\n\r\n")
                except Exception:
                    break
                continue
            try:
                await resp.write(
                    b"--esp32nvr\r\nContent-Type: image/jpeg\r\n\r\n"
                    + frame + b"\r\n")
            except Exception:
                break
    finally:
        server.stream_subs.get(cam_id, set()).discard(q)
    return resp
=======
    server: MultiCamServer = request.app["server"]
    return web.json_response({
        "cameras":       {str(cid): cam.to_dict() for cid, cam in server.cameras_by_id.items()},
        "ws_clients":    len(server.ws_clients),
        "total_cameras": sum(1 for c in server.cameras_by_id.values() if c.connected),
    })


async def record_start_handler(request: web.Request) -> web.Response:
    """POST /api/record/start?cam_id=0  — begin recording for camera."""
    server: MultiCamServer = request.app["server"]
    cam_id = int(request.rel_url.query.get("cam_id", 0))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.json_response({"error": "camera not found"}, status=404)
    if cam.recording:
        return web.json_response({"error": "already recording"})
    cam.record_frames   = []
    cam.record_frame_ts = []
    cam.record_start_ts = time.monotonic()
    cam.recording       = True
    log.info(f"{cam.name}: recording started")
    return web.json_response({"ok": True, "cam_id": cam_id})


async def record_stop_handler(request: web.Request) -> web.Response:
    """POST /api/record/stop?cam_id=0  — stop and return MJPEG as .avi inside a zip."""
    server: MultiCamServer = request.app["server"]
    cam_id = int(request.rel_url.query.get("cam_id", 0))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.json_response({"error": "camera not found"}, status=404)
    if not cam.recording:
        return web.json_response({"error": "not recording"})

    cam.recording = False
    frames    = cam.record_frames[:]
    frame_ts  = cam.record_frame_ts[:]
    cam.record_frames   = []
    cam.record_frame_ts = []
    n = len(frames)
    log.info(f"{cam.name}: recording stopped — {n} frames")

    if n == 0:
        return web.json_response({"error": "no frames recorded"})

    # Build MJPEG AVI in memory
    avi_bytes = _build_avi(frames, frame_ts)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{cam.name}_{ts}.avi"

    return web.Response(
        body=avi_bytes,
        content_type="video/x-msvideo",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def snapshot_handler(request: web.Request) -> web.Response:
    """GET /api/snapshot?cam_id=0  — download latest JPEG frame."""
    server: MultiCamServer = request.app["server"]
    cam_id = int(request.rel_url.query.get("cam_id", 0))
    cam = server.cameras_by_id.get(cam_id)
    if not cam or cam.latest_frame is None:
        return web.json_response({"error": "no frame available"}, status=404)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{cam.name}_{ts}.jpg"
    return web.Response(
        body=cam.latest_frame,
        content_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── MJPEG AVI builder ────────────────────────────────────────────────────────
# Produces a minimal OpenDML / MJPEG AVI-1.0 file playable by VLC, Windows
# Media Player, ffmpeg etc. No external dependency needed.

def _le16(v: int) -> bytes: return struct.pack("<H", v & 0xFFFF)
def _le32(v: int) -> bytes: return struct.pack("<I", v & 0xFFFFFFFF)
def _riff(fourcc: str, data: bytes) -> bytes:
    return fourcc.encode() + _le32(len(data)) + data

def _list_chunk(fourcc: str, chunks: list[bytes]) -> bytes:
    inner = fourcc.encode() + b"".join(chunks)
    return b"LIST" + _le32(len(inner)) + inner

def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Extract width/height from a JPEG by scanning SOF markers."""
    i = 0
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC2):  # SOF0, SOF2
            if i + 9 <= len(data):
                h = (data[i+5] << 8) | data[i+6]
                w = (data[i+7] << 8) | data[i+8]
                return w, h
        if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
            i += 2
            continue
        if i + 4 > len(data):
            break
        seg_len = (data[i+2] << 8) | data[i+3]
        i += 2 + seg_len
    return 640, 480  # fallback

def _build_avi(frames: list[bytes], timestamps: list[float]) -> bytes:
    n      = len(frames)
    # Compute average FPS from timestamps
    if n >= 2:
        total_t = timestamps[-1] - timestamps[0]
        fps_num = max(1, round((n - 1) / max(0.001, total_t)))
    else:
        fps_num = 25
    fps_den = 1

    # Assume all frames are same resolution (read first JPEG SOF0 if possible)
    width, height = _jpeg_dimensions(frames[0])

    # Build movi chunk with index
    movi_chunks: list[bytes] = []
    idx_entries  = bytearray()
    movi_offset  = 4  # after 'movi'
    for jpeg in frames:
        tag   = b"00dc"  # stream 0, compressed video
        padded = jpeg if len(jpeg) % 2 == 0 else jpeg + b"\x00"
        chunk = tag + _le32(len(jpeg)) + padded
        idx_entries += tag
        idx_entries += _le32(0x10)           # AVIIF_KEYFRAME
        idx_entries += _le32(movi_offset)
        idx_entries += _le32(len(jpeg))
        movi_offset += 8 + len(padded)
        movi_chunks.append(chunk)

    movi_data  = b"movi" + b"".join(movi_chunks)
    idx1_data  = b"idx1" + _le32(len(idx_entries)) + bytes(idx_entries)
    max_bytes  = max(len(f) for f in frames)
    avg_bytes  = sum(len(f) for f in frames) // n

    # avih — main AVI header
    avih = (
        _le32(1_000_000 // fps_num) +  # microseconds per frame
        _le32(avg_bytes * fps_num) +    # max bytes per second
        _le32(0) +                      # padding
        _le32(0x910) +                  # flags: AVIF_HASINDEX | AVIF_ISINTERLEAVED | AVIF_TRUSTCKTYPE
        _le32(n) +                      # total frames
        _le32(0) +                      # initial frames
        _le32(1) +                      # streams
        _le32(max_bytes) +              # suggested buffer size
        _le32(width) +
        _le32(height) +
        _le32(0) * 4                    # reserved
    )

    # strh — stream header
    strh = (
        b"vids" +                        # fccType
        b"MJPG" +                        # fccHandler
        _le32(0) +                       # flags
        _le16(0) +                       # priority
        _le16(0) +                       # language
        _le32(0) +                       # initial frames
        _le32(fps_den) +                 # scale
        _le32(fps_num) +                 # rate
        _le32(0) +                       # start
        _le32(n) +                       # length
        _le32(max_bytes) +               # suggested buffer
        _le32(10000) +                   # quality (-1 = default)
        _le32(0) +                       # sample size
        _le16(0) + _le16(0) + _le16(width) + _le16(height)  # rcFrame
    )

    # strf — BITMAPINFOHEADER
    strf = (
        _le32(40) +          # biSize
        _le32(width) +
        _le32(height) +
        _le16(1) +           # planes
        _le16(24) +          # bitCount
        b"MJPG" +
        _le32(width * height * 3) +
        _le32(0) + _le32(0) + _le32(0) + _le32(0)
    )

    strl = _list_chunk("strl", [
        _riff("strh", strh),
        _riff("strf", strf),
    ])

    hdrl = _list_chunk("hdrl", [
        _riff("avih", avih),
        strl,
    ])

    riff_data = hdrl + movi_data + idx1_data
    return b"RIFF" + _le32(len(riff_data) + 4) + b"AVI " + riff_data


# ================== BACKGROUND TASKS =====================

async def frame_broadcaster(app: web.Application):
    server: MultiCamServer = app["server"]
    while True:
        try:
            await asyncio.wait_for(server.any_new_frame.wait(), timeout=0.05)
            server.any_new_frame.clear()
        except asyncio.TimeoutError:
            pass

        for cam_id, cam in list(server.cameras_by_id.items()):
            if cam.latest_frame is None:
                continue
            if cam.latest_frame_id == cam.last_broadcast_id:
                continue
            cam.last_broadcast_id = cam.latest_frame_id
            msg = bytes([cam_id & 0xFF]) + cam.latest_frame
            dead = set()
            for ws in server.ws_clients:
                if ws.closed:
                    dead.add(ws); continue
                try:
                    await ws.send_bytes(msg)
                except Exception:
                    dead.add(ws)
            server.ws_clients -= dead


async def stats_broadcaster(app: web.Application):
    server: MultiCamServer = app["server"]
    while True:
        await asyncio.sleep(1.0)
        if not server.ws_clients:
            continue
        msg = json.dumps({
            "type":    "stats",
            "cameras": {str(cid): cam.to_dict() for cid, cam in server.cameras_by_id.items()},
        })
        dead = set()
        for ws in server.ws_clients:
            if ws.closed:
                dead.add(ws); continue
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        server.ws_clients -= dead


async def camera_watchdog(app: web.Application):
    server: MultiCamServer = app["server"]
    while True:
        await asyncio.sleep(3.0)
        now = time.monotonic()
        for ip, cam in list(server.cameras_by_ip.items()):
            if cam.connected and (now - cam.last_seen) > CAM_TIMEOUT_SECS:
                cam.connected = False
                log.info(f"{cam.name} ({ip}) timed out")
                server._notify(cam, "cam_status")
            if (not cam.connected) and (now - cam.last_seen) > CAM_REMOVE_SECS:
                server.cameras_by_ip.pop(ip, None)
                server.cameras_by_id.pop(cam.cam_id, None)
                log.info(f"{cam.name} ({ip}) removed")
                server._notify(cam, "cam_removed")


async def on_startup(app: web.Application):
    app["t_frames"]   = asyncio.create_task(frame_broadcaster(app))
    app["t_stats"]    = asyncio.create_task(stats_broadcaster(app))
    app["t_watchdog"] = asyncio.create_task(camera_watchdog(app))


async def on_cleanup(app: web.Application):
    for key in ("t_frames", "t_stats", "t_watchdog"):
        t = app.get(key)
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc


# ── SSE stream endpoint ───────────────────────────────────────────────────────
async def sse_handler(request: web.Request) -> web.StreamResponse:
    server: NVRServer = request.app["server"]
    cam_id = int(request.match_info.get("cam_id", -1))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.Response(status=404, text="Camera not found")

<<<<<<< HEAD
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    server.stream_subs.setdefault(cam_id, set()).add(q)
    ka_interval = 15
    ka_ts = time.monotonic()
    try:
        if cam.latest_frame:
            b64 = base64.b64encode(cam.latest_frame).decode()
            await resp.write(f"event: frame\ndata: {b64}\n\n".encode())
        while True:
            now = time.monotonic()
            try:
                frame = await asyncio.wait_for(q.get(), timeout=ka_interval - (now - ka_ts))
                b64 = base64.b64encode(frame).decode()
                await resp.write(f"event: frame\ndata: {b64}\n\n".encode())
                ka_ts = time.monotonic()
            except asyncio.TimeoutError:
                try:
                    await resp.write(b": keepalive\n\n")
                    ka_ts = time.monotonic()
                except Exception:
                    break
            except Exception:
                break
    finally:
        server.stream_subs.get(cam_id, set()).discard(q)
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# Background tasks
# ──────────────────────────────────────────────────────────────────────────────
async def frame_broadcaster(app: web.Application):
    """Pushes new frames to all connected WebSocket clients."""
    server: NVRServer = app["server"]
    while True:
        try:
            try:
                await asyncio.wait_for(server.any_new_frame.wait(), timeout=0.05)
                server.any_new_frame.clear()
            except asyncio.TimeoutError:
                pass

            for cam in list(server.cameras_by_id.values()):
                if cam.latest_frame is None:
                    continue
                if cam.latest_frame_id == cam.last_broadcast_id:
                    continue
                cam.last_broadcast_id = cam.latest_frame_id
                msg = bytes([cam.cam_id & 0xFF]) + cam.latest_frame
                dead = set()
                for ws in list(server.ws_clients):     # snapshot → no mutation errors
                    if ws.closed:
                        dead.add(ws)
                        continue
                    try:
                        await ws.send_bytes(msg)
                    except Exception:
                        dead.add(ws)
                server.ws_clients -= dead
        except Exception as e:
            log.error(f"frame_broadcaster: {e}")     # never let this task die


async def stats_broadcaster(app: web.Application):
    """Pushes stats JSON to all WS clients every second."""
    server: NVRServer = app["server"]
    while True:
        await asyncio.sleep(1.0)
        if not server.ws_clients:
            continue
        msg = json.dumps({
            "type":    "stats",
            "cameras": {str(c.cam_id): c.to_dict()
                        for c in server.cameras_by_id.values()},
        })
        dead = set()
        for ws in list(server.ws_clients):
            if ws.closed:
                dead.add(ws)
                continue
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        server.ws_clients -= dead


async def camera_watchdog(app: web.Application):
    """Marks cameras offline after timeout, removes after extended silence."""
    server: NVRServer = app["server"]
    while True:
        await asyncio.sleep(3.0)
        now = time.monotonic()
        for ip, cam in list(server.cameras_by_ip.items()):
            if cam.connected and now - cam.last_seen > CAM_TIMEOUT:
                cam.connected = False
                log.info(f"{cam.name} ({ip}) timed out")
                server.notify_status(cam)
            if not cam.connected and now - cam.last_seen > CAM_REMOVE:
                log.info(f"Removing {cam.name} ({ip})")
                server.cameras_by_ip.pop(ip, None)
                server.cameras_by_id.pop(cam.cam_id, None)
                server._broadcast_json({"type": "cam_removed", "cam_id": cam.cam_id})


# ──────────────────────────────────────────────────────────────────────────────
# Application startup / shutdown
# ──────────────────────────────────────────────────────────────────────────────
async def start_background(app: web.Application):
    app["tasks"] = [
        asyncio.ensure_future(frame_broadcaster(app)),
        asyncio.ensure_future(stats_broadcaster(app)),
        asyncio.ensure_future(camera_watchdog(app)),
    ]


async def stop_background(app: web.Application):
    for t in app.get("tasks", []):
        t.cancel()
    await asyncio.gather(*app.get("tasks", []), return_exceptions=True)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
async def main(host: str = "0.0.0.0", port: int = 8080):
    loop = asyncio.get_event_loop()
    server = NVRServer()
=======
async def main():
    parser = argparse.ArgumentParser(description="ESP32-CAM Multi-Cam UDP Streaming Server")
    parser.add_argument("--host",         default="0.0.0.0")
    parser.add_argument("--web-port",     type=int, default=8080)
    parser.add_argument("--udp-port",     type=int, default=9000,  help="Receive frames")
    parser.add_argument("--control-port", type=int, default=9001,  help="ESP32 control listen port")
    parser.add_argument("--local-port",   type=int, default=9002,  help="Server control send port / ESP32 source port")
    args = parser.parse_args()

    server = MultiCamServer(args.udp_port, args.control_port, args.local_port)
    loop   = asyncio.get_event_loop()
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc

    # UDP frame receiver
    fp = FrameProtocol(server)
    server._frame_proto = fp
    frame_transport, _ = await loop.create_datagram_endpoint(
<<<<<<< HEAD
        lambda: fp, local_addr=("0.0.0.0", UDP_RECV))
    log.info(f"UDP frame receiver on :{UDP_RECV}")

    # UDP control sender (outbound only)
    cp = ControlProtocol()
    ctrl_transport, _ = await loop.create_datagram_endpoint(
        lambda: cp, local_addr=("0.0.0.0", UDP_LOCAL))
    server._ctrl_transport = ctrl_transport
    log.info(f"UDP control transport on :{UDP_LOCAL}")

    # HTTP app
    app = web.Application()
    app["server"] = server
    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)

    app.router.add_get("/",               index_handler)
    app.router.add_get("/ws",             websocket_handler)
    app.router.add_get("/api/stats",      stats_handler)
    app.router.add_get("/api/snapshot",   snapshot_handler)
    app.router.add_post("/api/rec/start", rec_start_handler)
    app.router.add_post("/api/rec/stop",  rec_stop_handler)
    app.router.add_get("/stream/{cam_id}", mjpeg_handler)
    app.router.add_get("/sse/{cam_id}",    sse_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site   = web.TCPSite(runner, host, port)
    await site.start()

    log.info("=" * 52)
    log.info("  ESP32-CAM Multi-Camera NVR")
    log.info(f"  Web UI  → http://{host}:{port}")
    log.info(f"  UDP in  ← :{UDP_RECV}")
    log.info(f"  MJPEG   → http://<host>:{port}/stream/<cam_id>")
    log.info(f"  SSE     → http://<host>:{port}/sse/<cam_id>")
    log.info("  Waiting for cameras...")
    log.info("=" * 52)

    try:
        await asyncio.Event().wait()   # run forever
=======
        lambda: FrameReceiverProtocol(server),
        local_addr=("0.0.0.0", args.udp_port),
        allow_broadcast=True,
    )
    control_transport, _ = await loop.create_datagram_endpoint(
        lambda: ControlTransportProtocol(server),
        local_addr=("0.0.0.0", args.local_port),
        allow_broadcast=True,
    )

    app = web.Application()
    app["server"] = server
    app.router.add_get("/",                  index_handler)
    app.router.add_get("/ws",                websocket_handler)
    app.router.add_get("/api/stats",         stats_handler)
    app.router.add_post("/api/record/start",  record_start_handler)
    app.router.add_post("/api/record/stop",   record_stop_handler)
    app.router.add_get("/api/snapshot",       snapshot_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.web_port).start()

    log.info("=== ESP32-CAM Multi-Camera Streaming Server ===")
    log.info(f"Web UI   → http://localhost:{args.web_port}")
    log.info(f"UDP recv ← :{args.udp_port}")
    log.info(f"Control  → ESP32:{args.control_port}  (from :{args.local_port})")
    log.info("Waiting for ESP32-CAM cameras...")

    try:
        await asyncio.Future()
>>>>>>> 04800748ba083d03cb114330e14b25872fe506cc
    finally:
        frame_transport.close()
        ctrl_transport.close()
        await runner.cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ESP32-CAM NVR Streaming Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        log.info("Stopped.")
