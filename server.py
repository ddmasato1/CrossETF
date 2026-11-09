#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CrossETF HTTP入口：启动业务采集、分发接口并组织响应。"""

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from app.core.config import APP_VERSION, BIND_ADDRESS, PORT, CURRENT_DIR, DATA_DIR
from app.market.history import ensure_data_storage, read_history, read_history_bytes
from app.market.quotes import get_market_data, discover_etfs_online, refresh_symbols, start_daily_collector_daemon
from app.signals.nasdaq import get_fear_greed_data, get_valuation_data, get_nasdaq_dca_data, dca_evaluator_daemon
from app.a_share.volume import (
    get_a_share_volume_payload,
    get_available_dates as get_a_share_available_dates,
    start_a_share_volume_daemon,
    ensure_volume_storage,
    configure as configure_a_share_volume,
)
from app.a_share.daily import (
    configure as configure_a_share_daily,
    get_daily_history_payload,
    start_a_share_daily_daemon,
)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """支持多线程并发请求的HTTP服务器。"""
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200, *, cors=True,
                  cache_control="no-cache, no-store, must-revalidate", allow_nan=True):
        content = json.dumps(data, ensure_ascii=False, allow_nan=allow_nan).encode("utf-8")
        self.send_content(content, "application/json; charset=utf-8", status,
                          cors=cors, cache_control=cache_control)

    def send_content(self, content, content_type, status=200, *, cors=False,
                     cache_control=None, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if req_path == "/api/quotes":
            self.send_json(get_market_data())
            return

        if req_path in ["/api/fear-greed", "/api/fear-greed/"]:
            status, data = get_fear_greed_data()
            self.send_json(data, status)
            return

        if req_path in ["/api/nasdaq-valuation", "/api/nasdaq-valuation/"]:
            status, data = get_valuation_data()
            self.send_json(data, status, cors=False, cache_control="no-store", allow_nan=False)
            return

        if req_path in ["/api/nasdaq-dca", "/api/nasdaq-dca/"]:
            self.send_json(get_nasdaq_dca_data(), cors=False, cache_control="no-store", allow_nan=False)
            return

        if req_path in ["/api/refresh-symbols", "/api/refresh-symbols/"]:
            reg = refresh_symbols()
            data = {
                "code": 200, "version": APP_VERSION,
                "message": reg.get("last_log", "标的池刷新完成"),
                "log": reg.get("last_log", "标的池刷新完成"),
                "status": reg["status"], "status_desc": reg["status_desc"],
                "cached_date": reg["cached_date"], "updated_at": reg["updated_at"],
                "broad_count": len(reg["broad_etfs"]), "sector_count": len(reg["sector_etfs"]),
                "sp500_count": len(reg.get("sp500_etfs", [])),
                "broad_etfs": reg["broad_etfs"], "sector_etfs": reg["sector_etfs"],
                "sp500_etfs": reg.get("sp500_etfs", []),
            }
            self.send_json(data, cache_control=None)
            return

        if req_path in ["/api/history", "/api/history/"]:
            try:
                days_limit = int(query.get("days", [""])[0])
            except ValueError:
                days_limit = None
            history = read_history(days_limit)
            self.send_json({"code": 200, "version": APP_VERSION,
                            "total_days": len(history), "data": history})
            return

        if req_path in ["/api/history/download", "/api/history/download/"]:
            try:
                content = read_history_bytes()
            except FileNotFoundError:
                self.send_error(404, "History file not found")
                return
            self.send_content(content, "application/json; charset=utf-8",
                              disposition='attachment; filename="crossetf_history_daily.json"')
            return

        if req_path in ["/api/a-share-volume", "/api/a-share-volume/"]:
            data = get_a_share_volume_payload(target_date=query.get("date", [None])[0])
            self.send_json(data, data.get("code", 200), allow_nan=False)
            return

        if req_path in ["/api/a-share-volume/daily", "/api/a-share-volume/daily/"]:
            data = get_daily_history_payload(start=query.get("start", [None])[0],
                                            end=query.get("end", [None])[0])
            self.send_json(data, data.get("code", 200), allow_nan=False)
            return

        if req_path in ["/api/a-share-volume/dates", "/api/a-share-volume/dates/"]:
            self.send_json({"code": 200, "dates": get_a_share_available_dates()})
            return

        if req_path == "/health":
            self.send_content(b'{"status": "ok"}', "application/json")
            return

        if req_path in ["/volume", "/volume/", "/a-share-volume.html"]:
            self.serve_static("a-share-volume.html", "text/html; charset=utf-8")
            return

        if req_path in ["/", "/index.html"]:
            self.serve_static("index.html", "text/html; charset=utf-8")
            return

        if req_path in ["/favicon.ico", "/favicon.png", "/favicon.svg"]:
            png_file = os.path.join(CURRENT_DIR, "static", "favicon.png")
            if req_path != "/favicon.svg" and os.path.isfile(png_file):
                self.serve_static("favicon.png", "image/png", cache_control="public, max-age=86400")
            else:
                self.serve_static("favicon.svg", "image/svg+xml", cache_control="public, max-age=86400")
            return

        extensions = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                      ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                      ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".svg": "image/svg+xml"}
        content_type = extensions.get(os.path.splitext(req_path)[1], "application/octet-stream")
        self.serve_static(req_path.lstrip("/"), content_type)

    def serve_static(self, relative_path, content_type, *, cache_control=None):
        static_dir = os.path.join(CURRENT_DIR, "static")
        path = os.path.normpath(os.path.join(static_dir, relative_path))
        if os.path.commonpath([static_dir, path]) != static_dir or not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        with open(path, "rb") as handle:
            content = handle.read()
        self.send_content(content, content_type, cache_control=cache_control)

    def log_message(self, format, *args):
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()


def run_server():
    configure_a_share_volume(DATA_DIR)
    configure_a_share_daily(DATA_DIR)
    ensure_data_storage()
    ensure_volume_storage()
    print("🔍 正在在线检测并同步 ETF 标的池...")
    discover_etfs_online()
    start_daily_collector_daemon()
    threading.Thread(target=dca_evaluator_daemon, daemon=True, name="DcaEvaluatorDaemon").start()
    start_a_share_volume_daemon()
    start_a_share_daily_daemon()
    httpd = ThreadedHTTPServer((BIND_ADDRESS, PORT), DashboardHandler)
    print("=" * 60)
    print(f"🚀 CrossETF 跨境ETF实时溢价追踪服务已启动！[{APP_VERSION}]")
    print(f"👉 本地访问地址: http://127.0.0.1:{PORT}")
    print(f"👉 A股成交观察页: http://127.0.0.1:{PORT}/volume")
    print(f"👉 实时行情 API: http://127.0.0.1:{PORT}/api/quotes")
    print(f"👉 A股成交 API: http://127.0.0.1:{PORT}/api/a-share-volume")
    print(f"👉 估值观察 API: http://127.0.0.1:{PORT}/api/nasdaq-valuation")
    print(f"👉 市场情绪 API: http://127.0.0.1:{PORT}/api/fear-greed")
    print(f"👉 历史归档 API: http://127.0.0.1:{PORT}/api/history")
    print(f"👉 标的同步 API: http://127.0.0.1:{PORT}/api/refresh-symbols")
    print("💡 按 Ctrl+C 可停止服务")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 正在停止服务...")
        httpd.server_close()
        print("服务已平稳关闭。")


if __name__ == "__main__":
    run_server()
