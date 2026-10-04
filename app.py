"""
app.py — iPod Jukebox: turn a Spotify playlist into a tagged, artwork-complete
music folder ready for your iPod.

Setup:  see README.md
Run:    python app.py
Open:   http://127.0.0.1:4445   (override with PORT in .env)
"""

import asyncio
import csv
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

import requests as http
import uvicorn
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mutagen.mp4 import MP4, MP4Cover, AtomDataType

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
# Everything here has a sane default so `python app.py` just works after
# `pip install -r requirements.txt` — override any of it in .env if you need to.

BASE_DIR = Path(__file__).resolve().parent

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "4445"))

DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", str(BASE_DIR / "downloads"))

# yt-dlp and ffmpeg are found automatically if they're on PATH (the normal case
# after `pip install yt-dlp` and installing ffmpeg via your OS package manager).
# Only set YT_DLP_PATH / FFMPEG_PATH in .env if they aren't on PATH.
YT_DLP      = os.environ.get("YT_DLP_PATH") or shutil.which("yt-dlp") or "yt-dlp"
_ffmpeg_bin = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
FFMPEG      = os.path.dirname(_ffmpeg_bin) if _ffmpeg_bin else ""  # yt-dlp wants the containing folder

# Optional: direct "import a playlist without a CSV" feature — see README's
# Spotify section. Everything works without this; it just saves a CSV export.
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = f"http://127.0.0.1:{PORT}/api/spotify/callback"
SPOTIFY_SCOPES        = "playlist-read-private playlist-read-collaborative"

# Ensure the download folder exists before mounting it for the audio player
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ── Global state ──────────────────────────────────────────────────────────────

tracks:           dict[str, dict]            = {}
ws_clients:       set[WebSocket]             = set()
main_loop:        asyncio.AbstractEventLoop  = None
download_queue:   asyncio.Queue              = None
current_playlist: str                        = ""
server:           "uvicorn.Server"           = None

# Single-user, in-memory like everything else here — lost on restart, which is
# fine since you just reconnect (the refresh_token would otherwise outlive it).
spotify_auth:     dict                       = {"access_token": "", "refresh_token": "", "expires_at": 0}
_spotify_oauth_state: str                    = ""

# ── WebSocket helpers ─────────────────────────────────────────────────────────

async def _broadcast(msg: dict):
    dead = set()
    text = json.dumps(msg)
    for ws in ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


def _broadcast_sync(msg: dict):
    if main_loop and not main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast(msg), main_loop)


def _update(track_id: str, **kwargs):
    if track_id in tracks:
        tracks[track_id].update(kwargs)
        _broadcast_sync({"type": "track_update", "track": tracks[track_id]})

# ── YouTube ───────────────────────────────────────────────────────────────────

def _yt_base_args() -> list:
    args = [YT_DLP]
    if FFMPEG:
        args += ["--ffmpeg-location", FFMPEG]
    return args


def _yt_search(artist: str, title: str) -> Optional[str]:
    try:
        r = subprocess.run(
            _yt_base_args() + [
             "--no-warnings", "--quiet",
             "--extractor-args", "youtube:player_client=android",
             "--print", "webpage_url", f"ytsearch1:{artist} - {title} official audio"],
            capture_output=True, text=True, timeout=30,
        )
        url = r.stdout.strip()
        return url if url.startswith("http") else None
    except Exception:
        return None


