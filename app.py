#!/usr/bin/env python3
"""Kraken — search The Pirate Bay, download to this machine via Transmission."""
import hmac
import json
import os
import time
import threading
import requests
from flask import Flask, request, jsonify, render_template, session, redirect, url_for

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(BASE, "config.json")))

app = Flask(__name__)
app.secret_key = CONFIG.get("secret_key")

# --- login rate limiting (in-memory) ---
# Tracks failed attempts per client IP. Progressive lockout: each lockout window
# doubles. State resets on process restart (acceptable for a single-host app).
AUTH_MAX_ATTEMPTS = 5          # failures allowed per window
AUTH_WINDOW = 15 * 60          # seconds
AUTH_LOCKOUT_BASE = 15 * 60    # first lockout duration (seconds)
_lock = threading.Lock()
_failures = {}                 # ip -> list[unix ts]
_lockouts = {}                 # ip -> {"until": ts, "level": int}

APIBASES = [
    "https://apibay.org",
    "https://apibay.qx.is",
]
TR_RPC = "http://127.0.0.1:9091/transmission/rpc"
_session_id = None

CATS = [
    ("0", "All"),
    ("201", "Movies"),
    ("205", "TV Shows"),
    ("202", "Music Video"),
    ("101", "Music"),
    ("301", "Games"),
    ("401", "Applications"),
    ("701", "Books"),
    ("702", "Comics"),
]


def tr_rpc(method, args=None):
    """Talk to Transmission's RPC (handles the session-id dance)."""
    global _session_id
    payload = {"method": method, "arguments": args or {}}
    headers = {}
    if _session_id:
        headers["X-Transmission-Session-Id"] = _session_id
    r = requests.post(TR_RPC, json=payload, headers=headers, timeout=10)
    if r.status_code == 409:
        _session_id = r.headers.get("X-Transmission-Session-Id")
        headers["X-Transmission-Session-Id"] = _session_id
        r = requests.post(TR_RPC, json=payload, headers=headers, timeout=10)
    return r.json()


def human_size(n):
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# Torrent ids we already tried to re-adopt since process start: one walk, no retries.
_TRIED_ADOPT = set()


def _find_named(filename, roots):
    """Exact basename match anywhere under one of the roots, first hit wins."""
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if filename in files:
                return dirpath
    return None


def adopt_orphans(torrents):
    """Self-heal the 'No data found' class of errors.

    The files were downloaded fine — then someone organized them into the
    library, so Transmission's download-dir points at empty space. Search the
    configured roots for the exact filenames; when every file lives in one new
    dir, re-point the torrent there and re-verify instead of erroring forever.
    Data that is genuinely gone is left as an error (correctly).
    """
    fixed = False
    roots = [os.path.expanduser(d) for d in CONFIG.get("download_dirs", [])]
    for t in torrents:
        if not str(t.get("errorString") or "").startswith("No data found"):
            continue
        if t.get("id") in _TRIED_ADOPT:
            continue
        _TRIED_ADOPT.add(t.get("id"))
        files = t.get("files") or []
        if not files:
            continue
        locs = set()
        for f in files:
            hit = _find_named(os.path.basename(f.get("name") or ""), roots)
            if hit is None:
                locs = None
                break
            locs.add(hit)
        if not locs or len(locs) != 1:
            continue
        loc = locs.pop()
        # only adopt when the exact path Transmission will check resolves there
        if not all(os.path.exists(os.path.join(loc, f["name"])) for f in files):
            continue
        if loc != t.get("downloadDir"):
            tr_rpc("torrent-set-location", {"ids": [t["id"]], "location": loc, "move": False})
        tr_rpc("torrent-verify", {"ids": [t["id"]]})
        fixed = True
    return fixed


STATUS_NAMES = {
    0: "Stopped", 1: "Check wait", 2: "Checking", 3: "Download wait",
    4: "Downloading", 5: "Seed wait", 6: "Seeding",
}


def client_ip():
    """Real client IP behind nginx (X-Real-IP is set by the reverse proxy)."""
    return request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "unknown"


def lockout_remaining(ip):
    """Seconds remaining of an active lockout, 0 if none."""
    with _lock:
        rec = _lockouts.get(ip)
        if not rec:
            return 0
        left = rec["until"] - time.time()
        if left <= 0:
            _lockouts.pop(ip, None)
            return 0
        return left


def record_failure(ip):
    """Record a failed attempt; return lockout seconds if a new lockout starts."""
    now = time.time()
    with _lock:
        bucket = [t for t in _failures.get(ip, []) if now - t < AUTH_WINDOW]
        bucket.append(now)
        _failures[ip] = bucket
        if len(bucket) >= AUTH_MAX_ATTEMPTS:
            level = _lockouts.get(ip, {}).get("level", 0) + 1
            duration = AUTH_LOCKOUT_BASE * (2 ** (level - 1))
            _lockouts[ip] = {"until": now + duration, "level": level}
            _failures.pop(ip, None)
            return duration
        return 0


