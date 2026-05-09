"""Live MQTT dashboard for Bambu Lab printers on the LAN.

Each printer keeps a persistent TLS MQTT connection on port 8883 (user
`bblp`, password = LAN access code, self-signed cert so verification is
disabled). We subscribe to `device/<serial>/report` and merge incoming
deltas into a per-printer state dict. A `pushall` request is sent on
connect so the first snapshot arrives immediately rather than waiting
for the next delta.

Mounted into app.py via include_router(), shares the FastAPI lifespan.
The kiosk page at /dashboard opens a WebSocket to /ws/printers and
re-renders tiles as state changes.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import subprocess
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import imageio_ffmpeg
import paho.mqtt.client as mqtt
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

# Resolved once at import — get_ffmpeg_exe() does filesystem probes and we
# call _grab_snapshot once per minute per printer. No reason to re-probe.
_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

BASE_DIR = Path(__file__).resolve().parent
PRINTERS_JSON = BASE_DIR / "printers.json"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
SNAPSHOT_INTERVAL_SEC = 60
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _load_printers() -> list[dict]:
    if not PRINTERS_JSON.exists():
        return []
    try:
        data = json.loads(PRINTERS_JSON.read_text())
    except json.JSONDecodeError:
        return []
    return data.get("printers", []) or []


def _grab_snapshot(ip: str, access_code: str, timeout: float = 12.0) -> bytes:
    """Pull a single JPEG frame from the printer's RTSPS camera stream.

    LAN Mode Liveview on current X1C firmware exposes RTSPS at port 322 with
    H264 video. We shell out to the ffmpeg bundled by imageio-ffmpeg to do
    the RTSP setup + TLS handshake + H264 decode + JPEG encode in one shot
    (~1-2s end-to-end, dominated by RTSP handshake). The legacy port-6000
    custom-protocol path stays gated behind full LAN-Only Mode, which would
    break Handy, so we don't fall back to it.

    Auth credentials are URL-embedded; the printer's TLS cert is self-signed
    but ffmpeg's RTSPS client doesn't verify it by default."""
    url = (
        f"rtsps://bblp:{quote(access_code, safe='')}"
        f"@{ip}:322/streaming/live/1"
    )
    cmd = [
        _FFMPEG,
        "-loglevel", "error",
        "-rtsp_transport", "tcp",   # UDP over TLS isn't a thing
        "-i", url,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        # Suppress the brief console flash on Windows when ffmpeg launches.
        creationflags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout, creationflags=creationflags,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode(errors="replace").strip()[-300:]
        raise IOError(f"ffmpeg rc={proc.returncode}: {err or 'empty output'}")
    if proc.stdout[:3] != b"\xff\xd8\xff":
        raise IOError("ffmpeg returned non-JPEG output")
    return proc.stdout


def _deep_merge(dst: dict, src: dict) -> None:
    """In-place recursive merge — Bambu sends partial reports, we keep the
    cumulative picture. Lists overwrite (AMS tray arrays are sent whole when
    they change, so element-wise merge would corrupt the model)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