def _yt_download(url: str, out_path: str, on_progress) -> bool:
    try:
        proc = subprocess.Popen(
            _yt_base_args() + [
             "-x", "--audio-format", "m4a",
             "--audio-quality", "0",
             "--extractor-args", "youtube:player_client=android",
             "--postprocessor-args", "ffmpeg:-c:a aac -b:a 128k",
             "--no-warnings", "--progress", "--newline",
             "-o", out_path, url],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            m = re.search(r"(\d+\.?\d*)%", line)
            if m:
                on_progress(float(m.group(1)))
        proc.wait()
        return proc.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def _embed_tags(path: str, t: dict):
    try:
        audio = MP4(path)
        audio.tags["\xa9nam"] = [t["title"]]
        audio.tags["\xa9ART"] = [t["artist"]]
        audio.tags["\xa9alb"] = [t["album"]]
        audio.tags["trkn"]    = [(t["num"], 0)]
        audio.save()
    except Exception:
        pass

# ── iTunes artwork ────────────────────────────────────────────────────────────

def _normalize(artist: str) -> str:
    for tok in [" ft. ", " ft ", " feat. ", " feat ", " featuring ", " & "]:
        artist = artist.replace(tok, " ")
    return " ".join(artist.split())


def _itunes_search(query: str, limit: int = 5) -> list:
    try:
        data = http.get(
            "https://itunes.apple.com/search",
            params={"term": query, "media": "music", "entity": "song", "limit": limit},
            timeout=15,
        ).json()
        results = []
        for r in data.get("results", []):
            url = r.get("artworkUrl100", "")
            if url:
                results.append({
                    "url":       url,
                    "url_hires": url.replace("100x100bb", "3000x3000bb"),
                    "track":     r.get("trackName", ""),
                    "artist":    r.get("artistName", ""),
                    "album":     r.get("collectionName", ""),
                })
        return results
    except Exception:
        return []


def _embed_artwork(path: str, url: str) -> bool:
    try:
        data = http.get(url, timeout=15).content
        fmt  = AtomDataType.PNG if data[:8] == b'\x89PNG\r\n\x1a\n' else AtomDataType.JPEG
        audio = MP4(path)
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()
        return True
    except Exception:
        return False

# ── Download worker ───────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    for c in r'\/:*?"<>|':
        name = name.replace(c, "_")
    return name.strip()


def _purge_file(track_id: str):
    """Delete a track's downloaded file and clear the fields that point at it."""
    t = tracks.get(track_id)
    if not t:
        return
    fp = t.get("file_path", "")
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception:
            pass
    t.update(file_path="", url_path="", progress=0)


def _do_artwork(track_id: str, file_path: str):
    t = tracks.get(track_id)
    if not t:
        return
    hires = t.get("artwork_hires_url")
    if not hires:
        _update(track_id, artwork_status="searching")
        artist, title = t["artist"], t["title"]
        results = _itunes_search(f"{_normalize(artist)} {title}", limit=1)
        if not results:
            primary = artist.split(",")[0].strip()
            if primary != artist:
                results = _itunes_search(f"{primary} {title}", limit=1)
        if results:
            r = results[0]
            hires = r["url_hires"]
            _update(track_id, artwork_url=hires, artwork_hires_url=hires, artwork_status="found")
        else:
            _update(track_id, artwork_status="failed")
            return
    if _embed_artwork(file_path, hires):
        _update(track_id, artwork_status="embedded")
    else:
        _update(track_id, artwork_status="failed")


def _do_download(track_id: str):
    t = tracks.get(track_id)
    if not t:
        return

    folder = Path(DOWNLOAD_FOLDER) / current_playlist
    folder.mkdir(parents=True, exist_ok=True)

    file_name = _safe(f"{t['num']:03d} - {t['artist']} - {t['title']}") + ".m4a"
    out = folder / file_name

    # Path used by the HTML audio player — quoted so "#", "&", spaces etc. survive
    url_path = "/downloads/" + quote(f"{current_playlist}/{file_name}")

    if out.exists():
        _update(track_id, status="done", progress=100, file_path=str(out), url_path=url_path)
        _do_artwork(track_id, str(out))
        return

    _update(track_id, status="searching")
    yt_url = t.get("yt_url") or _yt_search(t["artist"], t["title"])
    if not yt_url:
        _update(track_id, status="failed", error="Not found on YouTube")
        return

    _update(track_id, status="downloading", progress=0, yt_url_used=yt_url)
    ok = _yt_download(yt_url, str(out), lambda p: _update(track_id, progress=p))
    if not ok:
        _update(track_id, status="failed", error="Download failed")
        return

    _embed_tags(str(out), t)
    _update(track_id, status="tagging", progress=100, file_path=str(out))
    _do_artwork(track_id, str(out))
    _update(track_id, status="done", url_path=url_path)


async def _worker():
    while True:
        tid = await download_queue.get()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_download, tid)
        download_queue.task_done()
        if download_queue.empty():
            await _broadcast({"type": "all_done"})


