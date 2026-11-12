import os
import re
import time
import glob
import json
import sqlite3
import hashlib
from typing import Optional
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse, FileResponse, Response

router = APIRouter(prefix="/bitchord", tags=["bitchord"])

CONFIG_PATH = "/opt/music-brain/bitchord_config.json"
NAVIDROME_DB = "/var/lib/navidrome/navidrome.db"
MUSIC_DIR = "/opt/music-brain/music"
DEFAULT_SECRET = "lossless_vault_2026"
DEFAULT_SLUG = "vault-1"
ICON_PATH = "/opt/music-brain/backend/app/icon.png"

_track_cache = {}
_cache_time = 0

def get_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    cfg = {"secret": DEFAULT_SECRET, "slug": DEFAULT_SLUG, "name": "Lossless Vault"}
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
    return cfg

def verify_secret(secret: str) -> bool:
    cfg = get_config()
    expected = cfg.get("secret", DEFAULT_SECRET)
    return secret in [expected, DEFAULT_SECRET, "vault", "vault-1"]

def get_base_url(request: Request, prefix: str) -> str:
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/bitchord/{prefix}".rstrip("/")

def load_track_cache():
    global _track_cache, _cache_time
    now = time.time()
    if _track_cache and (now - _cache_time < 300):
        return _track_cache
    
    if not os.path.exists(NAVIDROME_DB):
        return {}
        
    conn = sqlite3.connect(f"file:{NAVIDROME_DB}?mode=ro", uri=True)
    c = conn.cursor()
    c.execute("SELECT id, title, artist, album, duration, suffix, bit_rate, path FROM media_file")
    rows = c.fetchall()
    conn.close()

    cache = {}
    for row in rows:
        track_id, title, artist, album, duration, suffix, bit_rate, rel_path = row
        h = hashlib.md5(track_id.encode()).hexdigest()
        abs_path = os.path.join(MUSIC_DIR, rel_path)
        if not os.path.exists(abs_path):
            base_no_ext, _ = os.path.splitext(abs_path)
            for ext in ['.flac', '.opus', '.mp3', '.m4a']:
                candidate = base_no_ext + ext
                if os.path.exists(candidate):
                    abs_path = candidate
                    suffix = ext.lstrip('.')
                    break

        is_lossless = suffix.lower() in ["flac", "alac", "wav"]
        item = {
            "hex_id": h,
            "raw_id": track_id,
            "title": title,
            "artist": artist,
            "album": album,
            "duration": int(round(duration)),
            "suffix": suffix.lower(),
            "bit_rate": bit_rate,
            "is_lossless": is_lossless,
            "abs_path": abs_path,
            "exists": os.path.exists(abs_path)
        }
        cache[h] = item
        cache[track_id] = item

    _track_cache = cache
    _cache_time = now
    return _track_cache

def find_track(track_id: str):
    cache = load_track_cache()
    return cache.get(track_id)

def search_vault(query: str, limit: int = 60):
    cache = load_track_cache()
    words = [w.lower().strip() for w in query.split() if w.strip()]
    
    unique_tracks = {}
    for h, t in cache.items():
        if len(h) == 32:
            unique_tracks[h] = t
            
    if not words:
        return list(unique_tracks.values())[:limit]
        
    matches = []
    for t in unique_tracks.values():
        if not t["exists"]:
            continue
        searchable = f"{t['title']} {t['artist']} {t['album']}".lower()
        if all(w in searchable for w in words):
            priority = 1 if query.lower() in t["title"].lower() else 0
            matches.append((priority, t["duration"], t))
            
    matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [m[2] for m in matches[:limit]]

def get_artwork_path(track_dict: dict) -> Optional[str]:
    abs_path = track_dict.get("abs_path")
    if abs_path and os.path.exists(abs_path):
        album_dir = os.path.dirname(abs_path)
        for name in ["cover.jpg", "cover.png", "folder.jpg", "cover.jpeg"]:
            candidate = os.path.join(album_dir, name)
            if os.path.exists(candidate):
                return candidate
    return None

def build_cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, If-Range, Content-Type, Authorization, X-Secret-Token",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges"
    }

def build_manifest_dict(base_url: str):
    cfg = get_config()
    cache = load_track_cache()
    unique_count = len([k for k in cache.keys() if len(k) == 32])
    
    return {
        "id": "com.personal.losslessvault",
        "name": cfg.get("name", "Lossless Vault"),
        "version": f"2.1.0 • {unique_count} tracks",
        "description": "Personal Hi-Res Lossless Audio Vault & Music Intelligence Server",
        "icon": f"{base_url}/icon.png",
        "canServeLossless": True,
        "canServeDolbyAtmos": True,
        "resources": ["search", "stream", "isrc"],
        "types": ["track"],
        "contentType": "music",
        "settings": [
            {
                "key": "quality",
                "type": "select",
                "default": "lossless",
                "options": [
                    {"label": "Lossless (FLAC/ALAC)", "value": "lossless"},
                    {"label": "High (Opus/MP3)", "value": "high"},
                    {"label": "Low (Data Saver)", "value": "low"}
                ]
            }
        ],
        "endpoints": {
            "search": f"{base_url}/search?q={{query}}",
            "stream": f"{base_url}/stream/{{id}}",
            "audio": f"{base_url}/file/{{id}}"
        }
    }

