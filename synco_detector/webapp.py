"""FastAPI API + React SPA."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import Body, FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from synco_detector import log
from synco_detector.config import ROOT, SAMPLE_RATE
from synco_detector.engine import ENGINE
from synco_detector.selftest import format_report, run_selftest

DIST = ROOT / "client" / "dist"
LEGACY_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Synco Detector", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")
elif LEGACY_STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=LEGACY_STATIC), name="static")


@app.on_event("startup")
def _startup() -> None:
    log.boot("starting microphone + Whisper")
    ENGINE.start()
    ENGINE.ensure_whisper()
    log.boot("live engine is up")


@app.on_event("shutdown")
def _shutdown() -> None:
    ENGINE.stop()


@app.get("/")
def index() -> FileResponse:
    spa = DIST / "index.html"
    if spa.is_file():
        return FileResponse(spa)
    return FileResponse(LEGACY_STATIC / "index.html")


@app.get("/api/state")
def state() -> dict:
    return ENGINE.snapshot()


@app.post("/api/selftest")
def api_selftest() -> dict:
    log.boot("running in-process self-test")
    result = run_selftest()
    result["report"] = format_report(result)
    log.boot(f"self-test {result['passed']}/{result['total']}")
    return result


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)) -> JSONResponse:
    raw = await file.read()
    tmp = Path("/tmp") / f"synco_upload_{file.filename or 'clip.wav'}"
    tmp.write_bytes(raw)
    log.boot(f"file upload {file.filename} ({len(raw)} bytes)")
    try:
        y, sr = sf.read(str(tmp), always_2d=False)
    except Exception as exc:
        log.error(f"could not read audio: {exc}")
        return JSONResponse({"error": f"could not read audio: {exc}"}, status_code=400)
    if y.ndim > 1:
        y = np.mean(y, axis=1)
    y = y.astype(np.float32)
    v = ENGINE.analyze_array(y, sr=int(sr))
    return JSONResponse(v.to_dict())


@app.post("/api/bt/control")
async def api_bt_control(payload: dict = Body(default_factory=dict)) -> JSONResponse:
    action = str((payload or {}).get("action") or "").strip().lower()
    if action not in {"play", "pause", "next", "prev", "seek"}:
        return JSONResponse(
            {"ok": False, "error": "action must be play|pause|next|prev|seek"},
            status_code=400,
        )
    position_ms = (payload or {}).get("position_ms")
    try:
        pos = int(position_ms) if position_ms is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad position_ms"}, status_code=400)
    result = await asyncio.to_thread(ENGINE.media_command, action, pos)
    return JSONResponse(result)


_COVER_CACHE: dict[str, str | None] = {}


@app.get("/api/cover")
async def api_cover(title: str = "", artist: str = "", album: str = "") -> JSONResponse:
    """Look up cover art from public catalogs (AVRCP does not carry images)."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    album = (album or "").strip()
    if not title and not artist and not album:
        return JSONResponse({"url": None})
    key = f"{artist.casefold()}|{album.casefold()}|{title.casefold()}"
    if key in _COVER_CACHE:
        return JSONResponse({"url": _COVER_CACHE[key]})

    def _lookup() -> str | None:
        import urllib.parse
        import urllib.request

        term = " ".join(p for p in (artist, album or title, title if album else "") if p).strip()
        if not term:
            term = title or artist or album
        q = urllib.parse.urlencode(
            {"term": term, "entity": "song", "media": "music", "limit": "5"}
        )
        req = urllib.request.Request(
            f"https://itunes.apple.com/search?{q}",
            headers={"User-Agent": "SyncoDetector/1.1"},
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = data.get("results") or []
        title_l = title.casefold()
        artist_l = artist.casefold()
        album_l = album.casefold()
        best_url = None
        best_score = -1
        for row in results:
            score = 0
            rt = str(row.get("trackName") or "").casefold()
            ra = str(row.get("artistName") or "").casefold()
            rc = str(row.get("collectionName") or "").casefold()
            if title_l and title_l in rt:
                score += 4
            if artist_l and artist_l in ra:
                score += 3
            if album_l and album_l in rc:
                score += 2
            art = row.get("artworkUrl100") or row.get("artworkUrl60")
            if not art:
                continue
            if score > best_score:
                best_score = score
                best_url = str(art).replace("100x100bb", "600x600bb").replace("60x60bb", "600x600bb")
        if best_url is None and results:
            art = results[0].get("artworkUrl100") or results[0].get("artworkUrl60")
            if art:
                best_url = str(art).replace("100x100bb", "600x600bb").replace("60x60bb", "600x600bb")
        return best_url

    try:
        url = await asyncio.to_thread(_lookup)
    except Exception:
        url = None
    _COVER_CACHE[key] = url
    if len(_COVER_CACHE) > 256:
        # Drop arbitrary oldest-ish entries
        for stale in list(_COVER_CACHE.keys())[:64]:
            _COVER_CACHE.pop(stale, None)
    return JSONResponse({"url": url})


@app.post("/api/audio/ensure-speakers")
async def api_ensure_speakers() -> JSONResponse:
    """Make sure desktop/browser audio is on real speakers (not the silent BT sink)."""
    cap = ENGINE.capture
    mgr = getattr(cap, "_mgr", None)
    if mgr is not None and hasattr(mgr, "_ensure_desktop_speakers"):
        await asyncio.to_thread(mgr._ensure_desktop_speakers)
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "not in bluetooth sink mode"})


