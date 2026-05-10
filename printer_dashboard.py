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
import ftplib
import json
import re
import socket
import ssl
import subprocess
import threading
import time as _time_mod
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import imageio_ffmpeg
import paho.mqtt.client as mqtt
from fastapi import APIRouter, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

import jobs_db

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


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTPS in implicit-TLS mode (port 990, TLS from the first byte).

    Bambu printers expose FTPS implicitly — there's no plaintext AUTH TLS
    handshake. Python's stdlib only ships explicit-TLS support, so we wrap
    the control socket in SSL on connect. Self-signed cert; verification
    is disabled by the caller (same posture as the MQTT and RTSPS paths).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def makepasv(self):
        # Bambu printers report their internal LAN address in the PASV
        # response. When the printer is on a different subnet (or the
        # firmware is just buggy about this), the IP it returns isn't
        # routable from the host, so the data-channel connect hangs
        # until the socket timeout fires. Force the data channel to use
        # the same IP we already reached the control channel on.
        _host, port = super().makepasv()
        return self.host, port

    def ntransfercmd(self, cmd, rest=None):
        # Bambu's FTPS server requires the data-channel TLS to resume
        # the session negotiated on the control channel. Python's
        # default ftplib opens a fresh TLS handshake on the data port,
        # which Bambu refuses with an immediate EOF (looks like
        # "EOF occurred in violation of protocol (_ssl.c:2427)" to the
        # client). Reusing the control socket's SSLSession satisfies
        # the printer.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = getattr(self.sock, "session", None)
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=session,
            )
        return conn, size

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        # Same close_notify-skip pattern as storbinary below — without
        # this the FTPS download hangs at end-of-transfer waiting for
        # a TLS shutdown that Bambu never sends.
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while 1:
                data = conn.recv(blocksize)
                if not data:
                    break
                callback(data)
        return self.voidresp()

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        # Verbatim copy of stdlib FTP_TLS.storbinary minus the trailing
        # `conn.unwrap()` call. Bambu's FTPS server (P1S firmware in
        # particular) sends `226 Transfer complete` on the control
        # channel as soon as it sees the data socket close, but never
        # sends a TLS close_notify on the data channel. Python's
        # default unwrap() blocks waiting for that close_notify until
        # the socket timeout fires — the upload appears to "hang" even
        # though every byte was already received. Letting the socket
        # close on context-manager exit (no unwrap) is what every
        # working community Bambu FTPS client does.
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while 1:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback is not None:
                    callback(buf)
        return self.voidresp()


