"""Public tracker fallback appended to magnets before torrent-add.

TPB magnets carry no `tr` params, and Transmission's DHT can go stale, so a
few known-good trackers are cheap insurance. `config.json` "trackers" key
overrides DEFAULT_TRACKERS when present.
"""

DEFAULT_TRACKERS = [
    "https://open.stealth.si/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "udp://tracker.opentrackr.org:6969",
    "http://open.demonii.dyn:6699/announce",
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
