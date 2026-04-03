"""
ESP32-CAM Multi-Camera UDP Streaming Server

Supports multiple ESP32-CAM devices simultaneously, each auto-discovered
by source IP. Streams to browser via WebSocket with per-camera multiplexing.
Receives telemetry (RSSI, heap, uptime, FPS) from each camera.

Binary WS frame format:  [cam_id: 1 byte][JPEG data: N bytes]
Text WS messages: JSON with "type" field

Usage:
    python server.py [--host 0.0.0.0] [--web-port 8080] [--udp-port 9000]
                     [--control-port 9001] [--local-port 9002]
"""

import asyncio
import argparse
import struct
import time
import json
import logging
import os
import io
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
MAGIC_FRAME = bytes([0xCA, 0x4D])
MAGIC_ACK   = bytes([0xAC, 0x4B])
MAGIC_CTRL  = bytes([0xC0, 0x4D])
MAGIC_TELE  = bytes([0x54, 0x45])

HEADER_SIZE = 12  # magic:2 + frame_id:4 + chunk_idx:2 + total:2 + payload_len:2

CAM_TIMEOUT_SECS = 5.0
CAM_REMOVE_SECS  = 60.0

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


# ──────────────────────────────────────────────────────────────────────────────
# Camera Session
# ──────────────────────────────────────────────────────────────────────────────
class CameraSession:
    def __init__(self, cam_id: int, ip: str, control_port: int):
        self.cam_id       = cam_id
        self.ip           = ip
        self.control_port = control_port
        self.name         = f"CAM-{cam_id}"

        self.frame_chunks:       dict[int, dict[int, bytes]] = defaultdict(dict)
        self.frame_total_chunks: dict[int, int] = {}

        self.latest_frame:    bytes | None = None
        self.latest_frame_id: int = -1
        self.last_broadcast_id: int = -1

        self.frames_received = 0
        self.frames_dropped  = 0
        self.bytes_received  = 0
        self.fps   = 0.0
        self.kbps  = 0.0
        self._fps_count  = 0
        self._fps_time   = time.monotonic()
        self._kbps_bytes = 0
        self._kbps_time  = time.monotonic()

        self.rssi         = 0
        self.free_heap_kb = 0
        self.uptime_secs  = 0
        self.esp_fps      = 0
        self.resolution   = "VGA (480p)"

        self.connected = True
        self.last_seen = time.monotonic()
        self.joined_at = time.monotonic()

        self.recording         = False
        self.record_frames:    list[bytes] = []
        self.record_start_ts:  float = 0.0
        self.record_frame_ts:  list[float] = []

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


# ──────────────────────────────────────────────────────────────────────────────
# Multi-Camera Server
# ──────────────────────────────────────────────────────────────────────────────
class MultiCamServer:
    def __init__(self, udp_port: int, control_port: int, local_port: int):
        self.udp_port     = udp_port
        self.control_port = control_port
        self.local_port   = local_port

        self.cameras_by_ip: dict[str, CameraSession] = {}
        self.cameras_by_id: dict[int, CameraSession] = {}
        self._next_cam_id = 0

        self.frame_transport:   asyncio.DatagramTransport | None = None
        self.control_transport: asyncio.DatagramTransport | None = None

        self.ws_clients: set[web.WebSocketResponse] = set()
        self.any_new_frame = asyncio.Event()

    def get_or_create(self, ip: str) -> tuple[CameraSession, bool]:
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
        if frame_id < cam.latest_frame_id:
            return
        stale = [fid for fid in cam.frame_chunks if fid < frame_id]
        for fid in stale:
            cam.frame_chunks.pop(fid, None)
            cam.frame_total_chunks.pop(fid, None)
            cam.frames_dropped += 1
        cam.frame_chunks[frame_id][chunk_idx] = payload
        cam.frame_total_chunks[frame_id] = total_chunks
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
        log.info(f"-> {cam.name} cmd=0x{cmd_id:02x} payload={payload.hex()}")

    def _notify(self, cam: CameraSession, msg_type: str):
        msg = json.dumps({"type": msg_type, "cam_id": cam.cam_id, "data": cam.to_dict()})
        dead = set()
        for ws in self.ws_clients:
            if ws.closed:
                dead.add(ws)
            else:
                asyncio.ensure_future(ws.send_str(msg))
        self.ws_clients -= dead