def _ftps_upload(ip: str, access_code: str, local_path: Path, remote_name: str,
                 timeout: float = 120.0) -> None:
    """Upload a single file to a Bambu printer via implicit FTPS.

    Lands at the FTP root, which on X1C maps to the SD card and on P1S to
    internal storage — either way the firmware's project_file command can
    address the file by name. Raises on any FTP error so the caller can
    surface a useful message.

    The SSL context drops to SECLEVEL=0 because Bambu's embedded FTPS
    server uses weak crypto (small DH params / legacy ciphers) that
    modern OpenSSL builds refuse by default — without this, the TLS
    handshake closes mid-stream with "EOF occurred in violation of
    protocol (_ssl.c:...)". cert verification is already off (self-signed
    cert), so loosening the cipher floor doesn't widen the trust surface
    further than it already is.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    except ssl.SSLError:
        # Older Python / OpenSSL builds parse SECLEVEL differently —
        # fall back to "everything we can negotiate".
        ctx.set_ciphers("ALL")
    # Pin to TLS 1.2 so the data-channel session-reuse trick actually works.
    # P1S firmware negotiates TLS 1.3 on the control channel by default in
    # Python 3.12+, but Python's TLS 1.3 ticket isn't available at the
    # moment ntransfercmd() needs it — `self.sock.session` reads back as
    # None and the data-channel TLS opens a fresh handshake that the
    # printer ignores, hanging the STOR mid-transfer. TLS 1.2 emits a
    # session ticket immediately on handshake completion, so resumption
    # works and the data channel comes up. (X1C tolerates either; P1S
    # needs this.)
    try:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    ftps = _ImplicitFTP_TLS(timeout=timeout)
    ftps.context = ctx
    # Verbose FTP conversation goes to stderr (uvicorn.err.log) so we can
    # see exactly which command times out on the slower P1S firmware.
    ftps.set_debuglevel(2)
    try:
        ftps.connect(ip, 990)
        ftps.login("bblp", access_code)
        # Encrypt the data channel too — the printer enforces this and will
        # reject a plain-text STOR.
        ftps.prot_p()
        ftps.set_pasv(True)
        with local_path.open("rb") as f:
            ftps.storbinary(f"STOR {remote_name}", f)
    finally:
        try:
            ftps.quit()
        except Exception:
            try:
                ftps.close()
            except Exception:
                pass


def _ftps_download(ip: str, access_code: str, remote_name: str, local_path: Path,
                   timeout: float = 60.0) -> None:
    """Pull a file off the printer's FTP root into a local path. Mirror
    image of _ftps_upload — same SECLEVEL=0 + TLS-1.2 + session-reuse +
    skip-unwrap workarounds. Used by the grams auto-fetch loop to grab
    SD-card-print 3MFs that didn't go through our intake so we can
    extract the slicer's predicted weight.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    except ssl.SSLError:
        ctx.set_ciphers("ALL")
    try:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    ftps = _ImplicitFTP_TLS(timeout=timeout)
    ftps.context = ctx
    try:
        ftps.connect(ip, 990)
        ftps.login("bblp", access_code)
        ftps.prot_p()
        ftps.set_pasv(True)
        with local_path.open("wb") as f:
            ftps.retrbinary(f"RETR {remote_name}", f.write)
    finally:
        try:
            ftps.quit()
        except Exception:
            try:
                ftps.close()
            except Exception:
                pass


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
        # P1S firmware (01.10 confirmed) kicks any client that subscribes
        # to /request — the topic for commands sent TO the printer — with
        # rc=Unspecified Error within ~50ms, then loops forever as paho
        # reconnects. X1C firmware allows it. Subscription to /request is
        # only needed for filename eavesdropping on prints initiated from
        # Studio / Handy / cloud; on P1S we skip it and rely on what the
        # printer voluntarily publishes in /report (which DOES include
        # subtask_name for non-SD prints there).
        self.eavesdrop_request: bool = cfg.get(
            "eavesdrop_request",
            not str(cfg.get("model", "")).upper().startswith("P1"),
        )
        # Per-printer toggle for the RTSPS snapshot loop. Default on,
        # but printers.json can set "webcam_enabled": false to skip it
        # — useful for cameras with quirky/unstable streams (e.g. P1S
        # over LAN-only at the moment) where the broken-frame errors
        # add noise without telling staff anything new.
        self.webcam_enabled: bool = bool(cfg.get("webcam_enabled", True))
        self.state: dict = {
            "id": self.id,
            "label": cfg.get("label", self.id),
            "model": cfg.get("model", ""),
            "online": False,
            "webcam_enabled": bool(cfg.get("webcam_enabled", True)),
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
        self._reconnect_task: asyncio.Task | None = None

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
        for task_attr in ("_snapshot_task", "_reconnect_task"):
            t = getattr(self, task_attr)
            if t is not None:
                t.cancel()
                setattr(self, task_attr, None)
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    async def _reconnect_watchdog(self) -> None:
        """Force a reconnect when paho's own retry loop won't.

        paho's `reconnect_delay_set` only handles network-level failures.
        For broker-initiated DISCONNECTs (Bambu kicking our session when
        Bambu Studio claims the single MQTT slot, firmware reboots,
        etc.) paho gives up and the connection sits dead. This task
        polls the cached state every 30s and forces `client.reconnect()`
        whenever we've been offline too long. The reconnect call is
        thread-safe in paho v2 and returns immediately — actual TLS
        handshake happens on the loop thread."""
        import time as _time
        STUCK_AFTER_SEC = 45
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            if self._client is None or self.state.get("online"):
                continue
            disc_at = self.state.get("disconnected_at")
            if not disc_at or _time.time() - disc_at < STUCK_AFTER_SEC:
                continue
            try:
                self._client.reconnect()
            except Exception as e:
                # Not fatal — we'll try again on the next tick. Log via
                # state so the UI shows the most recent attempt's error
                # if we stay stuck for a long time.
                self._set_error(f"reconnect: {e}"[:200])

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
        # the filename in "project_file" commands. X1C firmware allows this;
        # P1S firmware refuses and disconnects us — see eavesdrop_request
        # default in __init__.
        if self.eavesdrop_request:
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
        self._log_job_observation()
        self.state["online"] = True
        self.hub.notify(self.id)

    def _log_job_observation(self) -> None:
        """Shadow the live job state to printqueue/jobs.db so we have a
        durable record of every print the dashboard ever saw — including
        ones started outside the web intake (Bambu Studio sends, SD-card
        prints from the touchscreen). jobs_db.record_observation handles
        dedupe + transition tracking; we just feed it whatever the
        latest report says, plus the captured_filename fallback for
        firmwares that drop subtask_name from steady-state reports."""
        p = (self.state.get("report") or {}).get("print") or {}
        try:
            jobs_db.record_observation(
                self.id,
                task_id=p.get("task_id"),
                subtask_name=(p.get("subtask_name") or "").strip() or None,
                gcode_state=p.get("gcode_state"),
                print_error=p.get("print_error"),
                prediction_seconds=p.get("mc_print_total_time") or None,
                total_layers=p.get("total_layer_num") or None,
                captured_filename=self.state.get("captured_filename"),
                mc_percent=p.get("mc_percent"),
            )
        except Exception:
            # Logging must not break the dashboard's MQTT loop. The
            # row simply won't exist; the next report will retry.
            pass

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

    def claim_external_spool(
        self,
        *,
        tray_type: str = "PLA",
        tray_color: str = "FFFFFFFFFF"[:8],
        tray_info_idx: str = "GFL99",
        nozzle_temp_min: int = 190,
        nozzle_temp_max: int = 240,
    ) -> None:
        """Tell the printer "the external spool is loaded with X" via MQTT.

        Needed for the P1S without AMS: its vt_tray reports
        tray_color='00000000' (unidentified) when the user just spooled
        plain filament on the back, and the firmware then refuses every
        project_file with HMS 0500-0500-0001-0007 ("filament type
        mismatch"). This command is what Bambu Studio's "Print without
        AMS" path sends internally — ams_id=255 + tray_id=254 addresses
        the external spool, tray_info_idx="GFL99" is the generic PLA
        profile so the firmware doesn't require a brand-specific match.
        """
        payload = {
            "print": {
                "sequence_id": str(int(_time_mod.time())),
                "command": "ams_filament_setting",
                "ams_id": 255,
                "tray_id": 254,
                "tray_info_idx": tray_info_idx,
                "tray_color": tray_color,
                "tray_type": tray_type,
                "nozzle_temp_min": nozzle_temp_min,
                "nozzle_temp_max": nozzle_temp_max,
                "setting_id": "0",
            }
        }
        if self._client is None:
            return
        self._client.publish(self.request_topic, json.dumps(payload), qos=0)

    def publish_project_file(
        self,
        remote_filename: str,
        *,
        use_ams: bool = False,
        ams_mapping: list[int] | None = None,
    ) -> None:
        """Tell the printer to start printing a file already uploaded to its
        FTP root. Reuses the dashboard's persistent MQTT client to avoid
        Bambu's single-client-per-credentials kick — a one-shot publish
        with the same credentials would knock the dashboard offline.

        `ams_mapping` positions follow the file's filament index (0-based)
        and the values are AMS slot indices (0-3 for the first AMS unit,
        4-7 for the second, etc.). The sentinel value 255 (0xff) means
        "external spool" (the back tray, vt_tray) — that's the firmware's
        own magic number, not ours. When `use_ams=False` and the mapping
        is omitted, the printer falls back to whatever filament is loaded
        on the external spool.

        The payload is the project_file command shape that Bambu Studio /
        Handy send when invoking Send-to-Printer; the field set is the
        cross-printer subset confirmed working on X1C and P1S in the
        OpenBambuAPI / pybambu community ports.
        """
        if self._client is None or not self.state.get("online"):
            raise RuntimeError(f"printer '{self.id}' is offline")
        # ams_mapping is sent as a JSON-string-encoded array, not a real
        # JSON array — Bambu's parser is picky about this.
        mapping_str = json.dumps(ams_mapping) if ams_mapping else ""
        # `file:///mnt/sdcard/<name>` is the URL form Bambu Studio itself
        # publishes for LAN-mode "Send to Printer". The FTP root we
        # uploaded to maps to /mnt/sdcard/ on both X1C and P1S firmware.
        # The `ftp:///<name>` form is also documented in the wild but
        # P1S firmware silently ignores it — the printer latches the
        # subtask_name but the print never starts (gcode_state stays
        # FINISH, gcode_file stays empty).
        payload = {
            "print": {
                "sequence_id": str(int(_time_mod.time())),
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "subtask_name": remote_filename.rsplit(".3mf", 1)[0],
                "url": f"file:///mnt/sdcard/{remote_filename}",
                "md5": "",
                "bed_type": "auto",
                "bed_levelling": True,
                "flow_cali": False,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": bool(use_ams),
                "timelapse": False,
                "ams_mapping": mapping_str,
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
            },
            "user_id": "1",
        }
        # QoS 0 to match the rest of the dashboard's traffic. Bambu's broker
        # doesn't reliably PUBACK at QoS 1 — the dashboard's pushall on
        # connect already uses qos=0 for the same reason. paho's
        # is_published() returns True for QoS-0 as soon as the message is
        # handed to the network loop, so the wait_for_publish guard below
        # is a sanity check rather than a real ack.
        info = self._client.publish(
            self.request_topic, json.dumps(payload), qos=0,
        )
        try:
            info.wait_for_publish(timeout=5.0)
        except (ValueError, RuntimeError):
            pass
        if not info.is_published():
            raise RuntimeError("MQTT publish did not complete within 5s")


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
        self._grams_fetch_task: asyncio.Task | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        for cfg in _load_printers():
            client = PrinterClient(cfg, self)
            self.clients[client.id] = client
            client.start()
            if client.webcam_enabled:
                client._snapshot_task = loop.create_task(client._snapshot_loop())
            client._reconnect_task = loop.create_task(client._reconnect_watchdog())
        self._grams_fetch_task = loop.create_task(self._grams_fetch_loop())

    def stop(self) -> None:
        if self._grams_fetch_task is not None:
            self._grams_fetch_task.cancel()
            self._grams_fetch_task = None
        for c in self.clients.values():
            c.stop()
        self.clients.clear()

    async def _grams_fetch_loop(self) -> None:
        """Resolve missing predicted_grams for jobs we couldn't match
        locally (SD-card prints, Studio sends from another machine, etc.)
        by FTP-downloading the 3MF from the printer that ran it and
        running it through inspect_3mf.

        Runs every 60s, processes a small batch per tick. The fetch
        itself happens on a worker thread so the FTPS handshake doesn't
        starve the dashboard's MQTT loop. Bambu's storage rotates after
        a while, so the candidate query already filters out anything
        last seen more than 24h ago — older missing-grams rows just
        stay null."""
        # Slow first cycle so the dashboard finishes its initial
        # connect+pushall before we start hammering the FTP port.
        await asyncio.sleep(45)
        while True:
            try:
                await self._grams_fetch_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a single tick kill the loop; bad rows are
                # individually marked "failed" inside the tick.
                pass
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

    async def _grams_fetch_tick(self) -> None:
        candidates = jobs_db.find_grams_fetch_candidates(limit=3)
        for cand in candidates:
            client = self.clients.get(cand["printer_id"])
            if client is None:
                # No live client for this printer — can't ever fetch,
                # bypass the retry budget and mark terminal.
                jobs_db.set_grams_fetch_state(cand["id"], "failed")
                continue
            try:
                grams = await asyncio.to_thread(
                    self._fetch_grams_for, client.cfg, cand["filename"],
                )
            except Exception:
                # Network / FTP error. Counts as one of the retry
                # attempts; the row stays 'pending' until we exhaust
                # MAX_FETCH_ATTEMPTS, then promotes itself to 'failed'.
                jobs_db.record_grams_fetch_failure(cand["id"])
                continue
            if grams is None or grams <= 0:
                jobs_db.record_grams_fetch_failure(cand["id"])
                continue
            # If the print is already terminal, scale by the latched
            # final percent so actual_grams gets the same treatment as
            # the locally-resolved path. In-flight rows defer
            # actual_grams to the next observation that flips outcome.
            actual = None
            if cand.get("outcome") == "finished":
                actual = round(grams, 1)
            elif cand.get("outcome") in ("failed", "cancelled"):
                pct = cand.get("last_percent")
                if pct is not None:
                    actual = round(grams * (pct / 100.0), 1)
            jobs_db.set_grams_fetched(cand["id"], grams, actual_grams=actual)

    @staticmethod
    def _fetch_grams_for(printer_cfg: dict, filename: str) -> float | None:
        """Worker-thread half of _grams_fetch_tick. Tries a couple of
        common naming variations Bambu firmware writes ('foo' vs
        'foo.3mf' vs 'foo.gcode.3mf') before giving up."""
        import os as _os
        import tempfile
        from slice_order import inspect_3mf  # local: avoids circular import

        # Bambu firmware writes prints as either '<name>.3mf' or
        # '<name>.gcode.3mf' depending on whether they came from
        # Studio's "Send to Printer" (project file) or a slicer-output
        # gcode bundle. The subtask_name we record sometimes has one
        # form, the actual SD-card file has the other — try every
        # combination so a 550 on the first attempt isn't fatal.
        candidates: list[str] = [filename]
        basename = filename
        for suffix in (".gcode.3mf", ".3mf"):
            if basename.endswith(suffix):
                basename = basename[: -len(suffix)]
                break
        for variant in (f"{basename}.gcode.3mf", f"{basename}.3mf", basename):
            if variant and variant not in candidates:
                candidates.append(variant)

        last_err: Exception | None = None
        for name in candidates:
            fd, tmp = tempfile.mkstemp(suffix=".3mf")
            _os.close(fd)
            tmp_path = Path(tmp)
            try:
                try:
                    _ftps_download(
                        printer_cfg["ip"], printer_cfg["access_code"],
                        name, tmp_path,
                    )
                except Exception as e:
                    last_err = e
                    continue
                ins = inspect_3mf(tmp_path)
                grams = round(
                    sum(p.get("weight_grams", 0.0) for p in ins.get("plates", [])),
                    1,
                )
                if grams > 0:
                    return float(grams)
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        if last_err:
            raise last_err
        return None

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

    def list_printers(self) -> list[dict]:
        """Compact catalogue used by the result page to render send-to-printer
        controls. Returns id/label/model + online state + the printer's live
        AMS / external-spool palette so the UI can populate per-filament slot
        pickers without a follow-up query."""
        with self._lock:
            out = []
            for c in self.clients.values():
                p = (c.state.get("report") or {}).get("print") or {}
                ams_units: list[dict] = []
                ams_root = (p.get("ams") or {}).get("ams") or []
                for unit in ams_root:
                    if not isinstance(unit, dict):
                        continue
                    slots = []
                    for tray in unit.get("tray") or []:
                        if not isinstance(tray, dict):
                            continue
                        slots.append({
                            "id": str(tray.get("id", "")),
                            # tray_color is RGBA hex (8 chars) when set; empty
                            # when the slot is unloaded.
                            "color_hex": (tray.get("tray_color") or "").strip(),
                            "type": (tray.get("tray_type") or "").strip(),
                        })
                    ams_units.append({
                        "id": str(unit.get("id", "")),
                        "slots": slots,
                    })
                vt = p.get("vt_tray") or {}
                external_spool = {
                    "color_hex": (vt.get("tray_color") or "").strip(),
                    "type": (vt.get("tray_type") or "").strip(),
                }
                out.append({
                    "id": c.id,
                    "label": c.cfg.get("label", c.id),
                    "model": (c.cfg.get("model") or "").upper(),
                    "online": bool(c.state.get("online")),
                    "ams_units": ams_units,
                    "external_spool": external_spool,
                })
            return out

    async def send_to_printer(self, printer_id: str, local_path: Path,
                              remote_name: str | None = None,
                              *,
                              use_ams: bool = False,
                              ams_mapping: list[int] | None = None) -> dict:
        """Upload a 3MF over FTPS and start the print via MQTT. Both legs run
        on a worker thread; the FTP transfer dominates the wall time (a
        ~30 MB 3MF takes ~3-5s over Wi-Fi)."""
        client = self.clients.get(printer_id)
        if client is None:
            raise HTTPException(404, f"unknown printer '{printer_id}'")
        if not local_path.exists() or not local_path.is_file():
            raise HTTPException(404, f"file not found: {local_path.name}")
        remote = remote_name or local_path.name

        # Decide whether to pre-claim the external spool. P1S without
        # AMS shows up here as ams_units=[] AND vt_tray.tray_color='00000000'
        # when filament has been spooled on the back without telling
        # the printer what it is. In that state every project_file
        # gets rejected with HMS 0500-0500-0001-0007 — the only
        # workaround is to MQTT-publish ams_filament_setting first so
        # the firmware has *something* to compare against. We only do
        # it for use_ams=False (external-spool prints) and only when
        # the spool currently reports "unidentified", to avoid
        # clobbering a setting the user actually configured.
        report = (client.state.get("report") or {}).get("print") or {}
        vt = report.get("vt_tray") or {}
        spool_unidentified = (vt.get("tray_color") or "").strip("0") == ""
        will_claim_spool = (not use_ams) and spool_unidentified

        def _work() -> None:
            _ftps_upload(client.cfg["ip"], client.cfg["access_code"],
                         local_path, remote)
            if will_claim_spool:
                client.claim_external_spool()
                # Give the firmware a moment to ingest the new tray
                # info before the project_file pre-print check fires.
                _time_mod.sleep(0.5)
            client.publish_project_file(
                remote, use_ams=use_ams, ams_mapping=ams_mapping,
            )

        try:
            await asyncio.to_thread(_work)
        except ftplib.all_errors as e:
            raise HTTPException(502, f"FTPS upload failed: {e}")
        except (socket.timeout, ConnectionError, OSError) as e:
            raise HTTPException(502, f"network error talking to printer: {e}")
        except RuntimeError as e:
            raise HTTPException(502, str(e))

        return {
            "ok": True,
            "printer_id": printer_id,
            "label": client.cfg.get("label", printer_id),
            "filename": remote,
        }


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


@router.get("/api/printers/list")
async def api_printers_list() -> dict:
    """Lightweight printer catalogue (id, label, model, online) for the
    result page's send-to-printer controls. Distinct from /api/printers,
    which dumps the full live state for the dashboard."""
    return {"printers": hub.list_printers()}


@router.post("/api/printers/send")
async def api_printers_send(
    printer_id: str = Form(...),
    filename: str = Form(...),
    use_ams: str = Form("false"),
    # JSON-encoded array of slot indices, positionally matching the file's
    # filaments (e.g. `[0,2]` = filament 1 → AMS slot 0, filament 2 → slot 2).
    # The sentinel 255 means "external spool" (vt_tray).
    ams_mapping: str = Form(""),
) -> dict:
    """FTPS-upload the named 3MF (under printqueue/work) to the requested
    printer and immediately publish a project_file MQTT command so the
    print starts without staff touching the printer's screen."""
    if not _ID_RE.match(printer_id):
        raise HTTPException(400, "bad printer id")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "bad filename")
    if not filename.lower().endswith(".3mf"):
        raise HTTPException(400, "expected a .3mf file")
    work_dir = BASE_DIR / "printqueue" / "work"
    local = (work_dir / filename).resolve()
    if not str(local).startswith(str(work_dir.resolve())):
        raise HTTPException(400, "path escapes workdir")

    mapping: list[int] | None = None
    if ams_mapping.strip():
        try:
            parsed = json.loads(ams_mapping)
        except json.JSONDecodeError:
            raise HTTPException(400, "ams_mapping must be a JSON array")
        if not isinstance(parsed, list) or not all(isinstance(x, int) for x in parsed):
            raise HTTPException(400, "ams_mapping must be a JSON array of ints")
        mapping = parsed

    return await hub.send_to_printer(
        printer_id, local,
        use_ams=str(use_ams).lower() in ("1", "true", "yes", "on"),
        ams_mapping=mapping,
    )


