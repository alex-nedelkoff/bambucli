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
import struct
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
# How often the FTPS-based SD-card marker probe refreshes. Cards only
# change when a human swaps one between printers, which is rare — 5 min
# is plenty fast for the receipt auto-fill to be right, and we also
# trigger an immediate re-probe after every save-to-SD as a side-effect.
SD_CARD_PROBE_INTERVAL_SEC = 300
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


# P1S doesn't expose RTSPS on port 322 — that's an X1-family path. Under
# LAN-Only Mode the P1S instead exposes Bambu's legacy custom camera
# protocol on TCP 6000: TLS-wrapped, 80-byte auth packet up front, then
# a continuous stream of {16-byte header, JPEG payload} pairs. Protocol
# verified against Doridian/OpenBambuAPI video.md, synman/bambu-go2rtc
# camera-stream.py, and mattcar15/bambu-connect CameraClient.py — all
# three agree byte-for-byte.
_P1_CAM_PORT = 6000
# 16-byte header: u32 LE payload-size, then `00 30 00 00`, then two zero
# u32s. Username field is 32 bytes NUL-padded; access code is the same
# 32-byte NUL-padded layout. Total 80 bytes, sent in a single write.
_P1_CAM_AUTH_PREAMBLE = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
# Max plausible JPEG size from the camera (it's a 1280x720 MJPEG-ish
# stream; frames are typically 60-120 KB). Anything beyond 4 MB is
# almost certainly a desync — bail rather than blocking on recv.
_P1_CAM_MAX_FRAME = 4 * 1024 * 1024


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    """Read exactly n bytes off a TLS socket. recv() can return short
    even on blocking sockets, so loop until we have the full ask or the
    peer closes (0-byte read → IOError)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError(f"peer closed after {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


def _grab_snapshot_p1(ip: str, access_code: str, timeout: float = 12.0) -> bytes:
    """Pull a single JPEG frame from a P1-family printer over the LAN-Only
    Mode custom camera protocol (TCP 6000).

    Requires LAN-Only Mode enabled on the printer. Connection lifetime is
    a single frame — we connect, auth, read one header + one payload,
    close. The server would happily keep streaming, but a snapshot loop
    that opens and closes per tick is cleaner than maintaining a
    persistent connection that has to be torn down and rebuilt on
    printer reboots / network blips.
    """
    auth = _P1_CAM_AUTH_PREAMBLE + b"bblp".ljust(32, b"\x00") \
        + access_code.encode("ascii").ljust(32, b"\x00")
    assert len(auth) == 80
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((ip, _P1_CAM_PORT), timeout=timeout)
    try:
        # SNI is still set even with verify disabled — some embedded TLS
        # stacks (mbedTLS in particular) refuse the handshake otherwise.
        tls = ctx.wrap_socket(raw, server_hostname=ip)
        try:
            tls.settimeout(timeout)
            tls.sendall(auth)
            header = _recv_exact(tls, 16)
            payload_size, _itrack, _flags, _resv = struct.unpack("<IIII", header)
            if payload_size == 0 or payload_size > _P1_CAM_MAX_FRAME:
                raise IOError(f"implausible frame size {payload_size}")
            jpeg = _recv_exact(tls, payload_size)
        finally:
            try:
                tls.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            tls.close()
    finally:
        try:
            raw.close()
        except OSError:
            pass
    if jpeg[:3] != b"\xff\xd8\xff":
        raise IOError(f"non-JPEG payload (magic={jpeg[:4].hex()})")
    return jpeg


def _send_close_notify_best_effort(conn: ssl.SSLSocket) -> None:
    """Send our TLS close_notify alert on the data channel without
    blocking on the server's reply.

    OpenSSL's `SSL_shutdown` is two-stage: stage 1 writes our
    close_notify (fast, just a socket send); stage 2 reads the peer's
    close_notify. Python's `SSLSocket.unwrap()` runs both stages
    blocking. X1C vsftpd refuses uploads with "426 Failure reading
    network stream" if stage 1 doesn't happen — but P1S firmware
    never replies in stage 2, so a plain unwrap() hangs there until
    the socket timeout fires. Lowering the data socket's timeout
    around the unwrap call lets stage 1 finish on both, while
    bounding the P1S stall to a few hundred ms.
    """
    try:
        conn.settimeout(0.5)
    except OSError:
        # Socket already in a bad state — skip; the worst case is
        # the server reports 426 on this transfer. Don't make it
        # worse by raising.
        return
    try:
        conn.unwrap()
    except (ssl.SSLError, OSError):
        # P1S: TimeoutError once close_notify has been sent. X1C
        # under bad conditions: SSLError. Either way our half of the
        # shutdown is on the wire by the time these raise.
        pass


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
        # Same best-effort close_notify pattern as storbinary below.
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while 1:
                data = conn.recv(blocksize)
                if not data:
                    break
                callback(data)
            _send_close_notify_best_effort(conn)
        return self.voidresp()

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        # Verbatim stdlib FTP_TLS.storbinary except for the unwrap()
        # call at the tail — we use a best-effort variant that sends
        # the client's TLS close_notify alert (X1C vsftpd needs it; on
        # current firmware the upload returns "426 Failure reading
        # network stream" without it) but doesn't wait for the
        # server's close_notify response (P1S firmware never sends
        # one, so a blocking unwrap stalls until the socket timeout).
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while 1:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback is not None:
                    callback(buf)
            _send_close_notify_best_effort(conn)
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


# Tiny marker file we write to the printer's FTP root to identify the
# physical SD card. Bambu firmware doesn't expose any card-side serial,
# label, or fingerprint over MQTT or FTPS — this marker is the only way
# to tell *which* card is in *which* printer (and to follow a card when
# staff swap them between printers). Hidden via leading dot so it
# doesn't clutter the touchscreen's file list.
_SD_MARKER_NAME = ".bambucli_sd_id.txt"
SD_CARDS_JSON = BASE_DIR / "sd_cards.json"


def _ftps_get_sd_marker(ip: str, access_code: str,
                        timeout: float = 30.0) -> str:
    """Read the SD-card marker UUID from a printer's FTP root, writing a
    fresh one if the marker doesn't exist yet. Same FTPS workarounds as
    _ftps_upload (SECLEVEL=0 + TLS 1.2 + session-reuse + close_notify-
    best-effort). Single connection per call.

    Returns a UUID4 hex string identifying the physical SD card that's
    currently in this printer. The UUID travels with the card across
    printers — that's the whole point.
    """
    import io
    import uuid as _uuid

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
        # Try to read an existing marker first.
        buf = io.BytesIO()
        try:
            ftps.retrbinary(f"RETR {_SD_MARKER_NAME}", buf.write)
            existing = buf.getvalue().decode("ascii", errors="ignore").strip()
            # 32-char hex check — guard against a corrupted / hand-edited
            # marker by rewriting if the content doesn't look like a uuid.
            int(existing, 16)
            if len(existing) == 32:
                return existing
        except (ftplib.error_perm, ValueError):
            pass
        # No marker (or unreadable): generate a fresh one and write it.
        new_id = _uuid.uuid4().hex
        ftps.storbinary(f"STOR {_SD_MARKER_NAME}",
                        io.BytesIO(new_id.encode("ascii")))
        return new_id
    finally:
        try:
            ftps.quit()
        except Exception:
            try:
                ftps.close()
            except Exception:
                pass


def _load_sd_card_names() -> dict[str, str]:
    """Friendly names for SD card UUIDs, loaded from sd_cards.json. Missing
    file / parse error returns an empty dict — caller falls back to a
    short-prefix default like "Card a1b2c3d4"."""
    if not SD_CARDS_JSON.exists():
        return {}
    try:
        data = json.loads(SD_CARDS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save_sd_card_names(names: dict[str, str]) -> None:
    """Atomic write: serialize to a temp sibling and rename, so a partial
    write never leaves a corrupt sd_cards.json behind."""
    tmp = SD_CARDS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(names, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(SD_CARDS_JSON)


def _default_sd_card_name(uuid_hex: str) -> str:
    return f"Card {uuid_hex[:8]}"


# Per-printer captured_filename + captured_task_id persisted to disk so a
# dashboard restart mid-print doesn't lose the name we eavesdropped from
# the original project_file command. The /report frames Bambu firmware
# sends in steady state often omit subtask_name, so without this restart
# = "Local print (no filename sent)" until the next print starts.
CAPTURE_STATE_JSON = BASE_DIR / "printer_capture_state.json"
_capture_state_lock = threading.Lock()


def _load_capture_state() -> dict[str, dict]:
    if not CAPTURE_STATE_JSON.exists():
        return {}
    try:
        data = json.loads(CAPTURE_STATE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_capture_state_entry(printer_id: str, filename: str | None,
                              task_id: str | None) -> None:
    """Atomic read-modify-write for one printer's slot. Tiny file; we
    rewrite the whole thing each call so concurrent printers can't
    clobber each other's entries."""
    with _capture_state_lock:
        data = _load_capture_state()
        if filename is None and task_id is None:
            data.pop(printer_id, None)
        else:
            data[printer_id] = {"filename": filename, "task_id": task_id}
        tmp = CAPTURE_STATE_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(CAPTURE_STATE_JSON)


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
            # Identifies the physical SD card currently in this printer.
            # uuid is None until the FTPS marker probe has run at least
            # once; refreshed on a background loop + after every save-to-SD
            # so a card swap is picked up within ~5min worst-case.
            "sd_card_uuid": None,
            "sd_card_probed_at": None,
            "report": {},
        }
        self._client: mqtt.Client | None = None
        self._snapshot_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._sd_card_task: asyncio.Task | None = None
        # Synchronisation guard: _ftps_get_sd_marker opens a real FTPS
        # connection. Skip any probe attempt that overlaps with a
        # save-to-SD upload to the same printer — Bambu's FTPS server
        # is single-connection per credentials and a second login while
        # the first is mid-STOR sometimes 426s the upload.
        self._sd_card_lock = threading.Lock()

        # Restore the captured filename across dashboard restarts so an
        # in-flight print doesn't fall back to "Local print (no
        # filename sent)" until it ends. See _persist_capture.
        _saved = _load_capture_state().get(self.id) or {}
        _sn = _saved.get("filename")
        if isinstance(_sn, str) and _sn:
            self.state["captured_filename"] = _sn
            _stid = _saved.get("task_id")
            self.state["captured_task_id"] = _stid if isinstance(_stid, str) else None

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
        for task_attr in ("_snapshot_task", "_reconnect_task", "_sd_card_task"):
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
        is_p1 = str(self.cfg.get("model", "")).upper().startswith("P1")
        grabber = _grab_snapshot_p1 if is_p1 else _grab_snapshot
        while True:
            try:
                jpg = await asyncio.to_thread(
                    grabber, self.cfg["ip"], self.cfg["access_code"],
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

    def _probe_sd_card_sync(self) -> str | None:
        """Run the FTPS marker probe on the calling thread. Updates the
        cached uuid + timestamp; returns the uuid or None on failure.
        Serialized by self._sd_card_lock so it doesn't race a concurrent
        save-to-SD upload (Bambu's FTPS is single-connection per creds)."""
        if not self._sd_card_lock.acquire(blocking=False):
            return self.state.get("sd_card_uuid")
        try:
            uuid_hex = _ftps_get_sd_marker(
                self.cfg["ip"], self.cfg["access_code"], timeout=30.0,
            )
        except Exception as e:
            # Probe failures are non-fatal — the next periodic tick will
            # try again. Surface as snapshot_error-style soft state.
            self.state["sd_card_error"] = str(e)[:200]
            return None
        finally:
            self._sd_card_lock.release()
        self.state["sd_card_uuid"] = uuid_hex
        self.state["sd_card_probed_at"] = _time_mod.time()
        self.state["sd_card_error"] = None
        return uuid_hex

    async def _sd_card_loop(self) -> None:
        """Refresh the SD-card marker every SD_CARD_PROBE_INTERVAL_SEC so a
        card swap is picked up without a dashboard restart. First probe
        runs immediately after MQTT connect (which triggers from the
        watcher in _on_connect) — this loop is the catch-up for
        long-running sessions."""
        # First tick: short delay so we don't pile onto the initial MQTT
        # connect storm.
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        while True:
            try:
                await asyncio.to_thread(self._probe_sd_card_sync)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Belt-and-braces — _probe_sd_card_sync already swallows
                # FTPS errors, but if anything else explodes we don't
                # want to take down the dashboard's event loop.
                pass
            self.hub.notify(self.id)
            try:
                await asyncio.sleep(SD_CARD_PROBE_INTERVAL_SEC)
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
                    self._persist_capture()
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
            wake_fetch = jobs_db.record_observation(
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
            wake_fetch = False
        if wake_fetch:
            self.hub.signal_grams_fetch_now()

    def _update_filename_capture(self) -> None:
        """Reconcile captured_filename against the latest report.

        Three cases:
          - Report has a non-empty subtask_name: trust it, bind to current task_id.
          - Capture is unbound (just received a project_file command): bind to
            whatever task_id the printer is now reporting.
          - Task_id changed and we never got a fresh name for it: drop the
            stale capture so we don't mislabel the new print.
        Persists to disk whenever captured_filename / captured_task_id
        actually change, so a dashboard restart keeps the name."""
        before = (self.state.get("captured_filename"),
                  self.state.get("captured_task_id"))
        p = (self.state["report"].get("print") or {})
        current_tid = p.get("task_id")
        sn = (p.get("subtask_name") or "").strip()

        if sn:
            self.state["captured_filename"] = sn
            self.state["captured_task_id"] = current_tid
        elif (self.state.get("captured_filename")
                and self.state.get("captured_task_id") is None
                and current_tid):
            self.state["captured_task_id"] = current_tid
        elif (current_tid
                and self.state.get("captured_task_id") not in (None, current_tid)):
            self.state["captured_filename"] = None
            self.state["captured_task_id"] = None

        after = (self.state.get("captured_filename"),
                 self.state.get("captured_task_id"))
        if after != before:
            self._persist_capture()

    def _persist_capture(self) -> None:
        """Best-effort write of (captured_filename, captured_task_id) to
        the on-disk JSON. Called only when the values actually change,
        so we're not hitting the disk on every report frame."""
        try:
            _save_capture_state_entry(
                self.id,
                self.state.get("captured_filename"),
                self.state.get("captured_task_id"),
            )
        except Exception:
            # Persistence is best-effort. A failed write doesn't
            # corrupt in-memory state, and the next change retries.
            pass

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
        self._grams_fetch_task: asyncio.Task | None = None
        # Signalled from the MQTT callback thread when a row transitions
        # to terminal with pending grams. The fetch loop waits on this
        # event instead of sleeping a flat 60s, so the FTPS fetch lands
        # while the file is still on the SD card.
        self._grams_fetch_event: asyncio.Event | None = None

    def signal_grams_fetch_now(self) -> None:
        """Thread-safe wakeup for the grams-fetch loop. Called from the
        paho MQTT callback thread; the actual Event.set runs back on the
        asyncio loop via call_soon_threadsafe."""
        ev = self._grams_fetch_event
        if ev is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(ev.set)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._grams_fetch_event = asyncio.Event()
        for cfg in _load_printers():
            client = PrinterClient(cfg, self)
            self.clients[client.id] = client
            client.start()
            if client.webcam_enabled:
                client._snapshot_task = loop.create_task(client._snapshot_loop())
            client._reconnect_task = loop.create_task(client._reconnect_watchdog())
            client._sd_card_task = loop.create_task(client._sd_card_loop())
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
            # Wait on the wake event with a 60s ceiling. Terminal
            # transitions (a print finishing/failing/cancelling) set the
            # event so the fetch runs within ms instead of up to a
            # minute later — see DashboardHub.signal_grams_fetch_now.
            ev = self._grams_fetch_event
            try:
                if ev is not None:
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                    ev.clear()
                else:
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
        # Lazy import: slice_order owns the palette + hex→name table. Doing
        # the lookup here keeps the dashboard's snapshot endpoint as the
        # single source of truth for "what colour is this slot loaded with"
        # and saves the JS from carrying a duplicate table.
        from slice_order import _hex_to_name as _bambu_hex_to_name
        sd_names = _load_sd_card_names()

        def _name_for(raw_hex: str) -> str:
            # Bambu reports tray_color as RGBA (e.g. "FFFFFFFF") or empty.
            # An unloaded slot reports "00000000" — distinct from real black.
            # _hex_to_name wants "#RRGGBB", so strip alpha and prefix.
            h = (raw_hex or "").strip()
            if not h or set(h.upper()) <= {"0"}:
                return ""
            return _bambu_hex_to_name("#" + h[:6])

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
                        color_hex = (tray.get("tray_color") or "").strip()
                        slots.append({
                            "id": str(tray.get("id", "")),
                            # tray_color is RGBA hex (8 chars) when set; empty
                            # when the slot is unloaded.
                            "color_hex": color_hex,
                            "color_name": _name_for(color_hex),
                            "type": (tray.get("tray_type") or "").strip(),
                        })
                    ams_units.append({
                        "id": str(unit.get("id", "")),
                        "slots": slots,
                    })
                vt = p.get("vt_tray") or {}
                vt_color = (vt.get("tray_color") or "").strip()
                external_spool = {
                    "color_hex": vt_color,
                    "color_name": _name_for(vt_color),
                    "type": (vt.get("tray_type") or "").strip(),
                }
                sd_uuid = c.state.get("sd_card_uuid")
                sd_card = None
                if sd_uuid:
                    sd_card = {
                        "uuid": sd_uuid,
                        "name": sd_names.get(sd_uuid) or _default_sd_card_name(sd_uuid),
                    }
                out.append({
                    "id": c.id,
                    "label": c.cfg.get("label", c.id),
                    "model": (c.cfg.get("model") or "").upper(),
                    "online": bool(c.state.get("online")),
                    "ams_units": ams_units,
                    "external_spool": external_spool,
                    "sd_card": sd_card,
                })
            return out

    async def save_to_printer_sd(self, printer_id: str, local_path: Path,
                                 remote_name: str | None = None) -> dict:
        """Upload a 3MF over FTPS so it lands on the printer's SD card and
        shows up in the touchscreen's file list. No MQTT command is sent —
        the print is started manually by staff from the touchscreen.

        Auto-launch over MQTT was attempted earlier in development but
        ran into the X1C "AMS Mapping Table" HMS that we couldn't clear
        without cloud auth (see the bambu-ams-mapping-table memory).
        Save-to-SD is the reliable LAN flow and is what the UI now uses
        exclusively.
        """
        client = self.clients.get(printer_id)
        if client is None:
            raise HTTPException(404, f"unknown printer '{printer_id}'")
        if not local_path.exists() or not local_path.is_file():
            raise HTTPException(404, f"file not found: {local_path.name}")
        remote = remote_name or local_path.name

        def _work() -> None:
            _ftps_upload(client.cfg["ip"], client.cfg["access_code"],
                         local_path, remote)
            # Re-probe the SD-card marker right after upload: an associate
            # who's hot-swapping cards usually saves the next file
            # immediately after putting the new card in, so this catches
            # the swap without waiting for the 5-min periodic refresh.
            # The probe takes a separate FTPS connection but runs after
            # the STOR completes, so it can't 426 the upload.
            client._probe_sd_card_sync()

        try:
            await asyncio.to_thread(_work)
        except ftplib.all_errors as e:
            raise HTTPException(502, f"FTPS upload failed: {e}")
        except (socket.timeout, ConnectionError, OSError) as e:
            raise HTTPException(502, f"network error talking to printer: {e}")
        except RuntimeError as e:
            raise HTTPException(502, str(e))

        sd_uuid = client.state.get("sd_card_uuid")
        sd_names = _load_sd_card_names()
        return {
            "ok": True,
            "printer_id": printer_id,
            "label": client.cfg.get("label", printer_id),
            "filename": remote,
            "sd_card": {
                "uuid": sd_uuid,
                "name": (sd_names.get(sd_uuid) or _default_sd_card_name(sd_uuid))
                        if sd_uuid else None,
            } if sd_uuid else None,
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
) -> dict:
    """FTPS-upload the named 3MF (under printqueue/work) to the chosen
    printer's SD card. The file appears in the printer's on-screen file
    list — staff start the print by tapping it on the touchscreen.

    Endpoint name preserved (`/api/printers/send`) for URL stability;
    the action is now save-to-SD only — see DashboardHub.save_to_printer_sd
    for the rationale.
    """
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

    return await hub.save_to_printer_sd(printer_id, local)


@router.get("/sd-cards", response_class=HTMLResponse)
async def page_sd_cards(request: Request) -> HTMLResponse:
    """Tiny admin page: list every SD card UUID we've seen, let staff
    rename them. Linked from the global nav so it lives at the same
    level as /dashboard, /history, /jobs."""
    cards = (await api_sd_cards())["cards"]
    return templates.TemplateResponse(request, "sd_cards.html", {"cards": cards})


@router.get("/api/sd-cards")
async def api_sd_cards() -> dict:
    """List every SD card UUID we've ever probed + its friendly name and
    last-seen printer (if currently in one). Powers a small admin UI for
    renaming cards to operational labels like R1/R2/B1."""
    names = _load_sd_card_names()
    snapshot = hub.list_printers()
    by_uuid: dict[str, dict] = {}
    for uuid_hex, name in names.items():
        by_uuid[uuid_hex] = {
            "uuid": uuid_hex,
            "name": name,
            "currently_in": None,
        }
    for p in snapshot:
        sd = p.get("sd_card") or {}
        u = sd.get("uuid")
        if not u:
            continue
        if u not in by_uuid:
            by_uuid[u] = {
                "uuid": u,
                "name": names.get(u) or _default_sd_card_name(u),
                "currently_in": None,
            }
        by_uuid[u]["currently_in"] = {
            "printer_id": p["id"], "printer_label": p["label"],
        }
    return {"cards": sorted(by_uuid.values(), key=lambda c: c["name"].lower())}


@router.post("/api/sd-cards/{uuid_hex}/rename")
async def api_sd_card_rename(
    uuid_hex: str,
    name: str = Form(...),
) -> dict:
    """Set the friendly name for an SD-card UUID. Empty / whitespace
    names fall back to the default 'Card <prefix>'. Strict uuid format
    check keeps the JSON store from accumulating junk keys if an
    attacker hits this endpoint with bad inputs."""
    if not re.match(r"^[0-9a-f]{32}$", uuid_hex):
        raise HTTPException(400, "uuid must be 32 hex chars")
    name = name.strip()[:80]
    names = _load_sd_card_names()
    if name:
        names[uuid_hex] = name
    else:
        names.pop(uuid_hex, None)
    _save_sd_card_names(names)
    return {
        "ok": True,
        "uuid": uuid_hex,
        "name": name or _default_sd_card_name(uuid_hex),
    }


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