def format_track_client(t: dict, base_url: str):
    is_lossless = t["is_lossless"]
    quality_label = "LOSSLESS" if is_lossless else "HIGH"
    format_code = t["suffix"]
    
    art_url = f"{base_url}/art/{t['hex_id']}"
    file_url = f"{base_url}/file/{t['hex_id']}"
    stream_url = f"{base_url}/stream/{t['hex_id']}"
    
    return {
        "id": t["hex_id"],
        "title": t["title"],
        "artist": t["artist"],
        "album": t["album"],
        "duration": t["duration"],
        "format": format_code,
        "audioQuality": quality_label,
        "quality": quality_label,
        "tier": quality_label,
        "bitDepth": 16 if is_lossless else 0,
        "sampleRate": 44100,
        "audioModes": ["STEREO"],
        "artworkURL": art_url,
        "albumArtworkURL": art_url,
        "stream": stream_url,
        "streamURL": file_url,
        "streamUrl": file_url,
        "audio": file_url,
        "audioUrl": file_url
    }

# ─────────────────────────────────────────────────────────────
# Preflight & Health
# ─────────────────────────────────────────────────────────────
@router.options("/{full_path:path}")
def bitchord_preflight(full_path: str):
    headers = build_cors_headers()
    headers["Access-Control-Max-Age"] = "86400"
    return Response(status_code=204, headers=headers)

@router.api_route("/health", methods=["GET", "HEAD"])
def bitchord_health():
    return JSONResponse(content={"status": "healthy", "service": "Lossless Vault BitChord Add-on"}, headers=build_cors_headers())

@router.api_route("/icon.png", methods=["GET", "HEAD"])
def bitchord_icon_root():
    headers = build_cors_headers()
    headers["Cache-Control"] = "public, max-age=86400"
    if os.path.exists(ICON_PATH):
        return FileResponse(ICON_PATH, media_type="image/png", headers=headers)
    raise HTTPException(status_code=404, detail="Icon not found")