# ──────────────────────────────────────────────────────────────────────────────
# UDP Protocols
# ──────────────────────────────────────────────────────────────────────────────
class FrameReceiverProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: MultiCamServer):
        self.server = server

    def connection_made(self, transport):
        self.server.frame_transport = transport
        log.info(f"UDP frame receiver on :{self.server.udp_port}")

    def datagram_received(self, data: bytes, addr):
        self.server.process_packet(data, addr)

    def error_received(self, exc):
        log.error(f"UDP frame error: {exc}")


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


# ──────────────────────────────────────────────────────────────────────────────
# HTTP / WebSocket Handlers
# ──────────────────────────────────────────────────────────────────────────────
async def index_handler(request: web.Request) -> web.Response:
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    server: MultiCamServer = request.app["server"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    server.ws_clients.add(ws)
    log.info(f"WS connected from {request.remote} ({len(server.ws_clients)} total)")
    await ws.send_str(json.dumps({
        "type":    "cam_list",
        "cameras": {str(cid): cam.to_dict() for cid, cam in server.cameras_by_id.items()},
    }))
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    _handle_cmd(server, json.loads(msg.data))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                    log.debug(f"Bad WS cmd: {e}")
            elif msg.type == web.WSMsgType.ERROR:
                log.error(f"WS error: {ws.exception()}")
    finally:
        server.ws_clients.discard(ws)
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
    if param == "aec_value":
        payload = struct.pack(">H", max(0, min(1200, int(value))))
    elif param in ("brightness", "contrast", "saturation", "sharpness", "ae_level"):
        payload = struct.pack("b", max(-2, min(2, int(value))))
    else:
        payload = bytes([max(0, min(255, int(value)))])
    server.send_control(cam_id, cmd_id, payload)


async def stats_handler(request: web.Request) -> web.Response:
    server: MultiCamServer = request.app["server"]
    return web.json_response({
        "cameras":       {str(cid): cam.to_dict() for cid, cam in server.cameras_by_id.items()},
        "ws_clients":    len(server.ws_clients),
        "total_cameras": sum(1 for c in server.cameras_by_id.values() if c.connected),
    })


async def record_start_handler(request: web.Request) -> web.Response:
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
    log.info(f"{cam.name}: recording stopped -- {n} frames")
    if n == 0:
        return web.json_response({"error": "no frames recorded"})
    avi_bytes = _build_avi(frames, frame_ts)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{cam.name}_{ts}.avi"
    return web.Response(
        body=avi_bytes,
        content_type="video/x-msvideo",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def snapshot_handler(request: web.Request) -> web.Response:
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


# ──────────────────────────────────────────────────────────────────────────────
# AVI Builder (pure Python, no ffmpeg)
# ──────────────────────────────────────────────────────────────────────────────
def _le16(v: int) -> bytes: return struct.pack("<H", v & 0xFFFF)
def _le32(v: int) -> bytes: return struct.pack("<I", v & 0xFFFFFFFF)

def _riff(fourcc: str, data: bytes) -> bytes:
    return fourcc.encode() + _le32(len(data)) + data

def _list_chunk(fourcc: str, chunks: list[bytes]) -> bytes:
    inner = fourcc.encode() + b"".join(chunks)
    return b"LIST" + _le32(len(inner)) + inner

def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    i = 0
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC2):
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
    return 640, 480


def _build_avi(frames: list[bytes], timestamps: list[float]) -> bytes:
    n = len(frames)
    if n >= 2:
        total_t = timestamps[-1] - timestamps[0]
        fps_num = max(1, round((n - 1) / max(0.001, total_t)))
    else:
        fps_num = 25
    fps_den = 1
    width, height = _jpeg_dimensions(frames[0])

    movi_chunks: list[bytes] = []
    idx_entries  = bytearray()
    movi_offset  = 4
    for jpeg in frames:
        tag    = b"00dc"
        padded = jpeg if len(jpeg) % 2 == 0 else jpeg + b"\x00"
        chunk  = tag + _le32(len(jpeg)) + padded
        idx_entries += tag
        idx_entries += _le32(0x10)
        idx_entries += _le32(movi_offset)
        idx_entries += _le32(len(jpeg))
        movi_offset += 8 + len(padded)
        movi_chunks.append(chunk)

    movi_data = b"movi" + b"".join(movi_chunks)
    idx1_data = b"idx1" + _le32(len(idx_entries)) + bytes(idx_entries)
    max_bytes = max(len(f) for f in frames)
    avg_bytes = sum(len(f) for f in frames) // n

    avih = (
        _le32(1_000_000 // fps_num) +
        _le32(avg_bytes * fps_num) +
        _le32(0) +
        _le32(0x910) +
        _le32(n) +
        _le32(0) +
        _le32(1) +
        _le32(max_bytes) +
        _le32(width) +
        _le32(height) +
        _le32(0) * 4
    )
    strh = (
        b"vids" + b"MJPG" +
        _le32(0) + _le16(0) + _le16(0) +
        _le32(0) + _le32(fps_den) + _le32(fps_num) +
        _le32(0) + _le32(n) + _le32(max_bytes) + _le32(10000) + _le32(0) +
        _le16(0) + _le16(0) + _le16(width) + _le16(height)
    )
    strf = (
        _le32(40) + _le32(width) + _le32(height) +
        _le16(1) + _le16(24) + b"MJPG" +
        _le32(width * height * 3) +
        _le32(0) + _le32(0) + _le32(0) + _le32(0)
    )
    strl      = _list_chunk("strl", [_riff("strh", strh), _riff("strf", strf)])
    hdrl      = _list_chunk("hdrl", [_riff("avih", avih), strl])
    riff_data = hdrl + movi_data + idx1_data
    return b"RIFF" + _le32(len(riff_data) + 4) + b"AVI " + riff_data


# ──────────────────────────────────────────────────────────────────────────────
# Background Tasks
# ──────────────────────────────────────────────────────────────────────────────
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
                    dead.add(ws)
                    continue
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
                dead.add(ws)
                continue
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


# ──────────────────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="ESP32-CAM Multi-Cam UDP Streaming Server")
    parser.add_argument("--host",         default="0.0.0.0")
    parser.add_argument("--web-port",     type=int, default=8080)
    parser.add_argument("--udp-port",     type=int, default=9000,  help="Receive frames")
    parser.add_argument("--control-port", type=int, default=9001,  help="ESP32 control listen port")
    parser.add_argument("--local-port",   type=int, default=9002,  help="Server ACK/control send port")
    args = parser.parse_args()

    server = MultiCamServer(args.udp_port, args.control_port, args.local_port)
    loop   = asyncio.get_event_loop()

    frame_transport, _ = await loop.create_datagram_endpoint(
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
    app.router.add_post("/api/record/start", record_start_handler)
    app.router.add_post("/api/record/stop",  record_stop_handler)
    app.router.add_get("/api/snapshot",      snapshot_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.web_port).start()

    log.info("=== ESP32-CAM Multi-Camera Streaming Server ===")
    log.info(f"Web UI   -> http://localhost:{args.web_port}")
    log.info(f"UDP recv <- :{args.udp_port}")
    log.info(f"Control  -> ESP32:{args.control_port}  (from :{args.local_port})")
    log.info("Waiting for ESP32-CAM cameras...")

    try:
        await asyncio.Future()
    finally:
        frame_transport.close()
        control_transport.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