class PrinterClient:
    """One MQTT connection per printer. paho's loop_start() runs the
    network loop on a background thread; callbacks fire from that thread,
    so the main event loop is reached via call_soon_threadsafe()."""

    def __init__(self, cfg: dict, hub: "DashboardHub") -> None:
        self.cfg = cfg
        self.hub = hub
        self.id: str = cfg["id"]
        self.serial: str = cfg["serial"]
        self.report_topic = f"device/{self.serial}/report"
        self.request_topic = f"device/{self.serial}/request"
        self.state: dict = {
            "id": self.id,
            "label": cfg.get("label", self.id),
            "model": cfg.get("model", ""),
            "online": False,
            "last_error": None,
            # Wall-clock when we last lost the connection — used by the UI
            # to suppress error flashes during the normal reconnect window
            # (Bambu's broker enforces single-client-per-credentials, so
            # opening Bambu Studio briefly kicks the dashboard, paho
            # reconnects in 2-30s, no real user action needed).
            "disconnected_at": None,
            "snapshot_ok": False,
            "snapshot_error": None,
            # Filename eavesdropping: SD-card prints never publish a name in
            # the report topic, but Send-to-Printer / Handy / cloud-initiated
            # prints publish a "project_file" command on the request topic
            # that carries it. We latch the most recent value and tie it to
            # the printer's task_id so a new print clears the old name.
            "captured_filename": None,
            "captured_task_id": None,
            "report": {},
        }
        self._client: mqtt.Client | None = None
        self._snapshot_task: asyncio.Task | None = None

    def start(self) -> None:
        c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambucli-dash-{self.id}",
        )
        c.username_pw_set("bblp", self.cfg["access_code"])
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(ctx)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=2, max_delay=30)
        self._client = c
        try:
            c.connect_async(self.cfg["ip"], 8883, keepalive=60)
            c.loop_start()
        except Exception as e:
            self._set_error(f"connect failed: {e}")

    def stop(self) -> None:
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
            self._snapshot_task = None
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    async def _snapshot_loop(self) -> None:
        """Pull one camera frame every SNAPSHOT_INTERVAL_SEC. Errors don't
        kill the loop — the camera disappears whenever a print finishes or
        the printer reboots, and we want the next attempt to recover."""
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        out_path = SNAPSHOT_DIR / f"{self.id}.jpg"
        tmp_path = out_path.with_suffix(".jpg.tmp")
        while True:
            try:
                jpg = await asyncio.to_thread(
                    _grab_snapshot, self.cfg["ip"], self.cfg["access_code"],
                )
                tmp_path.write_bytes(jpg)
                tmp_path.replace(out_path)
                self.state["snapshot_ok"] = True
                self.state["snapshot_error"] = None
                # Monotonic counter — the dashboard uses it as the <img>
                # cache buster, so the browser only re-fetches on a real
                # new frame, not on every MQTT temp tick.
                self.state["snapshot_seq"] = self.state.get("snapshot_seq", 0) + 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state["snapshot_error"] = str(e)[:200]
                # Don't flip snapshot_ok off — the previous JPEG is still on
                # disk and worth showing in the UI even if a refresh failed.
            self.hub.notify(self.id)
            try:
                await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise

    def _on_connect(self, client: mqtt.Client, _u, _f, reason_code, _props=None) -> None:
        if reason_code != 0:
            self._set_error(f"connect rc={reason_code}")
            return
        client.subscribe(self.report_topic, qos=0)
        # Also eavesdrop on commands sent TO the printer (Bambu Studio's
        # Send-to-Printer, Handy, anything else on the LAN) so we can capture
        # the filename in "project_file" commands. Bambu's broker is a plain
        # mosquitto with no ACLs — anyone holding the access code can read
        # both topics.
        client.subscribe(self.request_topic, qos=0)
        # Ask the printer to push its full current state right now; otherwise
        # we'd only receive deltas as values change and the dashboard would
        # be blank until the next slice/temp tick.
        client.publish(
            self.request_topic,
            json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
            qos=0,
        )
        self.state["online"] = True
        self.state["last_error"] = None
        self.state["disconnected_at"] = None
        self.hub.notify(self.id)

    def _on_disconnect(self, _c, _u, _f, reason_code, _props=None) -> None:
        import time as _time
        self.state["online"] = False
        # Record when the disconnect happened so the UI can show
        # "reconnecting..." instead of the raw error during the brief
        # window before paho reconnects.
        self.state["disconnected_at"] = _time.time()
        if reason_code:
            self._set_error(f"disconnected rc={reason_code}")
        self.hub.notify(self.id)

    def _on_message(self, _c, _u, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if msg.topic == self.request_topic:
            # Eavesdropped command. We only care about project_file (start a
            # new print) — pushall, gcode_line, light commands etc. all
            # ignore. The "subtask_name" field carries the original 3MF
            # filename even when the printer's own steady-state reports leave
            # it blank.
            print_block = payload.get("print") or {}
            if print_block.get("command") == "project_file":
                name = (print_block.get("subtask_name") or "").strip()
                if name:
                    self.state["captured_filename"] = name
                    # task_id is assigned when the printer accepts the job;
                    # we'll bind to whatever task_id appears in the next
                    # report (None is the "unbound" sentinel).
                    self.state["captured_task_id"] = None
                    self.hub.notify(self.id)
            return

        # Reports look like {"print": {...}} or {"info": {...}}; both go
        # straight into self.state["report"] under their top-level key so
        # the UI can read state["report"]["print"] uniformly.
        _deep_merge(self.state["report"], payload)
        self._update_filename_capture()
        self.state["online"] = True
        self.hub.notify(self.id)

    def _update_filename_capture(self) -> None:
        """Reconcile captured_filename against the latest report.

        Three cases:
          - Report has a non-empty subtask_name: trust it, bind to current task_id.
          - Capture is unbound (just received a project_file command): bind to
            whatever task_id the printer is now reporting.
          - Task_id changed and we never got a fresh name for it: drop the
            stale capture so we don't mislabel the new print."""
        p = (self.state["report"].get("print") or {})
        current_tid = p.get("task_id")
        sn = (p.get("subtask_name") or "").strip()

        if sn:
            self.state["captured_filename"] = sn
            self.state["captured_task_id"] = current_tid
            return

        if (self.state.get("captured_filename")
                and self.state.get("captured_task_id") is None
                and current_tid):
            self.state["captured_task_id"] = current_tid
            return

        if (current_tid
                and self.state.get("captured_task_id") not in (None, current_tid)):
            self.state["captured_filename"] = None
            self.state["captured_task_id"] = None

    def _set_error(self, msg: str) -> None:
        self.state["online"] = False
        self.state["last_error"] = msg
        self.hub.notify(self.id)


class DashboardHub:
    """Owns the printer clients + the set of subscribed WebSockets.
    Snapshot reads are guarded by a lock because MQTT callbacks mutate
    state from a background thread while WebSockets read from the asyncio
    loop."""

    def __init__(self) -> None:
        self.clients: dict[str, PrinterClient] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        for cfg in _load_printers():
            client = PrinterClient(cfg, self)
            self.clients[client.id] = client
            client.start()
            client._snapshot_task = loop.create_task(client._snapshot_loop())

    def stop(self) -> None:
        for c in self.clients.values():
            c.stop()
        self.clients.clear()

    def notify(self, printer_id: str) -> None:
        if self.loop is None:
            return
        snap = self.snapshot_one(printer_id)
        if snap is None:
            return
        # Hop from the paho thread to the asyncio loop so we can safely
        # touch the queues. drop_oldest semantics: if a client is slow, we
        # discard rather than block — the dashboard only cares about the
        # latest state.
        self.loop.call_soon_threadsafe(self._fanout, snap)

    def _fanout(self, snap: dict) -> None:
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            return [deepcopy(c.state) for c in self.clients.values()]

    def snapshot_one(self, printer_id: str) -> dict | None:
        with self._lock:
            c = self.clients.get(printer_id)
            return deepcopy(c.state) if c else None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


hub = DashboardHub()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {
        "printers": hub.snapshot_all(),
    })


@router.get("/api/printers")
async def api_printers() -> dict:
    return {"printers": hub.snapshot_all()}


@router.get("/snapshot/{printer_id}.jpg")
async def snapshot(printer_id: str) -> FileResponse:
    # Strict id whitelist — printer_id is interpolated into a filename so
    # we refuse anything that could traverse out of SNAPSHOT_DIR.
    if not _ID_RE.match(printer_id):
        raise HTTPException(400, "bad printer id")
    path = SNAPSHOT_DIR / f"{printer_id}.jpg"
    if not path.exists():
        raise HTTPException(404, "no snapshot yet")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@router.websocket("/ws/printers")
async def ws_printers(ws: WebSocket) -> None:
    await ws.accept()
    # Send an initial snapshot of every printer so the page renders before
    # the first MQTT delta arrives.
    for snap in hub.snapshot_all():
        await ws.send_json(snap)

    q = hub.subscribe()
    try:
        while True:
            snap = await q.get()
            await ws.send_json(snap)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(q)
