"""
ESP32-CAM UDP Streaming Server

Receives JPEG frames from ESP32-CAM via UDP, sends ACK,
and streams to web clients via WebSocket. Includes a web UI
with camera controls, resolution switching, and flash control.

Usage:
    python server.py [--host 0.0.0.0] [--web-port 8080] [--udp-port 9000] [--control-port 9001] [--esp32-port 9002]
"""

import asyncio
import argparse
import struct
import time
import json
import logging
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

HEADER_SIZE = 12  # 2 magic + 4 frame_id + 2 chunk_idx + 2 total_chunks + 2 payload_len

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


# ====================== SERVER STATE ======================

class StreamServer:
    def __init__(self, udp_port: int, control_port: int, esp32_port: int):
        self.udp_port = udp_port
        self.control_port = control_port
        self.esp32_port = esp32_port

        # Frame reassembly
        self.frame_chunks: dict[int, dict[int, bytes]] = defaultdict(dict)
        self.frame_total_chunks: dict[int, int] = {}
        self.latest_complete_frame: bytes | None = None
        self.latest_frame_id: int = -1
        self.frame_event = asyncio.Event()

        # ESP32 address (discovered from first packet)
        self.esp32_addr: tuple[str, int] | None = None

        # UDP transport (set during startup)
        self.frame_transport: asyncio.DatagramTransport | None = None
        self.control_transport: asyncio.DatagramTransport | None = None

        # WebSocket clients
        self.ws_clients: set[web.WebSocketResponse] = set()

        # Stats
        self.frames_received = 0
        self.frames_dropped = 0
        self.bytes_received = 0
        self.last_frame_time = 0.0
        self.fps = 0.0
        self._fps_counter = 0
        self._fps_last_time = time.monotonic()

    def update_fps(self):
        """Update FPS calculation."""
        self._fps_counter += 1
        now = time.monotonic()
        elapsed = now - self._fps_last_time
        if elapsed >= 1.0:
            self.fps = self._fps_counter / elapsed
            self._fps_counter = 0
            self._fps_last_time = now

    def send_ack(self, frame_id: int):
        """Send ACK to ESP32 for completed frame."""
        if not self.esp32_addr or not self.frame_transport:
            return
        ack = MAGIC_ACK + struct.pack(">I", frame_id)
        self.frame_transport.sendto(ack, self.esp32_addr)

    def send_control(self, cmd_id: int, payload: bytes):
        """Send control command to ESP32."""
        if not self.esp32_addr or not self.control_transport:
            log.warning("Cannot send control: ESP32 address unknown")
            return
        esp32_control_addr = (self.esp32_addr[0], self.control_port)
        packet = MAGIC_CTRL + bytes([cmd_id]) + payload
        self.control_transport.sendto(packet, esp32_control_addr)
        log.info(f"Sent control cmd=0x{cmd_id:02x} payload={payload.hex()} to {esp32_control_addr}")

    def process_packet(self, data: bytes, addr: tuple[str, int]):
        """Process incoming UDP frame packet."""
        if len(data) < HEADER_SIZE:
            return

        # Validate magic
        if data[0:2] != MAGIC_FRAME:
            return

        # Parse header
        frame_id = struct.unpack(">I", data[2:6])[0]
        chunk_idx = struct.unpack(">H", data[6:8])[0]
        total_chunks = struct.unpack(">H", data[8:10])[0]
        payload_len = struct.unpack(">H", data[10:12])[0]

        if len(data) < HEADER_SIZE + payload_len:
            return

        payload = data[HEADER_SIZE:HEADER_SIZE + payload_len]

        # Remember ESP32 address
        if self.esp32_addr is None:
            self.esp32_addr = addr
            log.info(f"ESP32-CAM discovered at {addr}")

        self.esp32_addr = addr
        self.bytes_received += len(data)

        # Discard frames older than what we already have
        if frame_id < self.latest_frame_id:
            return

        # If a newer frame arrives, discard all older incomplete frames
        stale_ids = [fid for fid in self.frame_chunks if fid < frame_id]
        for fid in stale_ids:
            del self.frame_chunks[fid]
            if fid in self.frame_total_chunks:
                del self.frame_total_chunks[fid]
            self.frames_dropped += 1

        # Store chunk
        self.frame_chunks[frame_id][chunk_idx] = payload
        self.frame_total_chunks[frame_id] = total_chunks

        # Check if frame is complete
        if len(self.frame_chunks[frame_id]) == total_chunks:
            # Reassemble frame
            frame_data = b""
            for i in range(total_chunks):
                chunk = self.frame_chunks[frame_id].get(i)
                if chunk is None:
                    # Missing chunk - discard
                    del self.frame_chunks[frame_id]
                    del self.frame_total_chunks[frame_id]
                    return
                frame_data += chunk

            # Update latest frame
            self.latest_complete_frame = frame_data
            self.latest_frame_id = frame_id
            self.frames_received += 1
            self.last_frame_time = time.monotonic()
            self.update_fps()

            # Cleanup
            del self.frame_chunks[frame_id]
            del self.frame_total_chunks[frame_id]

            # Send ACK
            self.send_ack(frame_id)

            # Signal new frame available
            self.frame_event.set()
            self.frame_event.clear()


# ==================== UDP PROTOCOL ========================

class FrameReceiverProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: StreamServer):
        self.server = server

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.server.frame_transport = transport
        log.info(f"UDP frame receiver listening on port {self.server.udp_port}")

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        self.server.process_packet(data, addr)


class ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: StreamServer):
        self.server = server

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.server.control_transport = transport
        log.info(f"UDP control sender ready on port {self.server.esp32_port}")

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        pass  # We don't expect data on this port from ESP32


