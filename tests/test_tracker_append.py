"""Unit tests for tracker appending. Runnable with plain python3 (no pytest)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackers import DEFAULT_TRACKERS, append_trackers  # noqa: E402

MAGNET = "magnet:?xt=urn:btih:abc123&dn=Some.Name"
A = "http://a.example/announce"
B = "udp://b.example:6969"


def test_appends_missing():
    out = append_trackers(MAGNET, [A, B])
    assert out == f"{MAGNET}&tr={A}&tr={B}"


def test_noop_when_present():
    existing = f"{MAGNET}&tr={A}"
    assert append_trackers(existing, [A]) == existing


def test_noop_case_insensitive():
    existing = f"{MAGNET}&tr=HTTP://A.EXAMPLE/ANNOUNCE"
    assert append_trackers(existing, [A]) == existing


def test_dedupe_within_tracker_list():
    out = append_trackers(MAGNET, [A, A])
    assert out == f"{MAGNET}&tr={A}"


def test_defaults_nonempty():
    assert len(DEFAULT_TRACKERS) == 4


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