def _make_track_row(i: int, title: str, artist: str, album: str, **extra) -> dict:
    """The one place the track schema is defined — used by both CSV upload and
    Spotify import so the two paths can never drift out of sync."""
    row = {
        "id": str(i), "num": i,
        "title": title, "artist": artist, "album": album,
        "status": "waiting", "progress": 0,
        "yt_url": "", "yt_url_used": "", "file_path": "", "url_path": "", "error": "",
        "artwork_status": "none", "artwork_url": "", "artwork_hires_url": "",
        "duration_ms": 0, "isrc": "", "track_uri": "", "album_uri": "",
    }
    row.update(extra)
    return row

# ── Spotify import (optional) ─────────────────────────────────────────────────
# OAuth Authorization Code flow. Tokens live in memory only (like everything
# else in this app) — restarting the server just means reconnecting.
# Needs your own free app registered at developer.spotify.com — see README.
# Note: Spotify requires the app-owner account to have an active Premium
# subscription before ANY Web API call succeeds while the app is in
# Development Mode. If that's not you, just use the CSV import instead —
# it needs no Spotify app or Premium at all.

def _spotify_refresh_token():
    resp = http.post("https://accounts.spotify.com/api/token", data={
        "grant_type":    "refresh_token",
        "refresh_token": spotify_auth["refresh_token"],
        "client_id":     SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    spotify_auth["access_token"] = data["access_token"]
    spotify_auth["expires_at"]   = time.time() + data.get("expires_in", 3600) - 30
    if data.get("refresh_token"):  # Spotify doesn't always rotate it
        spotify_auth["refresh_token"] = data["refresh_token"]


def _spotify_get(url: str, params: Optional[dict] = None) -> dict:
    if not spotify_auth.get("refresh_token"):
        raise HTTPException(401, "Not connected to Spotify")
    if time.time() >= spotify_auth.get("expires_at", 0):
        _spotify_refresh_token()
    headers = {"Authorization": f"Bearer {spotify_auth['access_token']}"}
    r = http.get(url, headers=headers, params=params, timeout=15)
    if r.status_code == 401:  # token revoked/expired early — refresh once and retry
        _spotify_refresh_token()
        headers = {"Authorization": f"Bearer {spotify_auth['access_token']}"}
        r = http.get(url, headers=headers, params=params, timeout=15)
    if r.status_code >= 400:
        # Surface Spotify's own reason (e.g. "Insufficient client scope", or a
        # Development Mode restriction) instead of a bare "403 Forbidden".
        try:
            reason = r.json().get("error", {}).get("message") or r.text
        except Exception:
            reason = r.text
        raise HTTPException(r.status_code, f"Spotify API error: {reason}")
    return r.json()


def _spotify_all_playlists() -> list:
    playlists = []
    url, params = "https://api.spotify.com/v1/me/playlists", {"limit": 50}
    while url:
        data = _spotify_get(url, params)
        for p in data.get("items", []):
            if not p:  # Spotify can return null entries for deleted playlists
                continue
            images = p.get("images") or []
            playlists.append({
                "id":           p["id"],
                "name":         p["name"],
                "tracks_total": p.get("tracks", {}).get("total", 0),
                "image_url":    images[0]["url"] if images else "",
            })
        url, params = data.get("next"), None  # "next" already carries its own query params
    return playlists


def _spotify_playlist_tracks(playlist_id: str) -> list:
    fields = ("next,items(track(name,artists(name),album(name,uri),"
              "duration_ms,external_ids,uri,is_local))")
    url    = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {"limit": 100, "fields": fields}
    rows, i = [], 0
    while url:
        data = _spotify_get(url, params)
        for item in data.get("items", []):
            tr = item.get("track")
            # Removed tracks come back as null; local files carry no useful metadata
            if not tr or tr.get("is_local"):
                continue
            title = (tr.get("name") or "").strip()
            if not title:
                continue
            i += 1
            artist = ", ".join(a["name"] for a in tr.get("artists", []))
            album  = (tr.get("album") or {}).get("name", "")
            rows.append(_make_track_row(
                i, title, artist, album,
                duration_ms=tr.get("duration_ms", 0),
                isrc=(tr.get("external_ids") or {}).get("isrc", ""),
                track_uri=tr.get("uri", ""),
                album_uri=(tr.get("album") or {}).get("uri", ""),
            ))
        url, params = data.get("next"), None
    return rows

# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop, download_queue
    main_loop = asyncio.get_event_loop()
    download_queue = asyncio.Queue()
    asyncio.create_task(_worker())
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/downloads", StaticFiles(directory=DOWNLOAD_FOLDER), name="downloads")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return HTMLResponse((Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8"))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    await websocket.send_text(json.dumps({
        "type": "state", "tracks": list(tracks.values()), "playlist": current_playlist,
    }))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    global tracks, current_playlist
    content = (await file.read()).decode("utf-8-sig")
    current_playlist = Path(file.filename).stem
    rows = []
    for i, row in enumerate(csv.DictReader(io.StringIO(content)), 1):
        title  = row.get("Track Name",    "").strip().replace(";", ",")
        artist = row.get("Artist Name(s)","").strip().replace(";", ",")
        album  = row.get("Album Name",    "").strip().replace(";", ",")
        if not title:
            continue
        rows.append(_make_track_row(i, title, artist, album))
    tracks = {r["id"]: r for r in rows}
    await _broadcast({"type": "state", "tracks": rows, "playlist": current_playlist})
    return JSONResponse({"count": len(rows), "playlist": current_playlist})


