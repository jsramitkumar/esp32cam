"""
ESP32-CAM AI Detection Server
==============================
Port 8181 — HTTP + WebSocket AI dashboard

Architecture:
  1. Connects to streaming server ws://localhost:8080/ws as a WS CLIENT
  2. Forwards every JPEG frame IMMEDIATELY to dashboard clients (zero-latency pass-through)
  3. Asynchronously decodes JPEG → runs YOLO → sends JSON detections to dashboard
  4. Dashboard draws bounding boxes on canvas in JS (no server-side annotation cost)

WebSocket (server → dashboard):
  binary : [cam_id:1][jpeg:N]          — live frame (pass-through, no annotation)
  JSON   : {"type":"detections", ...}  — detection results for current frame
  JSON   : {"type":"cam_list", ...}    — camera list from streaming server
  JSON   : {"type":"cam_joined", ...}
  JSON   : {"type":"cam_removed", ...}
  JSON   : {"type":"stats", ...}
  JSON   : {"type":"server_status", "streaming":bool}
  JSON   : {"type":"model_loading" | "model_loaded", ...}
  JSON   : {"type":"ai_status", "ai_on":bool}

WebSocket (dashboard → server):
  {"action":"load_model",  "model":"yolov8n"}
  {"action":"set_ai",      "enabled":true}
  {"action":"set_conf",    "cam_id":N|null, "value":0.4}
  {"action":"set_iou",     "cam_id":N|null, "value":0.45}
  {"action":"set_classes", "cam_id":N|null, "classes":[0,2]|null}
  {"action":"get_stats"}
"""

import asyncio
import json
import time
import logging
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from aiohttp import web
import numpy as np
import cv2

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ai")

# ── Config ────────────────────────────────────────────────────────────────────
STREAMING_WS   = os.environ.get("STREAMING_WS",   "ws://127.0.0.1:8080/ws")
STREAMING_HTTP = os.environ.get("STREAMING_HTTP",  "http://127.0.0.1:8080")
AI_PORT        = int(os.environ.get("AI_PORT", 8181))

AVAILABLE_MODELS: dict[str, str] = {
     "yolo26s":  "yolo26s.pt",
}

# ── YOLO availability ─────────────────────────────────────────────────────────
YOLO_AVAILABLE = False
try:
    from ultralytics import YOLO as _YOLO
    YOLO_AVAILABLE = True
    log.info("ultralytics YOLO ready")
except ImportError:
    log.warning("ultralytics not found – run: pip install ultralytics")


# ═══════════════════════════════════════════════════════════════════════════════
# Model Manager
# ═══════════════════════════════════════════════════════════════════════════════
class ModelManager:
    def __init__(self):
        self.model      = None
        self.model_name = "none"
        self.loading    = False
        self._executor  = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yolo-load")
        self._inf_lock  = asyncio.Lock()   # serialize inference calls

    async def load(self, name: str) -> bool:
        if not YOLO_AVAILABLE:
            log.error("ultralytics not installed")
            return False
        self.loading = True
        try:
            path = AVAILABLE_MODELS.get(name, name)
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(
                self._executor,
                lambda: _YOLO(path)
            )
            self.model      = model
            self.model_name = name
            log.info(f"Model loaded: {name}")
            return True
        except Exception as e:
            log.error(f"Failed to load model '{name}': {e}")
            return False
        finally:
            self.loading = False