# ==================== WEB HANDLERS ========================

async def index_handler(request: web.Request) -> web.Response:
    """Serve the main web UI."""
    import os
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler for streaming frames and receiving controls."""
    server: StreamServer = request.app["server"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    server.ws_clients.add(ws)
    log.info(f"WebSocket client connected ({len(server.ws_clients)} total)")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                    handle_ws_command(server, cmd)
                except json.JSONDecodeError:
                    log.warning(f"Invalid JSON from WS client: {msg.data}")
            elif msg.type == web.WSMsgType.ERROR:
                log.error(f"WebSocket error: {ws.exception()}")
    finally:
        server.ws_clients.discard(ws)
        log.info(f"WebSocket client disconnected ({len(server.ws_clients)} total)")

    return ws


def handle_ws_command(server: StreamServer, cmd: dict):
    """Process a control command from WebSocket client."""
    action = cmd.get("action")
    if not action:
        return

    if action == "control":
        param = cmd.get("param")
        value = cmd.get("value")
        if param is None or value is None:
            return

        cmd_id = CMD_MAP.get(param)
        if cmd_id is None:
            log.warning(f"Unknown parameter: {param}")
            return

        # Build payload based on command type
        if param == "aec_value":
            # 16-bit value
            value = max(0, min(1200, int(value)))
            payload = struct.pack(">H", value)
        elif param in ("brightness", "contrast", "saturation", "sharpness", "ae_level"):
            # Signed 8-bit
            value = max(-2, min(2, int(value)))
            payload = struct.pack("b", value)
        else:
            # Unsigned 8-bit
            value = max(0, min(255, int(value)))
            payload = bytes([value])

        server.send_control(cmd_id, payload)

    elif action == "get_stats":
        # Return stats (sent in broadcast loop)
        pass


async def stats_handler(request: web.Request) -> web.Response:
    """Return current streaming stats as JSON."""
    server: StreamServer = request.app["server"]
    stats = {
        "fps": round(server.fps, 1),
        "frames_received": server.frames_received,
        "frames_dropped": server.frames_dropped,
        "bytes_received": server.bytes_received,
        "esp32_connected": server.esp32_addr is not None,
        "esp32_addr": f"{server.esp32_addr[0]}:{server.esp32_addr[1]}" if server.esp32_addr else None,
        "ws_clients": len(server.ws_clients),
    }
    return web.json_response(stats)


# ================== FRAME BROADCASTER ====================

async def frame_broadcaster(app: web.Application):
    """Background task: push latest frame to all WebSocket clients."""
    server: StreamServer = app["server"]
    last_sent_frame_id = -1

    while True:
        # Wait for a new frame or check periodically
        try:
            await asyncio.wait_for(server.frame_event.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass

        if server.latest_complete_frame is None:
            continue

        if server.latest_frame_id <= last_sent_frame_id:
            continue

        frame = server.latest_complete_frame
        frame_id = server.latest_frame_id
        last_sent_frame_id = frame_id

        # Send to all connected WebSocket clients
        dead_clients = set()
        for ws in server.ws_clients:
            try:
                await ws.send_bytes(frame)
            except Exception:
                dead_clients.add(ws)

        # Remove dead clients
        for ws in dead_clients:
            server.ws_clients.discard(ws)

        # Also send stats periodically
        if frame_id % 30 == 0:
            stats = json.dumps({
                "type": "stats",
                "fps": round(server.fps, 1),
                "frames": server.frames_received,
                "dropped": server.frames_dropped,
                "bytes": server.bytes_received,
            })
            for ws in list(server.ws_clients):
                try:
                    await ws.send_str(stats)
                except Exception:
                    server.ws_clients.discard(ws)


async def start_background_tasks(app: web.Application):
    """Start background tasks on app startup."""
    app["broadcaster"] = asyncio.create_task(frame_broadcaster(app))


async def cleanup_background_tasks(app: web.Application):
    """Cleanup on app shutdown."""
    app["broadcaster"].cancel()
    try:
        await app["broadcaster"]
    except asyncio.CancelledError:
        pass


# ======================== MAIN ============================

async def main():
    parser = argparse.ArgumentParser(description="ESP32-CAM UDP Streaming Server")
    parser.add_argument("--host", default="0.0.0.0", help="Web server bind address")
    parser.add_argument("--web-port", type=int, default=8080, help="Web server port")
    parser.add_argument("--udp-port", type=int, default=9000, help="UDP port for receiving frames")
    parser.add_argument("--control-port", type=int, default=9001, help="UDP port for ESP32 control commands")
    parser.add_argument("--esp32-port", type=int, default=9002, help="ESP32 source port")
    args = parser.parse_args()

    # Create server state
    server = StreamServer(args.udp_port, args.control_port, args.esp32_port)

    # Setup UDP listeners
    loop = asyncio.get_event_loop()

    frame_transport, _ = await loop.create_datagram_endpoint(
        lambda: FrameReceiverProtocol(server),
        local_addr=("0.0.0.0", args.udp_port),
    )

    control_transport, _ = await loop.create_datagram_endpoint(
        lambda: ControlProtocol(server),
        local_addr=("0.0.0.0", args.esp32_port),
    )

    log.info(f"UDP frame receiver on port {args.udp_port}")
    log.info(f"UDP control sender on port {args.esp32_port}")

    # Setup web app
    app = web.Application()
    app["server"] = server
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/api/stats", stats_handler)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    # Start web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.web_port)
    await site.start()

    log.info(f"Web UI available at http://{args.host}:{args.web_port}")
    log.info("Waiting for ESP32-CAM connection...")

    # Run forever
    try:
        await asyncio.Future()  # run forever
    finally:
        frame_transport.close()
        control_transport.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
