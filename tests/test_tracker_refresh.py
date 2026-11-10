"""Tests for peer stats in /api/status and POST /api/tracker-pool/refresh."""
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


class TrackerRefreshTests(unittest.TestCase):
    def setUp(self):
        kraken.app.config["TESTING"] = True
        self.client = kraken.app.test_client()
        with self.client.session_transaction() as s:
            s["authed"] = True
        self.rpc = mock.patch.object(
            kraken, "tr_rpc",
            return_value={"result": "success", "arguments": {"torrents": []}},
        ).start()
        self.refresh = mock.patch.object(
            kraken, "maintain_pool", return_value=["tcp-live 3/5: []", "config unchanged"],
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_status_exposes_peer_fields(self):
        self.rpc.return_value = {
            "result": "success",
            "arguments": {"torrents": [{
                "id": 1, "name": "x", "status": 4, "percentDone": 0.5,
                "rateDownload": 10, "rateUpload": 2, "downloadDir": "/tmp",
                "errorString": "", "eta": 60, "files": [],
                "seeders": 12, "leechers": 45, "uploadedEver": 123456,
            }]},
        }
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        row = r.get_json()["torrents"][0]
        self.assertEqual(row["seeders"], 12)
        self.assertEqual(row["leechers"], 45)
        self.assertEqual(row["uploaded"], 123456)
        fields = self.rpc.call_args[0][1]["fields"]
        for f in ("seeders", "leechers", "uploadedEver"):
            self.assertIn(f, fields)

    def test_status_peer_fields_default_to_zero(self):
        self.rpc.return_value = {
            "result": "success",
            "arguments": {"torrents": [{"id": 1, "name": "x", "status": 0,
                                        "percentDone": 1, "files": []}]},
        }
        row = self.client.get("/api/status").get_json()["torrents"][0]
        self.assertEqual((row["seeders"], row["leechers"], row["uploaded"]), (0, 0, 0))

    def test_refresh_runs_one_pass(self):
        r = self.client.post("/api/tracker-pool/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {
            "result": "success",
            "lines": ["tcp-live 3/5: []", "config unchanged"],
        })
        self.refresh.assert_called_once_with(
            kraken.CONFIG, os.path.join(kraken.BASE, "config.json"))

    def test_refresh_failure_is_502(self):
        self.refresh.side_effect = RuntimeError("boom")
        r = self.client.post("/api/tracker-pool/refresh")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"], "boom")

    def test_refresh_already_running_is_409(self):
        acquired = kraken.POOL_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            r = self.client.post("/api/tracker-pool/refresh")
        finally:
            kraken.POOL_LOCK.release()
        self.assertEqual(r.status_code, 409)
        self.refresh.assert_not_called()

    def test_refresh_unauthenticated_is_401(self):
        self.client = kraken.app.test_client()
        r = self.client.post("/api/tracker-pool/refresh")
        self.assertEqual(r.status_code, 401)
        self.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
