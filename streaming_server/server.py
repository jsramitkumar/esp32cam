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
from collections import defaultdict

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("streamer")

# ======================== PROTOCOL ========================

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

CMD_MAP = {
    "resolution":     CMD_RESOLUTION,
    "brightness":     CMD_BRIGHTNESS,
    "contrast":       CMD_CONTRAST,
    "saturation":     CMD_SATURATION,
    "sharpness":      CMD_SHARPNESS,
    "awb":            CMD_AWB,
    "awb_gain":       CMD_AWB_GAIN,
    "wb_mode":        CMD_WB_MODE,
    "aec":            CMD_AEC,
    "aec2":           CMD_AEC2,
    "ae_level":       CMD_AE_LEVEL,
    "aec_value":      CMD_AEC_VALUE,
    "agc":            CMD_AGC,
    "agc_gain":       CMD_AGC_GAIN,
    "gainceiling":    CMD_GAINCEILING,
    "bpc":            CMD_BPC,
    "wpc":            CMD_WPC,
    "raw_gma":        CMD_RAW_GMA,
    "lenc":           CMD_LENC,
    "hmirror":        CMD_HMIRROR,
    "vflip":          CMD_VFLIP,
    "dcw":            CMD_DCW,
    "colorbar":       CMD_COLORBAR,
    "flash":          CMD_FLASH,
    "quality":        CMD_QUALITY,
    "special_effect": CMD_SPECIAL_EFFECT,
}


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


# ==================== WEB HANDLERS ========================

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

    # Send current camera list on connect
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


# ======================== MAIN ============================

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
    app.router.add_get("/",          index_handler)
    app.router.add_get("/ws",        websocket_handler)
    app.router.add_get("/api/stats", stats_handler)
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
    finally:
        frame_transport.close()
        control_transport.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