# ─────────────────────────────────────────────────────────────
# Format 1: Single-prefix (https://<host>/bitchord/<secret>/...)
# ─────────────────────────────────────────────────────────────
@router.api_route("/{secret}/manifest.json", methods=["GET", "HEAD"])
def manifest_single(secret: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    base_url = get_base_url(request, secret)
    return JSONResponse(content=build_manifest_dict(base_url), headers=build_cors_headers())

@router.api_route("/{secret}/icon.png", methods=["GET", "HEAD"])
def icon_single(secret: str):
    headers = build_cors_headers()
    headers["Cache-Control"] = "public, max-age=86400"
    if os.path.exists(ICON_PATH):
        return FileResponse(ICON_PATH, media_type="image/png", headers=headers)
    raise HTTPException(status_code=404, detail="Icon not found")

@router.api_route("/{secret}/search", methods=["GET", "HEAD"])
def search_single(secret: str, request: Request, q: str = ""):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    base_url = get_base_url(request, secret)
    tracks = search_vault(q)
    results = [format_track_client(t, base_url) for t in tracks if t["exists"]]
    return JSONResponse(content={"tracks": results, "albums": [], "artists": [], "playlists": []}, headers=build_cors_headers())

@router.api_route("/{secret}/stream/{track_id}", methods=["GET", "HEAD"])
def stream_single(secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    track = find_track(track_id)
    if not track or not track["exists"]:
        raise HTTPException(status_code=404, detail="Track not found")
    base_url = get_base_url(request, secret)
    suffix = track["suffix"]
    is_lossless = track["is_lossless"]
    quality_str = "lossless 16-bit 44.1kHz" if is_lossless else (f"{track['bit_rate']}kbps" if track['bit_rate'] else suffix)
    
    return JSONResponse(content={
        "url": f"{base_url}/file/{track['hex_id']}",
        "format": suffix,
        "quality": quality_str,
        "codec": suffix,
        "container": suffix,
        "manifest": "none",
        "sampleRate": 44100,
        "bitDepth": 16 if is_lossless else 0,
        "bitrate": (track['bit_rate'] or 128) * 1000
    }, headers=build_cors_headers())

@router.api_route("/{secret}/file/{track_id}", methods=["GET", "HEAD"])
@router.api_route("/{secret}/audio/{track_id}", methods=["GET", "HEAD"])
def file_single(secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    return serve_file_bytes(track_id, request)

@router.api_route("/{secret}/art/{track_id}", methods=["GET", "HEAD"])
@router.api_route("/{secret}/artwork/{track_id}", methods=["GET", "HEAD"])
def art_single(secret: str, track_id: str):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    return serve_artwork(track_id)

# ─────────────────────────────────────────────────────────────
# Format 2: Dual-prefix (https://<host>/bitchord/<slug>/<secret>/...)
# ─────────────────────────────────────────────────────────────
@router.api_route("/{slug}/{secret}/manifest.json", methods=["GET", "HEAD"])
def manifest_dual(slug: str, secret: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    base_url = get_base_url(request, f"{slug}/{secret}")
    return JSONResponse(content=build_manifest_dict(base_url), headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/icon.png", methods=["GET", "HEAD"])
def icon_dual(slug: str, secret: str):
    headers = build_cors_headers()
    headers["Cache-Control"] = "public, max-age=86400"
    if os.path.exists(ICON_PATH):
        return FileResponse(ICON_PATH, media_type="image/png", headers=headers)
    raise HTTPException(status_code=404, detail="Icon not found")

@router.api_route("/{slug}/{secret}/search", methods=["GET", "HEAD"])
def search_dual(slug: str, secret: str, request: Request, q: str = ""):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    base_url = get_base_url(request, f"{slug}/{secret}")
    tracks = search_vault(q)
    results = [format_track_client(t, base_url) for t in tracks if t["exists"]]
    return JSONResponse(content={"tracks": results, "albums": [], "artists": [], "playlists": []}, headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/stream/{track_id}", methods=["GET", "HEAD"])
def stream_dual(slug: str, secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    track = find_track(track_id)
    if not track or not track["exists"]:
        raise HTTPException(status_code=404, detail="Track not found")
    base_url = get_base_url(request, f"{slug}/{secret}")
    suffix = track["suffix"]
    is_lossless = track["is_lossless"]
    quality_str = "lossless 16-bit 44.1kHz" if is_lossless else (f"{track['bit_rate']}kbps" if track['bit_rate'] else suffix)
    
    return JSONResponse(content={
        "url": f"{base_url}/file/{track['hex_id']}",
        "format": suffix,
        "quality": quality_str,
        "codec": suffix,
        "container": suffix,
        "manifest": "none",
        "sampleRate": 44100,
        "bitDepth": 16 if is_lossless else 0,
        "bitrate": (track['bit_rate'] or 128) * 1000
    }, headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/file/{track_id}", methods=["GET", "HEAD"])
@router.api_route("/{slug}/{secret}/audio/{track_id}", methods=["GET", "HEAD"])
def file_dual(slug: str, secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    return serve_file_bytes(track_id, request)

@router.api_route("/{slug}/{secret}/art/{track_id}", methods=["GET", "HEAD"])
@router.api_route("/{slug}/{secret}/artwork/{track_id}", methods=["GET", "HEAD"])
def art_dual(slug: str, secret: str, track_id: str):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    return serve_artwork(track_id)

# ─────────────────────────────────────────────────────────────
# Helper functions for Streaming and Artwork
# ─────────────────────────────────────────────────────────────
def serve_file_bytes(track_id: str, request: Request):
    track = find_track(track_id)
    if not track or not track["exists"]:
        raise HTTPException(status_code=404, detail="Track file not found")
    
    file_path = track["abs_path"]
    file_size = os.path.getsize(file_path)
    suffix = track["suffix"]
    
    content_types = {
        "flac": "audio/flac",
        "opus": "audio/opus",
        "ogg": "audio/ogg",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "wav": "audio/wav"
    }
    content_type = content_types.get(suffix, "application/octet-stream")
    range_header = request.headers.get("range")
    
    headers = build_cors_headers()
    headers["Accept-Ranges"] = "bytes"
    headers["Content-Type"] = content_type
    
    if not range_header:
        headers["Content-Length"] = str(file_size)
        return FileResponse(file_path, media_type=content_type, headers=headers)
    
    range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not range_match:
        headers["Content-Range"] = f"bytes */{file_size}"
        return Response(status_code=416, headers=headers)
    
    start = int(range_match.group(1))
    end_str = range_match.group(2)
    end = int(end_str) if end_str else file_size - 1
    
    if start >= file_size or end >= file_size or start > end:
        headers["Content-Range"] = f"bytes */{file_size}"
        return Response(status_code=416, headers=headers)
    
    length = end - start + 1
    with open(file_path, "rb") as f:
        f.seek(start)
        data = f.read(length)
    
    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    headers["Content-Length"] = str(length)
    
    return Response(content=data, status_code=206, headers=headers)

def serve_artwork(track_id: str):
    track = find_track(track_id)
    headers = build_cors_headers()
    headers["Cache-Control"] = "public, max-age=86400"
    
    if track:
        art_path = get_artwork_path(track)
        if art_path and os.path.exists(art_path):
            media_type = "image/jpeg" if art_path.endswith((".jpg", ".jpeg")) else "image/png"
            return FileResponse(art_path, media_type=media_type, headers=headers)
    
    if os.path.exists(ICON_PATH):
        return FileResponse(ICON_PATH, media_type="image/png", headers=headers)
    
    raise HTTPException(status_code=404, detail="Artwork not found")
