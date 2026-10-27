"""Exercise the real ASGI boundary, without browsers or platform requests."""
import unittest
import tempfile
from pathlib import Path
from unittest import mock
import store
from fastapi.testclient import TestClient
import app


class LocalAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        self.db_patch.start()
        self.client = TestClient(app.app, base_url="http://127.0.0.1:18770")

    def tearDown(self):
        self.client.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_api_without_session_is_rejected(self):
        response = self.client.post("/api/delivery/request", json={})
        self.assertEqual(response.status_code, 401)

    def test_cross_origin_rejected_even_with_valid_session(self):
        self.client.get("/")
        for origin in ("https://audit.invalid", "null", "http://localhost:18770", "http://127.0.0.1:19999"):
            with self.subTest(origin=origin):
                response = self.client.post("/api/delivery/request", json={}, headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("access-control-allow-origin", response.headers)

    def test_bootstrap_sets_private_cookie_and_same_origin_api_works(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        response = self.client.get("/api/runtime/context",
                                   headers={"Origin": "http://127.0.0.1:18770"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_rebinding_and_malformed_hosts_are_rejected(self):
        for host in ("audit.invalid", "localhost.audit.invalid", "localhost:bad", "[broken", "localhost?evil"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/", headers={"Host": host}).status_code, 403)

    def test_cross_site_bootstrap_is_rejected(self):
        response = self.client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)

    def test_desktop_and_assets_share_custom_port_session(self):
        with TestClient(app.app, base_url="http://localhost:19999") as client:
            self.assertEqual(client.get("/desktop").status_code, 200)
            self.assertEqual(client.get("/workspace").status_code, 200)
            asset = client.get("/desktop-assets/delivery-engine.js")
            self.assertEqual(asset.status_code, 404)
            self.assertEqual(client.get("/desktop-assets/main.js").status_code, 404)
            self.assertEqual(client.get("/api/runtime/context").status_code, 200)

    def test_health_has_no_user_data_or_session(self):
        response = self.client.get("/health")
        self.assertEqual(response.json(), {"service": "jobkit", "ok": True})
        self.assertNotIn("set-cookie", response.headers)

    def test_arbitrary_cookie_does_not_authorize(self):
        self.client.cookies.set("jobkit_session", "synthetic-invalid")
        self.assertEqual(self.client.get("/api/delivery/pending").status_code, 401)