def clear_failures(ip):
    with _lock:
        _failures.pop(ip, None)


@app.before_request
def require_login():
    """Everything except login needs a session."""
    if request.endpoint in ("login", "static") or request.path == "/login":
        return None
    if not session.get("authed"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login"))


@app.get("/login")
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    ip = client_ip()
    left = lockout_remaining(ip)
    if left > 0:
        mins = int(left // 60) + 1
        return render_template("login.html", error=f"Too many attempts. Try again in ~{mins} min."), 429

    pw = (request.form.get("password") or "").strip()
    if pw and hmac.compare_digest(pw, CONFIG.get("password", "")):
        clear_failures(ip)
        session["authed"] = True
        return redirect(url_for("index"))

    # failed attempt
    duration = record_failure(ip)
    if duration:
        mins = int(duration // 60) + 1
        return render_template("login.html", error=f"Too many attempts. Locked for ~{mins} min."), 429
    return render_template("login.html", error="Wrong password")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    return render_template("index.html",
                           cats=CATS,
                           dirs=CONFIG.get("download_dirs", []),
                           default_dir=CONFIG.get("default_dir", ""))


@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "0")
    if not q:
        return jsonify({"results": []})
    errors = []
    for base in APIBASES:
        try:
            r = requests.get(f"{base}/q.php", params={"q": q, "cat": cat}, timeout=15)
            data = r.json()
            if not isinstance(data, list):
                errors.append(f"{base}: non-list response")
                continue
            results = []
            for t in data:
                if not isinstance(t, dict) or "name" not in t:
                    continue
                name = t.get("name", "")
                ih = t.get("info_hash", "")
                if not ih:
                    continue
                results.append({
                    "id": t.get("id"),
                    "name": name,
                    "size": int(t.get("size") or 0),
                    "size_h": human_size(int(t.get("size") or 0)),
                    "seeders": int(t.get("seeders") or 0),
                    "leechers": int(t.get("leechers") or 0),
                    "added": t.get("added"),
                    "category": t.get("category"),
                    "trusted": t.get("status"),
                    "magnet": f"magnet:?xt=urn:btih:{ih}&dn={requests.utils.quote(name)}",
                })
            return jsonify({"source": base, "results": results})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{base}: {e}")
    return jsonify({"error": "; ".join(errors), "results": []}), 502


@app.post("/api/download")
def download():
    body = request.get_json(force=True, silent=True) or {}
    magnet = (body.get("magnet") or "").strip()
    if not magnet:
        return jsonify({"error": "missing magnet"}), 400

    args = {"filename": magnet, "paused": False}
    dl_dir = (body.get("dir") or CONFIG.get("default_dir", "")).strip()
    if dl_dir:
        dl_dir = os.path.expanduser(dl_dir)
        try:
            os.makedirs(dl_dir, exist_ok=True)
        except OSError as e:
            return jsonify({"error": f"cannot create dir {dl_dir}: {e}"}), 400
        args["download-dir"] = dl_dir

    out = tr_rpc("torrent-add", args)
    if out.get("result") != "success":
        return jsonify({"error": out.get("result", "transmission error")}), 502
    added = out.get("arguments", {}).get("torrent-added", {}) or {}
    dup = out.get("arguments", {}).get("torrent-duplicate", {}) or {}
    info = added or dup
    return jsonify({"ok": True, "id": info.get("id"), "name": info.get("name", ""), "dir": dl_dir})


@app.get("/api/status")
def status():
    fields = ["id", "name", "status", "percentDone", "rateDownload",
              "rateUpload", "downloadDir", "errorString", "eta", "error", "files"]
    out = tr_rpc("torrent-get", {"fields": fields})
    if out.get("result") != "success":
        return jsonify({"error": out.get("result", "rpc error"), "torrents": []}), 502
    # data moved after download => re-point it once; if anything healed, re-read
    if adopt_orphans(out.get("arguments", {}).get("torrents", [])):
        out = tr_rpc("torrent-get", {"fields": fields})
        if out.get("result") != "success":
            return jsonify({"error": out.get("result", "rpc error"), "torrents": []}), 502
    torrents = []
    for t in out.get("arguments", {}).get("torrents", []):
        torrents.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "status": STATUS_NAMES.get(t.get("status"), str(t.get("status"))),
            "percent": round((t.get("percentDone") or 0) * 100, 1),
            "down": t.get("rateDownload") or 0,
            "up": t.get("rateUpload") or 0,
            "dir": t.get("downloadDir"),
            "error": t.get("errorString") or "",
            "eta": t.get("eta"),
        })
    return jsonify({"torrents": torrents})


if __name__ == "__main__":
    # Loopback only — nginx (tpb.spica.ooguy.com) terminates TLS and proxies here.
    port = int(CONFIG.get("port", 8098))
    app.run(host="127.0.0.1", port=port, debug=False)