@app.post("/api/start-download")
async def start_download():
    for tid, t in tracks.items():
        if t["status"] in ("waiting", "failed"):
            await download_queue.put(tid)
    return JSONResponse({"queued": download_queue.qsize()})


@app.post("/api/download/{track_id}")
async def download_one(track_id: str, body: Optional[dict] = Body(None)):
    if track_id not in tracks:
        raise HTTPException(404)
    # _do_download skips anything already on disk, so a re-download has to clear the file first
    if body and body.get("force"):
        _purge_file(track_id)
    tracks[track_id]["status"] = "waiting"
    await download_queue.put(track_id)
    await _broadcast({"type": "track_update", "track": tracks[track_id]})
    return JSONResponse({"status": "ok"})


@app.post("/api/set-yt-url/{track_id}")
async def set_yt_url(track_id: str, body: dict = Body(...)):
    if track_id not in tracks:
        raise HTTPException(404)
    tracks[track_id]["yt_url"] = body.get("url", "")
    await _broadcast({"type": "track_update", "track": tracks[track_id]})
    return JSONResponse({"status": "ok"})


@app.post("/api/set-artwork/{track_id}")
async def set_artwork(track_id: str, body: dict = Body(...)):
    if track_id not in tracks:
        raise HTTPException(404)
    url   = body.get("url", "")
    hires = url.replace("100x100bb", "3000x3000bb") if "100x100bb" in url else url
    tracks[track_id].update(artwork_url=hires, artwork_hires_url=hires, artwork_status="found")
    fp = tracks[track_id].get("file_path", "")
    if fp and os.path.exists(fp):
        ok = await asyncio.get_event_loop().run_in_executor(None, _embed_artwork, fp, hires)
        if ok:
            tracks[track_id]["artwork_status"] = "embedded"
    await _broadcast({"type": "track_update", "track": tracks[track_id]})
    return JSONResponse({"status": "ok"})


