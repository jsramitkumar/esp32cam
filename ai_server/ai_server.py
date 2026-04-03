"""
ESP32-CAM AI Detection Server  — YOLO26s Full Feature Edition
==============================================================
Port 8181 — HTTP + WebSocket AI dashboard

Architecture:
  1. Connects to streaming server ws://localhost:8080/ws as a WS CLIENT
  2. Forwards every JPEG frame IMMEDIATELY to dashboard (zero-latency pass-through)
  3. Asynchronously decodes JPEG → runs YOLO26s → sends JSON results to dashboard
  4. Dashboard draws overlays on canvas in JS (no server-side annotation cost)

YOLO26s Task Modes (switchable at runtime):
  - detect    : Object detection  (boxes + labels)
  - segment   : Instance segmentation (masks + boxes)
  - pose      : Pose estimation  (keypoints + boxes)
  - obb       : Oriented bounding boxes
  - track     : Multi-object tracking (ByteTrack / BoT-SORT)
  - classify  : Image classification (top-k)

WebSocket (server → dashboard):
  binary : [cam_id:1][jpeg:N]             — live frame pass-through
  JSON   : {"type":"detections",   ...}   — detect / segment / obb results
  JSON   : {"type":"poses",        ...}   — pose estimation results
  JSON   : {"type":"tracks",       ...}   — tracking results
  JSON   : {"type":"classification",...}  — classification top-k results
  JSON   : {"type":"cam_list",     ...}
  JSON   : {"type":"cam_joined",   ...}
  JSON   : {"type":"cam_removed",  ...}
  JSON   : {"type":"stats",        ...}
  JSON   : {"type":"server_status","streaming":bool}
  JSON   : {"type":"model_loading" | "model_loaded", ...}
  JSON   : {"type":"ai_status",    "ai_on":bool}
  JSON   : {"type":"task_changed", "task":str}

WebSocket (dashboard → server):
  {"action":"load_model",    "model":"yolo26s"}
  {"action":"set_task",      "task":"detect"|"segment"|"pose"|"obb"|"track"|"classify"}
  {"action":"set_ai",        "enabled":true}
  {"action":"set_conf",      "cam_id":N|null, "value":0.4}
  {"action":"set_iou",       "cam_id":N|null, "value":0.45}
  {"action":"set_classes",   "cam_id":N|null, "classes":[0,2]|null}
  {"action":"set_tracker",   "tracker":"bytetrack"|"botsort"}
  {"action":"set_max_det",   "value":300}
  {"action":"set_agnostic_nms","value":false}
  {"action":"set_retina_masks","value":false}
  {"action":"set_topk",      "value":5}
  {"action":"set_half",      "value":false}
  {"action":"set_imgsz",     "value":640}
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
    "yolo26s": "yolo26s.pt",
}

VALID_TASKS = {"detect", "segment", "pose", "obb", "track", "classify"}

# ── YOLO availability ─────────────────────────────────────────────────────────
YOLO_AVAILABLE = False
try:
    from ultralytics import YOLO as _YOLO
    YOLO_AVAILABLE = True
    log.info("ultralytics YOLO ready")
except ImportError:
    log.warning("ultralytics not found – run: pip install ultralytics")


# ═══════════════════════════════════════════════════════════════════════════════
# Inference settings (global, shared across all cameras)
# ═══════════════════════════════════════════════════════════════════════════════
class InferenceConfig:
    def __init__(self):
        self.task          = "detect"       # detect | segment | pose | obb | track | classify
        self.tracker       = "bytetrack"    # bytetrack | botsort
        self.max_det       = 300
        self.agnostic_nms  = False
        self.retina_masks  = False          # segment only
        self.topk          = 5              # classify only
        self.half          = False          # fp16 inference
        self.imgsz         = 640

    def to_dict(self) -> dict:
        return {
            "task":         self.task,
            "tracker":      self.tracker,
            "max_det":      self.max_det,
            "agnostic_nms": self.agnostic_nms,
            "retina_masks": self.retina_masks,
            "topk":         self.topk,
            "half":         self.half,
            "imgsz":        self.imgsz,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Model Manager
# ═══════════════════════════════════════════════════════════════════════════════
class ModelManager:
    def __init__(self):
        self.model      = None
        self.model_name = "none"
        self.loading    = False
        self._executor  = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yolo-load")

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
        self.classes    = None       # None = all classes

        # tracking state: keep tracker per camera
        self._tracker_results = None

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
                   classes, cfg: InferenceConfig,
                   cam_state: CamAIState) -> tuple[str, dict, float]:
    """Decode JPEG, run YOLO26s in the selected task mode.
    Returns (result_type, result_payload, inference_ms).
    """
    arr   = np.frombuffer(jpeg_bytes, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return "error", {}, 0.0

    h, w = frame.shape[:2]
    task = cfg.task
    t0   = time.perf_counter()

    common_kw = dict(
        conf=conf,
        iou=iou,
        classes=classes,
        verbose=False,
        half=cfg.half,
        imgsz=cfg.imgsz,
        max_det=cfg.max_det,
        agnostic_nms=cfg.agnostic_nms,
    )

    # ── CLASSIFY ────────────────────────────────────────────────────────────
    if task == "classify":
        results = model.predict(frame, verbose=False, half=cfg.half, imgsz=cfg.imgsz)
        ms = (time.perf_counter() - t0) * 1000.0
        payload = {"classifications": []}
        for r in results:
            if r.probs is not None:
                top_ids   = r.probs.top5
                top_confs = r.probs.top5conf.tolist()
                names     = r.names
                for cls_id, conf_v in zip(top_ids, top_confs):
                    payload["classifications"].append({
                        "class_id":   int(cls_id),
                        "label":      names.get(int(cls_id), f"cls-{cls_id}"),
                        "confidence": round(float(conf_v), 4),
                    })
                payload["classifications"] = payload["classifications"][:cfg.topk]
        return "classification", payload, ms

    # ── TRACK ───────────────────────────────────────────────────────────────
    if task == "track":
        results = model.track(
            frame,
            persist=True,
            tracker=f"{cfg.tracker}.yaml",
            **common_kw,
        )
        ms = (time.perf_counter() - t0) * 1000.0
        tracks = []
        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                track_id = int(box.id[0]) if box.id is not None else -1
                tracks.append({
                    "track_id":   track_id,
                    "label":      r.names[int(box.cls[0])],
                    "confidence": round(float(box.conf[0]), 3),
                    "class_id":   int(box.cls[0]),
                    "box":        [x1, y1, x2, y2],
                    "frame_w":    w,
                    "frame_h":    h,
                })
        return "tracks", {"tracks": tracks}, ms

    # ── POSE ────────────────────────────────────────────────────────────────
    if task == "pose":
        results = model.predict(frame, **common_kw)
        ms = (time.perf_counter() - t0) * 1000.0
        poses = []
        for r in results:
            if r.boxes is None:
                continue
            kps_data = r.keypoints  # may be None if model has no keypoints
            for i, box in enumerate(r.boxes):
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                pose_entry = {
                    "label":      r.names[int(box.cls[0])],
                    "confidence": round(float(box.conf[0]), 3),
                    "class_id":   int(box.cls[0]),
                    "box":        [x1, y1, x2, y2],
                    "frame_w":    w,
                    "frame_h":    h,
                    "keypoints":  [],
                }
                if kps_data is not None and i < len(kps_data.xy):
                    xy   = kps_data.xy[i].tolist()
                    conf = (kps_data.conf[i].tolist()
                            if kps_data.conf is not None else [1.0]*len(xy))
                    pose_entry["keypoints"] = [
                        {"x": round(p[0], 1), "y": round(p[1], 1), "conf": round(c, 3)}
                        for p, c in zip(xy, conf)
                    ]
                poses.append(pose_entry)
        return "poses", {"poses": poses}, ms

    # ── SEGMENT ─────────────────────────────────────────────────────────────
    if task == "segment":
        results = model.predict(frame, retina_masks=cfg.retina_masks, **common_kw)
        ms = (time.perf_counter() - t0) * 1000.0
        dets = []
        for r in results:
            if r.boxes is None:
                continue
            masks_xy = r.masks.xy if (r.masks is not None) else []
            for i, box in enumerate(r.boxes):
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                entry = {
                    "label":      r.names[int(box.cls[0])],
                    "confidence": round(float(box.conf[0]), 3),
                    "class_id":   int(box.cls[0]),
                    "box":        [x1, y1, x2, y2],
                    "frame_w":    w,
                    "frame_h":    h,
                    "mask":       [],
                }
                if i < len(masks_xy):
                    # downsample polygon to max 64 points for bandwidth
                    poly = masks_xy[i].tolist()
                    step = max(1, len(poly) // 64)
                    entry["mask"] = [[round(p[0], 1), round(p[1], 1)]
                                     for p in poly[::step]]
                dets.append(entry)
        return "detections", {"detections": dets, "task": "segment"}, ms

    # ── OBB ─────────────────────────────────────────────────────────────────
    if task == "obb":
        results = model.predict(frame, **common_kw)
        ms = (time.perf_counter() - t0) * 1000.0
        dets = []
        for r in results:
            if r.obb is None:
                continue
            for obb_box in r.obb:
                # xywhr: center_x, center_y, w, h, rotation_rad
                xywhr = obb_box.xywhr[0].tolist()
                xyxyxyxy = obb_box.xyxyxyxy[0].tolist()  # 4 corner points
                dets.append({
                    "label":      r.names[int(obb_box.cls[0])],
                    "confidence": round(float(obb_box.conf[0]), 3),
                    "class_id":   int(obb_box.cls[0]),
                    "xywhr":      [round(v, 2) for v in xywhr],
                    "corners":    [[round(p[0], 1), round(p[1], 1)] for p in xyxyxyxy],
                    "frame_w":    w,
                    "frame_h":    h,
                })
        return "detections", {"detections": dets, "task": "obb"}, ms

    # ── DETECT (default) ────────────────────────────────────────────────────
    results = model.predict(frame, **common_kw)
    ms = (time.perf_counter() - t0) * 1000.0
    dets = []
    for r in results:
        if r.boxes is None:
            continue
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
    return "detections", {"detections": dets, "task": "detect"}, ms


# ═══════════════════════════════════════════════════════════════════════════════
# AI Server
# ═══════════════════════════════════════════════════════════════════════════════
class AIServer:
    def __init__(self):
        self.model_mgr      = ModelManager()
        self.inf_cfg        = InferenceConfig()
        self.cam_states:    dict[int, CamAIState] = {}
        self.cameras_meta:  dict[int, dict]       = {}
        self.ws_clients:    set[web.WebSocketResponse] = set()
        self.streaming_ws   = None
        self.ai_on          = True
        self._executor      = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inf")

    # ── Streaming server connection ───────────────────────────────────────────
    async def run_streaming_connector(self):
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

        await self._broadcast_frame(cam_id, jpeg)

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
            model = self.model_mgr.model
            conf  = state.conf
            iou   = state.iou
            classes = state.classes
            cfg   = self.inf_cfg
            loop  = asyncio.get_event_loop()

            result_type, payload, ms = await loop.run_in_executor(
                self._executor,
                lambda: _run_inference(model, jpeg, conf, iou, classes, cfg, state),
            )

            state.last_dets  = payload.get("detections") or payload.get("tracks") or []
            state.last_ms    = ms
            state.frames_out += 1

            msg = {
                "type":    result_type,
                "cam_id":  cam_id,
                "inf_ms":  round(ms, 1),
                "model":   self.model_mgr.model_name,
                "task":    cfg.task,
            }
            msg.update(payload)
            await self._broadcast_json(msg)

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
            "inf_cfg":   self.inf_cfg.to_dict(),
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
            name = msg.get("model", "yolo26s")
            await self._broadcast_json({"type": "model_loading", "model": name})
            ok = await self.model_mgr.load(name)
            await self._broadcast_json({
                "type":    "model_loaded",
                "model":   self.model_mgr.model_name,
                "success": ok,
            })

        elif action == "unload_model":
            self.model_mgr.model      = None
            self.model_mgr.model_name = "none"
            await self._broadcast_json({"type": "model_loaded", "model": "none", "success": True})

        elif action == "set_task":
            task = msg.get("task", "detect")
            if task in VALID_TASKS:
                self.inf_cfg.task = task
                log.info(f"Task → {task}")
                await self._broadcast_json({"type": "task_changed", "task": task})

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
            cls_list = msg.get("classes")
            cid = msg.get("cam_id")
            targets = (
                [self.cam_states[cid]] if cid in self.cam_states
                else list(self.cam_states.values())
            )
            for s in targets:
                s.classes = cls_list

        elif action == "set_tracker":
            tracker = msg.get("tracker", "bytetrack")
            if tracker in ("bytetrack", "botsort"):
                self.inf_cfg.tracker = tracker

        elif action == "set_max_det":
            self.inf_cfg.max_det = int(msg.get("value", 300))

        elif action == "set_agnostic_nms":
            self.inf_cfg.agnostic_nms = bool(msg.get("value", False))

        elif action == "set_retina_masks":
            self.inf_cfg.retina_masks = bool(msg.get("value", False))

        elif action == "set_topk":
            self.inf_cfg.topk = int(msg.get("value", 5))

        elif action == "set_half":
            self.inf_cfg.half = bool(msg.get("value", False))

        elif action == "set_imgsz":
            v = int(msg.get("value", 640))
            if v in (320, 416, 480, 512, 608, 640, 768, 1024, 1280):
                self.inf_cfg.imgsz = v

        elif action == "get_stats":
            stats = {str(cid): s.to_dict() for cid, s in self.cam_states.items()}
            await ws.send_str(json.dumps({
                "type":    "ai_stats",
                "cameras": stats,
                "inf_cfg": self.inf_cfg.to_dict(),
            }))

    async def handle_api_cameras(self, request: web.Request) -> web.Response:
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
            "cameras": {str(cid): s.to_dict() for cid, s in self.cam_states.items()},
            "inf_cfg": self.inf_cfg.to_dict(),
        })

    async def handle_api_inf_cfg(self, request: web.Request) -> web.Response:
        return web.json_response(self.inf_cfg.to_dict())


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
    app.router.add_get("/api/inf_cfg",    server.handle_api_inf_cfg)

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