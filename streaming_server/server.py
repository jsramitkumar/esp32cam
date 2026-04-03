"""
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
"""

import asyncio
import argparse
import struct
import time
import json
import logging
import os
import io
import base64
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

# Watchdog
CAM_TIMEOUT   = 5.0    # secs without a frame → mark offline
CAM_REMOVE    = 60.0   # secs offline → remove from session

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
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        if len(data) < 2:
            return
        if data[0] == MAGIC_FRAME[0] and data[1] == MAGIC_FRAME[1]:
            self.server.on_frame_packet(data, addr)
        elif data[0] == MAGIC_TELE[0] and data[1] == MAGIC_TELE[1]:
            self.server.on_telemetry(data, addr)


class ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        pass  # control port is outbound-only


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
    path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
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

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    _handle_control(server, json.loads(msg.data))
                except Exception as e:
                    log.debug(f"Bad WS msg: {e}")
            elif msg.type == web.WSMsgType.ERROR:
                log.error(f"WS error: {ws.exception()}")
    finally:
        server.ws_clients.discard(ws)
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
    if param == "aec_value":
        payload = struct.pack(">H", max(0, min(1200, int(value))))
    elif param in ("brightness", "contrast", "saturation", "sharpness", "ae_level"):
        payload = struct.pack("b", max(-2, min(2, int(value))))
    else:
        payload = bytes([max(0, min(255, int(value)))])
    server.send_control(cam_id, cid, payload)


# ── Stats API ─────────────────────────────────────────────────────────────────
async def stats_handler(request: web.Request) -> web.Response:
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


# ── SSE stream endpoint ───────────────────────────────────────────────────────
async def sse_handler(request: web.Request) -> web.StreamResponse:
    server: NVRServer = request.app["server"]
    cam_id = int(request.match_info.get("cam_id", -1))
    cam = server.cameras_by_id.get(cam_id)
    if not cam:
        return web.Response(status=404, text="Camera not found")

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

    # UDP frame receiver
    fp = FrameProtocol(server)
    server._frame_proto = fp
    frame_transport, _ = await loop.create_datagram_endpoint(
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