@router.get("/api/jobs")
async def api_jobs(
    limit: int = 200,
    printer_id: str | None = None,
    capture_source: str | None = None,
) -> dict:
    """Recent observed jobs from jobs.db. Returned newest-first.

    Filters: `printer_id` (e.g. p2/p3/p1s) and `capture_source` (one of
    local-slice / local-import / sd-card-or-other). The page at /jobs
    fetches this; staff using the API directly can pass
    capture_source=sd-card-or-other to find prints not in the patron
    ledger."""
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be 1..1000")
    return {
        "jobs": jobs_db.list_jobs(
            limit=limit,
            printer_id=printer_id,
            capture_source=capture_source,
        ),
        "stats": jobs_db.stats(),
    }


@router.delete("/api/jobs/{job_id}")
async def api_jobs_delete(job_id: int) -> dict:
    """Drop a job row from jobs.db. The MQTT observer may re-create
    the row on the next report frame if the same (printer_id, task_id)
    is still active — for cleaning up ghost / false-start entries
    delete after the print has actually ended."""
    try:
        return jobs_db.delete_job(job_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/api/jobs/{job_id}")
async def api_jobs_update(job_id: int, request: Request) -> dict:
    """Patch editable fields on a single job row. Body is JSON
    `{field: value, ...}` — see jobs_db._EDITABLE_FIELDS for the
    whitelist. Setting is_manually_edited=1 (handled inside
    update_job) makes the MQTT observer back off, so the staff
    correction sticks across restarts and report frames."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected JSON body")
    try:
        return jobs_db.update_job(job_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request) -> HTMLResponse:
    """Reconciliation view — every job the dashboard saw, with a flag
    on the ones that don't match an entry in printqueue/orders.json."""
    import datetime as _dt

    def _fmt_hm(seconds: int) -> str:
        h, rem = divmod(int(seconds), 3600)
        mins = rem // 60
        return f"{h}h{mins:02d}m" if h else f"{mins}m"

    jobs = jobs_db.list_jobs(limit=500)
    now = _dt.datetime.now()
    for j in jobs:
        for fld in ("started_at", "finished_at", "last_seen"):
            v = j.get(fld) or ""
            j[f"_{fld}_short"] = f"{v[5:10]} {v[11:16]}" if len(v) >= 16 else v

        # Duration: completed jobs use the persisted total; in-flight
        # jobs use (now - started_at) so the column doesn't show blank
        # for prints we're currently watching.
        dur = j.get("duration_seconds")
        if dur:
            j["_duration_label"] = _fmt_hm(dur)
        elif j.get("started_at") and not j.get("finished_at"):
            try:
                s = _dt.datetime.fromisoformat(j["started_at"])
                j["_duration_label"] = f"{_fmt_hm(int((now - s).total_seconds()))} so far"
            except ValueError:
                j["_duration_label"] = ""
        else:
            j["_duration_label"] = ""

        # Filament: prefer the persisted actual (set at terminal). For
        # in-flight jobs, scale the slicer prediction by current percent
        # so staff can eyeball "how much filament we're burning right
        # now" without the row going blank.
        actual = j.get("actual_grams")
        pred   = j.get("predicted_grams")
        pct    = j.get("last_percent")
        fetch  = j.get("grams_fetch_state")
        if actual is not None:
            j["_grams_label"] = f"{actual:.1f} g"
            j["_grams_class"] = ""
        elif pred is not None and pct is not None and j.get("outcome") == "running":
            j["_grams_label"] = f"~{pred * pct / 100.0:.1f} / {pred:.1f} g"
            j["_grams_class"] = ""
        elif pred is not None:
            j["_grams_label"] = f"~{pred:.1f} g"
            j["_grams_class"] = ""
        elif fetch == "failed":
            # Auto-fetch tried and couldn't find the source 3MF (most
            # common cause: Bambu cloud / Handy print whose file never
            # landed in FTP-visible storage). Surface this so staff
            # know they need to type grams manually instead of
            # waiting for an auto-fill that won't come.
            j["_grams_label"] = "n/a"
            j["_grams_class"] = "grams-failed"
        elif fetch == "pending":
            j["_grams_label"] = "…"
            j["_grams_class"] = "grams-pending"
        else:
            j["_grams_label"] = ""
            j["_grams_class"] = ""

    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs,
        "stats": jobs_db.stats(),
    })


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
