"""Public tracker fallback appended to magnets before torrent-add.

TPB magnets carry no `tr` params, and Transmission's DHT can go stale, so a
few known-good trackers are cheap insurance. `config.json` "trackers" key
overrides DEFAULT_TRACKERS when present.

Pool maintenance (merged from apps/tracker-pool/tracker_pool.py):
maintain_pool() is the single entry point — health-gates the candidate
trackers, publishes the surviving list to config.json (in-memory + file)
and to active torrents, and self-heals a stale DHT. app.py runs it in a
background thread every 6 hours, replacing the standalone cron job.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_TRACKERS = [
    "https://open.stealth.si/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "udp://tracker.opentrackr.org:6969",
    "http://open.demonii.dyn:6699/announce",
]

# --- pool maintenance state -------------------------------------------------
DAEMON_DIR = "/home/patrick/.config/transmission-daemon"
RPC = "http://127.0.0.1:9091/transmission/rpc"
POOL_INTERVAL_S = 6 * 3600

# Candidate pool. TCP-checked entries: (host, port, use_tls, announce_url).
# Shared lock: serializes the background pool_loop with any on-demand refresh
# (POST /api/tracker-pool/refresh) so two maintain_pool passes never overlap.
POOL_LOCK = threading.Lock()
CANDIDATES = [
    ("open.stealth.si", 443, True, "https://open.stealth.si/announce"),
    ("tracker.opentrackr.org", 1337, False, "http://tracker.opentrackr.org:1337/announce"),
    ("open.demonii.dyn", 6699, False, "http://open.demonii.dyn:6699/announce"),
    ("open.nymp.com", 443, True, "https://open.nymp.com/announce"),
    ("tracker1.etrv.org", 6969, False, "http://tracker1.etrv.org:6969/announce"),
    ("bt.karnet.com.id", 1337, False, "http://bt.karnet.com.id:1337/announce"),
    ("tracker.bt4g.com", 6969, False, "http://tracker.bt4g.com:6969/announce"),
]
# UDP trackers: not health-checkable without P2P handshake; carried along.
UDP_TRACKERS = [
    "udp://tracker.opentrackr.org:6969",
    "udp://open.demonii.dyn:1337",
]


def append_trackers(magnet, trackers):
    """Append any tracker not already present in `magnet` as a `tr` param."""
    seen = magnet.lower()
    for url in trackers:
        key = f"tr={url}".lower()
        if key in seen:
            continue
        magnet += f"&tr={url}"
        seen += key
    return magnet


def _tcp_ok(host, port, tls, timeout=6):
    try:
        if tls:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as s:
                    return s.recv(1) is not None or True
        else:
            socket.create_connection((host, port), timeout=timeout)
            return True
    except Exception:
        return False


def _rpc_call(method, args):
    req = urllib.request.Request(RPC, data=json.dumps({"method": method, "arguments": args}).encode())
    try:
        return json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        req.add_header("X-Transmission-Session-Id", e.headers["X-Transmission-Session-Id"])
        return json.load(urllib.request.urlopen(req, timeout=15))


def maintain_pool(config, config_path):
    """One pool-maintenance pass. Returns log lines.

    1. health-gate CANDIDATES -> pool = tcp-live + UDP trackers
    2. publish pool into `config["trackers"]` (the in-memory dict passed in,
       plus the JSON file at `config_path`, backed up on change)
    3. re-apply the live list to active torrents over Transmission RPC
    4. DHT self-heal: stale dht.dat (>72h) or missing with stalled active
       downloads -> back up the table, reset it, restart the daemon
    """
    lines = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    live = [url for (host, port, tls, url) in CANDIDATES if _tcp_ok(host, port, tls)]
    pool = live + UDP_TRACKERS
    lines.append(f"tcp-live {len(live)}/{len(CANDIDATES)}: {live}")

    # 1. publish to config (in-memory dict + file)
    try:
        if config.get("trackers", []) != pool:
            bak = config_path + ".bak-" + datetime.now().strftime("%Y%m%d%H%M")
            if os.path.exists(config_path):
                shutil.copy2(config_path, bak)
            config["trackers"] = pool
            tmp = config_path + ".tmp"
            json.dump(config, open(tmp, "w"), indent=2)
            os.replace(tmp, config_path)
            lines.append(f"config trackers updated -> {len(pool)} urls (backup {os.path.basename(bak)})")
        else:
            lines.append(f"config trackers unchanged ({len(pool)} urls)")
    except Exception as e:
        lines.append(f"config update failed: {e}")

    # 2. apply to active torrents (ids currently downloading/checking)
    try:
        torrents = _rpc_call("torrent-get", {"fields": ["id", "status"]})["arguments"]["torrents"]
        active = [t["id"] for t in torrents if t.get("status") in (1, 2, 3, 4)]
        if active and live:
            r = _rpc_call("torrent-set", {"ids": active, "add-trackers": live})
            lines.append(f"add-trackers to {len(active)} active torrents: {r.get('result')}")
    except Exception as e:
        lines.append(f"rpc apply failed (daemon down?): {e}")

    # 3. DHT liveness check + self-heal (the Aug-31 / Nov-9 failure mode)
    try:
        dht = os.path.join(DAEMON_DIR, "dht.dat")
        exists = os.path.exists(dht)
        age_h = (time.time() - os.path.getmtime(dht)) / 3600 if exists else None
        torrents = _rpc_call("torrent-get", {"fields": ["id", "status", "peersConnected"]})["arguments"]["torrents"]
        active = [t for t in torrents if t.get("status") in (1, 2, 3, 4)]
        peers = sum(t.get("peersConnected", 0) or 0 for t in torrents)
        # heal only when DHT is stale/missing AND a download is actually stalled (active, zero peers)
        dht_bad = (age_h is not None and age_h > 72) or (age_h is None and peers == 0 and len(active) > 0)
        note = f"dht.dat {'age %.0fh' % age_h if age_h is not None else 'missing'}"
        if dht_bad and peers == 0 and active:
            ts2 = datetime.now().strftime("%Y%m%d%H%M")
            if exists:
                shutil.copy2(dht, os.path.join(os.path.dirname(__file__), f"dht.dat.stale-{ts2}"))
                os.remove(dht)
            env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")
            rc = subprocess.run(["systemctl", "--user", "restart", "transmission-daemon-user"],
                                env=env, capture_output=True, text=True).returncode
            lines.append(f"HEAL: {note}, {len(active)} active torrents, 0 peers -> "
                         f"reset dht.dat + restarted transmission-daemon-user (rc={rc})")
        else:
            lines.append(f"{note}, connected peers: {peers}, active torrents: {len(active)}")
    except Exception as e:
        lines.append(f"dht check failed: {e}")

    return lines


def pool_loop(config, config_path, interval_s=POOL_INTERVAL_S):
    """Run maintain_pool now, then every `interval_s` (background thread body)."""
    while True:
        try:
            with POOL_LOCK:
                for line in maintain_pool(config, config_path):
                    print(f"[tracker-pool] {line}", flush=True)
        except Exception as e:
            print(f"[tracker-pool] pass failed: {e}", flush=True)
        time.sleep(interval_s)
