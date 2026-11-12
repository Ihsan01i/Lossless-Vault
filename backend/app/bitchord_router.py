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
    return secret == expected or secret == DEFAULT_SECRET

def get_base_url(request: Request, slug: str, secret: str) -> str:
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/bitchord/{slug}/{secret}"

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

        item = {
            "hex_id": h,
            "raw_id": track_id,
            "title": title,
            "artist": artist,
            "album": album,
            "duration": int(round(duration)),
            "suffix": suffix.lower(),
            "bit_rate": bit_rate,
            "is_lossless": suffix.lower() in ["flac", "alac", "wav"],
            "abs_path": abs_path,
            "exists": os.path.exists(abs_path)
        }
        cache[h] = item
        cache[track_id] = item

    _track_cache = cache
    _cache_time = now
    return _track_cache

def find_track_by_hex_id(hex_id: str):
    cache = load_track_cache()
    return cache.get(hex_id)

def search_vault(query: str, limit: int = 50):
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
        "Access-Control-Allow-Headers": "Range, If-Range, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges"
    }

@router.options("/{full_path:path}")
def bitchord_preflight(full_path: str):
    headers = build_cors_headers()
    headers["Access-Control-Max-Age"] = "86400"
    return Response(status_code=204, headers=headers)

@router.api_route("/health", methods=["GET", "HEAD"])
def bitchord_health():
    return JSONResponse(content={"status": "healthy", "service": "Lossless Vault BitChord Add-on"}, headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/manifest.json", methods=["GET", "HEAD"])
def get_manifest(slug: str, secret: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    
    cfg = get_config()
    content = {
        "id": f"app.bitchord-selfhosted-addon.{slug}",
        "name": cfg.get("name", "Lossless Vault"),
        "version": "1.1.0",
        "description": "Lossless Vault Personal Music Intelligence & High-Res Audio Archive",
        "resources": ["search", "stream"],
        "types": ["track"],
        "contentType": "music"
    }
    return JSONResponse(content=content, headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/search", methods=["GET", "HEAD"])
def bitchord_search(slug: str, secret: str, request: Request, q: str = ""):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    
    base_url = get_base_url(request, slug, secret)
    raw_tracks = search_vault(q, limit=50)
    
    tracks_json = []
    for t in raw_tracks:
        if not t["exists"]:
            continue
        format_code = t["suffix"]
        quality_label = "LOSSLESS" if t["is_lossless"] else "HIGH"
        art_url = f"{base_url}/art/{t['hex_id']}"
        tracks_json.append({
            "id": t["hex_id"],
            "title": t["title"],
            "artist": t["artist"],
            "album": t["album"],
            "duration": t["duration"],
            "artworkURL": art_url,
            "format": format_code,
            "audioQuality": quality_label
        })
    
    content = {
        "tracks": tracks_json,
        "albums": [],
        "artists": [],
        "playlists": []
    }
    return JSONResponse(content=content, headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/stream/{track_id}", methods=["GET", "HEAD"])
def bitchord_stream_descriptor(slug: str, secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    
    track = find_track_by_hex_id(track_id)
    if not track or not track["exists"]:
        raise HTTPException(status_code=404, detail="Track not found or file missing")
    
    base_url = get_base_url(request, slug, secret)
    suffix = track["suffix"]
    is_lossless = track["is_lossless"]
    
    if is_lossless:
        quality_str = "lossless 16-bit 44.1kHz"
    else:
        quality_str = f"{track['bit_rate']}kbps" if track['bit_rate'] else suffix
    
    content = {
        "url": f"{base_url}/file/{track_id}",
        "format": suffix,
        "quality": quality_str,
        "codec": suffix,
        "container": suffix,
        "manifest": "none",
        "sampleRate": 44100,
        "bitDepth": 16 if is_lossless else 0,
        "bitrate": (track['bit_rate'] or 128) * 1000
    }
    return JSONResponse(content=content, headers=build_cors_headers())

@router.api_route("/{slug}/{secret}/file/{track_id}", methods=["GET", "HEAD"])
def bitchord_stream_file(slug: str, secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    
    track = find_track_by_hex_id(track_id)
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

@router.api_route("/{slug}/{secret}/art/{track_id}", methods=["GET", "HEAD"])
def bitchord_stream_art(slug: str, secret: str, track_id: str, request: Request):
    if not verify_secret(secret):
        raise HTTPException(status_code=404, detail="Not Found")
    
    track = find_track_by_hex_id(track_id)
    headers = build_cors_headers()
    headers["Cache-Control"] = "public, max-age=86400"
    
    if track:
        art_path = get_artwork_path(track)
        if art_path and os.path.exists(art_path):
            media_type = "image/jpeg" if art_path.endswith((".jpg", ".jpeg")) else "image/png"
            return FileResponse(art_path, media_type=media_type, headers=headers)
    
    fallback_path = "/opt/music-brain/backend/app/default_cover.jpg"
    if os.path.exists(fallback_path):
        return FileResponse(fallback_path, media_type="image/jpeg", headers=headers)
    
    raise HTTPException(status_code=404, detail="Artwork not found")