# ═══════════════════════════════════════════════════════════════════════════════
# Per-camera AI state
# ═══════════════════════════════════════════════════════════════════════════════
class CamAIState:
    def __init__(self, cam_id: int):
        self.cam_id     = cam_id
        self.busy       = False
        self.conf       = 0.40
        self.iou        = 0.45
        self.classes    = None       # None = all COCO classes

        self.last_dets  = []
        self.last_ms    = 0.0
        self.frames_in  = 0
        self.frames_out = 0
        self.dropped    = 0

        self._fps_n     = 0
        self._fps_t     = time.monotonic()
        self.fps        = 0.0

    def tick_fps(self):
        self._fps_n += 1
        now = time.monotonic()
        dt  = now - self._fps_t
        if dt >= 1.0:
            self.fps     = round(self._fps_n / dt, 1)
            self._fps_n  = 0
            self._fps_t  = now

    def to_dict(self) -> dict:
        return {
            "fps":        self.fps,
            "inf_ms":     round(self.last_ms, 1),
            "frames_in":  self.frames_in,
            "frames_out": self.frames_out,
            "dropped":    self.dropped,
            "detections": self.last_dets,
            "conf":       self.conf,
            "iou":        self.iou,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Sync inference  (runs inside ThreadPoolExecutor)
# ═══════════════════════════════════════════════════════════════════════════════
def _run_inference(model, jpeg_bytes: bytes, conf: float, iou: float,
                   classes) -> tuple[list, float]:
    """Decode JPEG, run YOLO, return (detections, inference_ms).
    Does NOT draw on the frame – annotation is done client-side for low latency.
    """
    arr   = np.frombuffer(jpeg_bytes, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return [], 0.0

    h, w = frame.shape[:2]
    t0   = time.perf_counter()
    results = model.predict(
        frame,
        conf=conf,
        iou=iou,
        classes=classes,
        verbose=False,
        stream=False,
    )
    ms = (time.perf_counter() - t0) * 1000.0

    dets = []
    for r in results:
        for box in r.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            dets.append({
                "label":      r.names[int(box.cls[0])],
                "confidence": round(float(box.conf[0]), 3),
                "box":        [x1, y1, x2, y2],
                "class_id":   int(box.cls[0]),
                "frame_w":    w,
                "frame_h":    h,
            })
    return dets, ms


# ═══════════════════════════════════════════════════════════════════════════════
# AI Server
# ═══════════════════════════════════════════════════════════════════════════════
class AIServer:
    def __init__(self):
        self.model_mgr      = ModelManager()
        self.cam_states:    dict[int, CamAIState] = {}
        self.cameras_meta:  dict[int, dict]       = {}
        self.ws_clients:    set[web.WebSocketResponse] = set()
        self.streaming_ws   = None
        self.ai_on          = True
        self._executor      = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inf")

    # ── Streaming server connection ───────────────────────────────────────────
    async def run_streaming_connector(self):
        """Background task: maintain WS connection to streaming server."""
        while True:
            try:
                log.info(f"Connecting → {STREAMING_WS}")
                timeout = aiohttp.ClientTimeout(total=None, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.ws_connect(
                        STREAMING_WS,
                        max_msg_size=0,
                        heartbeat=20,
                    ) as ws:
                        self.streaming_ws = ws
                        log.info("Streaming server connected")
                        await self._broadcast_json({
                            "type": "server_status", "streaming": True
                        })
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._on_stream_text(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await self._on_stream_binary(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                break
            except Exception as e:
                log.warning(f"Streaming server lost: {e}")
            finally:
                self.streaming_ws = None
                await self._broadcast_json({
                    "type": "server_status", "streaming": False
                })
            await asyncio.sleep(4)

    async def _on_stream_text(self, data: str):
        try:
            msg = json.loads(data)
        except Exception:
            return

        t = msg.get("type", "")

        if t == "cam_list":
            for d in msg.get("cameras", {}).values():
                cid = int(d.get("cam_id", -1))
                if cid >= 0:
                    self.cameras_meta[cid] = d
                    self.cam_states.setdefault(cid, CamAIState(cid))
            await self._broadcast_json({
                "type": "cam_list", "cameras": self.cameras_meta
            })

        elif t in ("cam_joined", "cam_status"):
            d   = msg.get("data", {})
            cid = int(msg.get("cam_id", -1))
            if cid >= 0:
                self.cameras_meta[cid] = d
                self.cam_states.setdefault(cid, CamAIState(cid))
            await self._broadcast_json(msg)

        elif t == "cam_removed":
            cid = int(msg.get("cam_id", -1))
            self.cameras_meta.pop(cid, None)
            self.cam_states.pop(cid, None)
            await self._broadcast_json(msg)

        elif t == "stats":
            # Merge AI stats into camera stats before forwarding
            for cid_str, d in msg.get("cameras", {}).items():
                cid = int(cid_str)
                if cid in self.cam_states:
                    s = self.cam_states[cid]
                    d["ai_fps"]    = s.fps
                    d["ai_inf_ms"] = round(s.last_ms, 1)
                    d["ai_dets"]   = len(s.last_dets)
            await self._broadcast_json(msg)

        else:
            await self._broadcast_json(msg)

    async def _on_stream_binary(self, data: bytes):
        if len(data) < 2:
            return
        cam_id = data[0]
        jpeg   = data[1:]

        state = self.cam_states.setdefault(cam_id, CamAIState(cam_id))
        state.frames_in += 1
        state.tick_fps()

        # ── Pass-through frame IMMEDIATELY (zero-latency display) ────────────
        await self._broadcast_frame(cam_id, jpeg)

        # ── Async inference (non-blocking, drop if busy) ─────────────────────
        if (
            self.ai_on
            and self.model_mgr.model is not None
            and not self.model_mgr.loading
            and not state.busy
        ):
            state.busy = True
            asyncio.create_task(self._infer_async(cam_id, jpeg, state))
        elif state.busy:
            state.dropped += 1

    async def _infer_async(self, cam_id: int, jpeg: bytes, state: CamAIState):
        try:
            model   = self.model_mgr.model
            conf    = state.conf
            iou     = state.iou
            classes = state.classes
            loop    = asyncio.get_event_loop()

            dets, ms = await loop.run_in_executor(
                self._executor,
                lambda: _run_inference(model, jpeg, conf, iou, classes),
            )

            state.last_dets  = dets
            state.last_ms    = ms
            state.frames_out += 1

            await self._broadcast_json({
                "type":       "detections",
                "cam_id":     cam_id,
                "detections": dets,
                "inf_ms":     round(ms, 1),
                "model":      self.model_mgr.model_name,
            })
        except Exception as e:
            log.error(f"Inference error cam {cam_id}: {e}")
        finally:
            state.busy = False

    # ── Broadcast helpers ─────────────────────────────────────────────────────
    async def _broadcast_frame(self, cam_id: int, jpeg: bytes):
        data = bytes([cam_id]) + jpeg
        dead = set()
        for ws in list(self.ws_clients):
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.add(ws)
        self.ws_clients -= dead

    async def _broadcast_json(self, obj: dict):
        data = json.dumps(obj)
        dead = set()
        for ws in list(self.ws_clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.add(ws)
        self.ws_clients -= dead

    # ── HTTP handlers ─────────────────────────────────────────────────────────
    async def handle_index(self, request: web.Request) -> web.Response:
        path = Path(__file__).parent / "ai_dashboard.html"
        return web.FileResponse(path)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)

        # Send current state immediately on connect
        await ws.send_str(json.dumps({
            "type":      "init",
            "streaming": (
                self.streaming_ws is not None
                and not self.streaming_ws.closed
            ),
            "model":     self.model_mgr.model_name,
            "ai_on":     self.ai_on,
            "cameras":   self.cameras_meta,
            "yolo_ok":   YOLO_AVAILABLE,
        }))

        self.ws_clients.add(ws)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_dashboard_msg(ws, msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        finally:
            self.ws_clients.discard(ws)

        return ws

    async def _on_dashboard_msg(self, ws: web.WebSocketResponse, data: str):
        try:
            msg = json.loads(data)
        except Exception:
            return

        action = msg.get("action", "")

        if action == "load_model":
            name = msg.get("model", "yolov8n")
            await self._broadcast_json({"type": "model_loading", "model": name})
            ok   = await self.model_mgr.load(name)
            await self._broadcast_json({
                "type":    "model_loaded",
                "model":   self.model_mgr.model_name,
                "success": ok,
            })

        elif action == "unload_model":
            self.model_mgr.model      = None
            self.model_mgr.model_name = "none"
            await self._broadcast_json({"type": "model_loaded", "model": "none", "success": True})

        elif action == "set_ai":
            self.ai_on = bool(msg.get("enabled", True))
            await self._broadcast_json({"type": "ai_status", "ai_on": self.ai_on})

        elif action == "set_conf":
            val = float(msg.get("value", 0.4))
            cid = msg.get("cam_id")
            targets = (
                [self.cam_states[cid]] if cid in self.cam_states
                else list(self.cam_states.values())
            )
            for s in targets:
                s.conf = val

        elif action == "set_iou":
            val = float(msg.get("value", 0.45))
            cid = msg.get("cam_id")
            targets = (
                [self.cam_states[cid]] if cid in self.cam_states
                else list(self.cam_states.values())
            )
            for s in targets:
                s.iou = val

        elif action == "set_classes":
            cls_list = msg.get("classes")   # None → all, list[int] → filter
            cid = msg.get("cam_id")
            targets = (
                [self.cam_states[cid]] if cid in self.cam_states
                else list(self.cam_states.values())
            )
            for s in targets:
                s.classes = cls_list

        elif action == "get_stats":
            stats = {str(cid): s.to_dict() for cid, s in self.cam_states.items()}
            await ws.send_str(json.dumps({"type": "ai_stats", "cameras": stats}))

    async def handle_api_cameras(self, request: web.Request) -> web.Response:
        """Proxy streaming server /api/stats so the dashboard can list cameras."""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"{STREAMING_HTTP}/api/stats",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    return web.json_response(await r.json())
        except Exception:
            return web.json_response({"cameras": self.cameras_meta})

    async def handle_api_models(self, request: web.Request) -> web.Response:
        return web.json_response({
            "available": list(AVAILABLE_MODELS.keys()),
            "current":   self.model_mgr.model_name,
            "yolo_ok":   YOLO_AVAILABLE,
        })

    async def handle_api_ai_stats(self, request: web.Request) -> web.Response:
        return web.json_response({
            str(cid): s.to_dict() for cid, s in self.cam_states.items()
        })


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    server = AIServer()

    app = web.Application()
    app.router.add_get("/",               server.handle_index)
    app.router.add_get("/ws",             server.handle_ws)
    app.router.add_get("/api/cameras",    server.handle_api_cameras)
    app.router.add_get("/api/models",     server.handle_api_models)
    app.router.add_get("/api/ai_stats",   server.handle_api_ai_stats)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", AI_PORT)
    await site.start()
    log.info(f"AI Server → http://0.0.0.0:{AI_PORT}")
    log.info(f"Streaming server → {STREAMING_WS}")

    asyncio.create_task(server.run_streaming_connector())

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
