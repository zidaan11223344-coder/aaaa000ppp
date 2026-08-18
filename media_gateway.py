"""بوابة Flask اختيارية لـ PythonAnywhere.

تخدم الصور المولدة من generated_gifts، وتستخرج ملف الصوت إلى media_cache عند طلب
/resolve. لا تحتوي على أسرار؛ استخدم media_gateway_token في config.json.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory
import yt_dlp

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
MEDIA_DIR = ROOT / "media_cache"
GIFTS_DIR = ROOT / "generated_gifts"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
GIFTS_DIR.mkdir(parents=True, exist_ok=True)

try:
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
except Exception:
    CONFIG = {}

TOKEN = str(CONFIG.get("media_gateway_token") or os.environ.get("MEDIA_GATEWAY_TOKEN", "")).strip()
PUBLIC_BASE = str(CONFIG.get("media_public_base_url") or os.environ.get("MEDIA_PUBLIC_BASE_URL", "")).rstrip("/")
LIMIT = max(1, int(CONFIG.get("music_hourly_limit", 100)))
REQUESTS = deque()

class SilentLogger:
    def debug(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        pass


def authorized():
    if not TOKEN:
        return True
    supplied = request.headers.get("X-Media-Token") or request.args.get("token", "")
    return supplied == TOKEN


def clean_old_files():
    cutoff = time.time() - 3600
    for directory in (MEDIA_DIR, GIFTS_DIR):
        for path in directory.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def allowed_request():
    now = time.monotonic()
    while REQUESTS and now - REQUESTS[0] >= 3600:
        REQUESTS.popleft()
    if len(REQUESTS) >= LIMIT:
        return False
    REQUESTS.append(now)
    return True


def is_direct_url(value):
    try:
        host = urlparse(value).hostname or ""
        return host.endswith("youtube.com") or host.endswith("youtu.be") or host.endswith("tiktok.com")
    except Exception:
        return False


def resolve_target(query, kind, direct_url):
    if direct_url and is_direct_url(direct_url):
        return direct_url
    if kind == "youtube":
        return f"ytsearch1:{query}"
    # TikTok name search requires an explicit provider because TikTok does not expose
    # a stable unauthenticated search endpoint for arbitrary server automation.
    return None


def download_audio(query, kind, direct_url):
    target = resolve_target(query, kind, direct_url)
    if not target:
        return None, "tiktok_name_search_needs_provider"
    cookie_files = CONFIG.get("youtube_cookie_files" if kind == "youtube" else "tiktok_cookie_files", [])
    if isinstance(cookie_files, str):
        cookie_files = [cookie_files]
    cookie_paths = []
    for cookie in cookie_files or []:
        path = Path(str(cookie))
        if not path.is_absolute():
            path = ROOT / path
        if path.exists() and path.is_file():
            cookie_paths.append(str(path))

    # جرّب عملاء YouTube مختلفة؛ بعض الخوادم ترفض web بينما تسمح android أو embedded.
    clients = [None, "android_vr", "web_embedded", "web_safari"] if kind == "youtube" else [None]
    last_error = "provider_failed"
    for cookie_file in [None] + cookie_paths:
        for client in clients:
            stem = uuid.uuid4().hex
            options = {
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
                "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": str(MEDIA_DIR / f"{stem}.%(ext)s"),
                "socket_timeout": 20,
                "retries": 1,
                "fragment_retries": 1,
                "extractor_retries": 1,
                "file_access_retries": 1,
                "logger": SilentLogger(),
        "no_color": True,
        "cachedir": False,
        "nocheckcertificate": True,
        "max_filesize": 50 * 1024 * 1024,
    }
            if kind == "youtube" and client:
                options["extractor_args"] = {"youtube": {"player_client": [client]}}
            if cookie_file:
                options["cookiefile"] = cookie_file
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(target, download=True)
                entry = info
                if info and info.get("entries"):
                    entry = next((x for x in info["entries"] if x), None)
                files = sorted(MEDIA_DIR.glob(f"{stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not entry or not files:
                    last_error = "no_audio_file"
                    continue
                file_name = files[0].name
                return {
                    "audio_url": f"__PUBLIC__/media/{file_name}",
                    "title": entry.get("title") or "المقطع",
                    "artist": entry.get("uploader") or entry.get("artist") or ("TikTok" if kind == "tiktok" else "YouTube"),
                    "duration_ms": int(float(entry.get("duration") or 0) * 1000),
                    "source_url": entry.get("webpage_url") or direct_url or "",
                }, None
            except Exception as exc:
                last_error = str(exc)
                # لا نسجل traceback ولا نرسل تفاصيل cookies أو الشبكة للمستخدم؛ ننتقل للعميل التالي.
                continue
    return None, "youtube_provider_blocked" if kind == "youtube" else "provider_failed"


def create_app():
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "media-gateway"})

    @app.get("/gifts/<path:name>")
    def gifts(name):
        return send_from_directory(GIFTS_DIR, name, conditional=True)

    @app.get("/media/<path:name>")
    def media(name):
        return send_from_directory(MEDIA_DIR, name, conditional=True)

    @app.get("/resolve")
    def resolve():
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if not allowed_request():
            return jsonify({"ok": False, "error": "hourly_limit"}), 429
        query = (request.args.get("q") or "").strip()
        kind = (request.args.get("kind") or "youtube").strip().lower()
        direct_url = (request.args.get("url") or "").strip()
        clean_old_files()
        result, error = download_audio(query, kind, direct_url)
        if not result:
            return jsonify({"ok": False, "error": "provider_failed", "detail": error if error == "tiktok_name_search_needs_provider" else "تعذر استخراج الصوت"}), 502
        base = PUBLIC_BASE or request.url_root.rstrip("/")
        result["audio_url"] = result["audio_url"].replace("__PUBLIC__", base)
        return jsonify({"ok": True, "track": result})

    return app


application = create_app()