@app.post("/api/history/clear")
async def api_history_clear() -> JSONResponse:
    ENGINE.clear_history()
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws(sock: WebSocket) -> None:
    await sock.accept()
    log.boot("ui websocket connected")
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    loop = asyncio.get_event_loop()

    def on_update(snap: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, snap)
        except Exception:
            pass

    unsub = ENGINE.subscribe(on_update)
    try:
        await sock.send_text(json.dumps(ENGINE.snapshot()))
        while True:
            try:
                snap = await asyncio.wait_for(queue.get(), timeout=1.0)
                await sock.send_text(json.dumps(snap))
            except asyncio.TimeoutError:
                await sock.send_text(json.dumps(ENGINE.snapshot()))
    except WebSocketDisconnect:
        log.boot("ui websocket disconnected")
        return
    except Exception as exc:
        log.warn(f"websocket: {exc}")
        return
    finally:
        unsub()


@app.websocket("/ws/audio")
async def ws_audio(sock: WebSocket) -> None:
    """Stream live float32 mono PCM for in-browser listen-through."""
    await sock.accept()
    log.boot("audio websocket connected")
    chunk_s = 0.08
    await sock.send_text(
        json.dumps(
            {
                "sr": SAMPLE_RATE,
                "channels": 1,
                "format": "f32le",
                "chunk_s": chunk_s,
                "mode": "consume",
            }
        )
    )
    loop = asyncio.get_running_loop()
    next_send = loop.time()
    try:
        while True:
            y = await asyncio.to_thread(ENGINE.pcm_chunk, chunk_s)
            if y.size == 0:
                await asyncio.sleep(0.015)
                continue
            await sock.send_bytes(np.ascontiguousarray(y, dtype=np.float32).tobytes())
            # Pace at real-time so the browser stays near the live edge.
            dur = y.size / float(SAMPLE_RATE)
            next_send += dur
            delay = next_send - loop.time()
            if delay > 0.002:
                await asyncio.sleep(delay)
            elif delay < -0.12:
                # Fell behind — resync clock and let client/consume drop lag.
                next_send = loop.time()
    except WebSocketDisconnect:
        log.boot("audio websocket disconnected")
        return
    except Exception as exc:
        log.warn(f"audio websocket: {exc}")
        return