@app.post("/api/search-artwork")
async def search_artwork_api(body: dict = Body(...)):
    query = body.get("query", "")
    if not query:
        raise HTTPException(400, "query required")
    results = await asyncio.get_event_loop().run_in_executor(None, _itunes_search, query, 6)
    return JSONResponse({"results": results})


@app.get("/api/spotify/login")
async def spotify_login():
    global _spotify_oauth_state
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise HTTPException(500, "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env (see .env.example)")
    _spotify_oauth_state = secrets.token_urlsafe(16)
    params = {
        "client_id":     SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "scope":         SPOTIFY_SCOPES,
        "state":         _spotify_oauth_state,
    }
    return RedirectResponse("https://accounts.spotify.com/authorize?" + urlencode(params))


@app.get("/api/spotify/callback")
async def spotify_callback(code: str = "", state: str = "", error: str = ""):
    # state must match what /login handed out, or this isn't the redirect we started
    if error or not code or not state or state != _spotify_oauth_state:
        return RedirectResponse("/?spotify=error")
    resp = http.post("https://accounts.spotify.com/api/token", data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "client_id":     SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }, timeout=15)
    if resp.status_code != 200:
        return RedirectResponse("/?spotify=error")
    data = resp.json()
    spotify_auth.update(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", spotify_auth.get("refresh_token", "")),
        expires_at=time.time() + data.get("expires_in", 3600) - 30,
    )
    return RedirectResponse("/?spotify=connected")


@app.get("/api/spotify/status")
async def spotify_status():
    return JSONResponse({"connected": bool(spotify_auth.get("refresh_token"))})


@app.get("/api/spotify/playlists")
async def spotify_playlists():
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, _spotify_all_playlists)
        return JSONResponse({"playlists": data})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Spotify error: {e}")


@app.post("/api/spotify/import/{playlist_id}")
async def spotify_import(playlist_id: str, body: dict = Body(...)):
    global tracks, current_playlist
    try:
        rows = await asyncio.get_event_loop().run_in_executor(None, _spotify_playlist_tracks, playlist_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Spotify error: {e}")
    current_playlist = _safe(body.get("name", "").strip()) or playlist_id
    tracks = {r["id"]: r for r in rows}
    await _broadcast({"type": "state", "tracks": rows, "playlist": current_playlist})
    return JSONResponse({"count": len(rows), "playlist": current_playlist})


@app.delete("/api/track/{track_id}")
async def delete_track(track_id: str):
    """Deletes a track from the UI list and permanently deletes its downloaded file."""
    if track_id in tracks:
        _purge_file(track_id)
        del tracks[track_id]
        await _broadcast({"type": "state", "tracks": list(tracks.values()), "playlist": current_playlist})
    return JSONResponse({"status": "deleted"})


@app.post("/api/shutdown")
async def shutdown_server():
    """Asks uvicorn to stop, then hard-exits if anything is still hanging on."""
    async def _stop():
        await asyncio.sleep(0.5)          # let this response reach the browser
        if server is not None:
            server.should_exit = True
        await asyncio.sleep(4)            # grace period for an in-flight download
        os._exit(0)

    asyncio.create_task(_stop())
    return JSONResponse({"status": "shutting down"})


if __name__ == "__main__":
    # The app object is passed directly (not "app:app") so this module isn't
    # re-imported — that keeps the `server` global visible to /api/shutdown.
    server = uvicorn.Server(uvicorn.Config(
        app, host=HOST, port=PORT, reload=False, timeout_graceful_shutdown=3,
    ))
    server.run()
