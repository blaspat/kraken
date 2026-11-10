"""Unit tests for /api/torrent controls. Runnable with plain python3 (no pytest)."""
import atexit
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["KRAKEN_NO_BG"] = "1"  # do not start the tracker-pool thread on import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cfg = os.path.join(_REPO_ROOT, "config.json")
if not os.path.exists(_cfg):  # clean checkouts: app.py needs config.json at import
    with open(_cfg, "w") as _f:
        json.dump({"port": 8098, "secret_key": "test-secret"}, _f)
    atexit.register(os.remove, _cfg)

import app as kraken  # noqa: E402


class TorrentControlTests(unittest.TestCase):
    def setUp(self):
        kraken.app.config["TESTING"] = True
        self.client = kraken.app.test_client()
        with self.client.session_transaction() as s:
            s["authed"] = True
        self.rpc = mock.patch.object(kraken, "tr_rpc", return_value={"result": "success"}).start()
        self.addCleanup(mock.patch.stopall)

    def post(self, body):
        return self.client.post("/api/torrent", json=body)

    def test_unknown_action_rejected(self):
        r = self.post({"action": "restart", "id": 1})
        self.assertEqual(r.status_code, 400)
        self.rpc.assert_not_called()

    def test_missing_action_rejected(self):
        r = self.post({"id": 1})
        self.assertEqual(r.status_code, 400)

    def test_non_int_id_rejected(self):
        for bad in ("3", 3.5, None, [3]):
            r = self.post({"action": "pause", "id": bad})
            self.assertEqual(r.status_code, 400, bad)
        self.rpc.assert_not_called()

    def test_bool_id_rejected(self):
        r = self.post({"action": "pause", "id": True})
        self.assertEqual(r.status_code, 400)

    def test_pause_maps_to_torrent_set(self):
        r = self.post({"action": "pause", "id": 7})
        self.assertEqual(r.status_code, 200)
        self.rpc.assert_called_once_with("torrent-set", {"ids": [7], "paused": True})

    def test_resume_maps_to_torrent_set(self):
        r = self.post({"action": "resume", "id": 7})
        self.assertEqual(r.status_code, 200)
        self.rpc.assert_called_once_with("torrent-set", {"ids": [7], "paused": False})

    def test_delete_defaults_to_keep_files(self):
        r = self.post({"action": "delete", "id": 7})
        self.assertEqual(r.status_code, 200)
        self.rpc.assert_called_once_with(
            "torrent-remove", {"ids": [7], "delete-local-data": False})

    def test_delete_with_files(self):
        r = self.post({"action": "delete", "id": 7, "with_files": True})
        self.assertEqual(r.status_code, 200)
        self.rpc.assert_called_once_with(
            "torrent-remove", {"ids": [7], "delete-local-data": True})

    def test_with_files_ignored_for_pause(self):
        self.post({"action": "pause", "id": 7, "with_files": True})
        args = self.rpc.call_args[0][1]
        self.assertNotIn("delete-local-data", args)

    def test_rpc_failure_is_502(self):
        self.rpc.return_value = {"result": "torrent not found"}
        r = self.post({"action": "resume", "id": 7})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"], "torrent not found")

    def test_rpc_exception_is_502(self):
        import requests
        self.rpc.side_effect = requests.RequestException("down")
        r = self.post({"action": "pause", "id": 7})
        self.assertEqual(r.status_code, 502)
        self.assertIn("error", r.get_json())

    def test_unauthenticated_is_401(self):
        self.client = kraken.app.test_client()
        r = self.client.post("/api/torrent", json={"action": "pause", "id": 1})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
