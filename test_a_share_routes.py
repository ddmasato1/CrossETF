"""观察页HTTP边界回归；临时本地端口、模拟数据提供者，不启动采集线程。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server


class AShareRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.httpd.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        for name, result in (
            ("get_a_share_volume_payload", {"code": 200, "date": "2026-11-06", "groups": [], "aggregate": None}),
            ("get_daily_history_payload", {"code": 200, "days": []}),
            ("get_a_share_available_dates", ["2026-11-06", "2026-11-05"]),
        ):
            setting = patch.object(server, name, return_value=result)
            setattr(self, name, setting.start())
            self.addCleanup(setting.stop)
        setting = patch.object(server.DashboardHandler, "log_message")
        setting.start()
        self.addCleanup(setting.stop)

    def request(self, path):
        try:
            response = urllib.request.urlopen(self.base + path, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def test_observation_page_aliases_and_query_string(self):
        for path in ("/volume", "/volume/", "/a-share-volume.html", "/volume?view=daily"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn("核心ETF成交观察", body.decode())
                self.assertIn("三年日成交额与上证指数", body.decode())

    def test_minute_query_decodes_date(self):
        status, headers, body = self.request("/api/a-share-volume/?date=2026%2D11%2D06")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["date"], "2026-11-06")
        self.get_a_share_volume_payload.assert_called_once_with(target_date="2026-11-06")
        self.assertIn("no-store", headers["Cache-Control"])

    def test_dates_endpoint_does_not_use_minute_payload(self):
        status, _, body = self.request("/api/a-share-volume/dates/")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["dates"], ["2026-11-06", "2026-11-05"])
        self.get_a_share_volume_payload.assert_not_called()

    def test_daily_range_and_blank_date_are_forwarded(self):
        status, _, body = self.request("/api/a-share-volume/daily/?start=2023%2D11%2D07&end=2026-11-06")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["days"], [])
        self.get_daily_history_payload.assert_called_once_with(start="2023-11-07", end="2026-11-06")
        self.request("/api/a-share-volume/daily?start=")
        self.get_daily_history_payload.assert_called_with(start="", end=None)

    def test_module_errors_keep_http_status_and_json_message(self):
        for name, path, code in (
            ("get_daily_history_payload", "/api/a-share-volume/daily", 400),
            ("get_daily_history_payload", "/api/a-share-volume/daily", 503),
            ("get_a_share_volume_payload", "/api/a-share-volume?date=2026-11-07", 404),
        ):
            with self.subTest(path=path, code=code):
                getattr(self, name).return_value = {"code": code, "message": "不可用"}
                status, headers, body = self.request(path)
                self.assertEqual(status, code)
                self.assertEqual(json.loads(body)["message"], "不可用")
                self.assertIn("application/json", headers["Content-Type"])

    def test_health_and_existing_quotes_are_independent(self):
        with patch.object(server, "get_market_data", return_value={"version": server.APP_VERSION, "etfs": []}):
            status, _, body = self.request("/api/quotes")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], server.APP_VERSION)
        status, _, body = self.request("/health")
        self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
        self.get_daily_history_payload.assert_not_called()
        self.get_a_share_volume_payload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
