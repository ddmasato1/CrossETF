#!/usr/bin/env python3
"""Mocked regression checks for serialized market-data refreshes."""

import threading
import time
import io
import inspect

import server


def _registry():
    return {
        "status": "online_fresh",
        "status_desc": "test",
        "cached_date": "2026-10-21",
        "updated_at": "test",
        "last_log": "test",
        "broad_etfs": [{"code": "513100", "name": "test", "symbol": "sh513100", "fee": "0.80%", "index_name": "test"}],
        "sector_etfs": [],
        "sp500_etfs": [],
    }


def _isolated_market_state():
    original = {
        "cache": dict(server.cache_data),
        "discover": server.discover_etfs_online,
        "fetch": server.fetch_url,
        "archive": server.record_daily_history,
    }
    server.cache_data.update(timestamp=0, payload=None)
    server.discover_etfs_online = lambda force=False: _registry()
    server.record_daily_history = lambda payload: None
    return original


def _restore_market_state(original):
    server.cache_data.update(original["cache"])
    server.discover_etfs_online = original["discover"]
    server.fetch_url = original["fetch"]
    server.record_daily_history = original["archive"]


def test_cold_cache_requests_are_coalesced():
    original = _isolated_market_state()
    started = threading.Event()
    release = threading.Event()
    ready = threading.Barrier(7)
    count_lock = threading.Lock()
    calls = []
    results = []
    errors = []

    def fake_fetch(url, **kwargs):
        with count_lock:
            calls.append(url)
            first = len(calls) == 1
        if first:
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release mocked upstream")
        return ""

    server.fetch_url = fake_fetch

    def request():
        try:
            ready.wait(timeout=2)
            results.append(server.get_market_data())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        ready.wait(timeout=2)
        assert started.wait(2), "首个刷新未进入 mock 上游"
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(2)
        assert all(not thread.is_alive() for thread in threads), "行情刷新线程未结束"
        assert not errors, f"并发请求异常: {errors}"
        assert len(results) == 6
        assert len(calls) == 2, f"腾讯和新浪请求应各一次，实际 {len(calls)} 次"
        assert sum("qt.gtimg.cn" in url for url in calls) == 1
        assert sum("hq.sinajs.cn" in url for url in calls) == 1
    finally:
        release.set()
        _restore_market_state(original)


def test_cache_ttl_starts_when_refresh_finishes():
    original = _isolated_market_state()
    original_time = server.time.time
    clock = [100.0]
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            clock[0] = 110.0
        return ""

    server.time.time = lambda: clock[0]
    server.fetch_url = fake_fetch
    try:
        first = server.get_market_data()
        assert server.cache_data["timestamp"] == 110.0, "缓存时间戳应取刷新完成时间"
        clock[0] = 112.0
        second = server.get_market_data()
        assert second is first, "完成后 2 秒仍应命中 3 秒缓存"
        assert len(calls) == 2, "缓存有效期内不应再次请求上游"

        clock[0] = 113.0
        forced = server.get_market_data(force=True)
        assert forced is not second, "有效缓存存在时 force=True 仍应产生新 payload"
        assert len(calls) == 4, "强制刷新应重新请求腾讯和新浪"
        assert server.cache_data["timestamp"] == 113.0
    finally:
        server.time.time = original_time
        _restore_market_state(original)


def test_refresh_exception_releases_lock():
    original = _isolated_market_state()
    server.fetch_url = lambda url, **kwargs: (_ for _ in ()).throw(RuntimeError("mock failure"))
    try:
        try:
            server.get_market_data()
        except RuntimeError as exc:
            assert str(exc) == "mock failure"
        else:
            raise AssertionError("mock 上游异常应向调用方传播")
        assert server.market_data_lock.acquire(timeout=1), "刷新异常后行情锁仍被占用"
        server.market_data_lock.release()
    finally:
        _restore_market_state(original)


def test_symbol_refresh_and_market_refresh_share_lock():
    original = _isolated_market_state()
    symbol_refresh_entered = threading.Event()
    release_symbol_refresh = threading.Event()
    market_discovery_entered = threading.Event()
    failures = []

    def discover(force=False):
        if force:
            symbol_refresh_entered.set()
            if not release_symbol_refresh.wait(2):
                raise AssertionError("test did not release symbol refresh")
        else:
            market_discovery_entered.set()
        return _registry()

    class LocalHandler(server.DashboardHandler):
        def __init__(self):
            self.path = "/api/refresh-symbols"
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            pass

        def end_headers(self):
            pass

    server.discover_etfs_online = discover
    server.fetch_url = lambda url, **kwargs: ""
    handler_thread = threading.Thread(target=lambda: LocalHandler().do_GET())

    def market_refresh():
        try:
            server.get_market_data(force=True)
        except Exception as exc:
            failures.append(exc)

    market_thread = threading.Thread(target=market_refresh)
    try:
        handler_thread.start()
        assert symbol_refresh_entered.wait(1), "管理刷新没有进入标的发现"
        market_thread.start()
        time.sleep(0.05)
        assert not market_discovery_entered.is_set(), "行情刷新应等待管理刷新释放共用锁"
        release_symbol_refresh.set()
        handler_thread.join(2)
        market_thread.join(2)
        assert not handler_thread.is_alive() and not market_thread.is_alive()
        assert market_discovery_entered.is_set(), "管理刷新结束后行情刷新未继续"
        assert not failures, f"行情强刷异常: {failures}"
    finally:
        release_symbol_refresh.set()
        _restore_market_state(original)


def test_daily_collector_requests_forced_refresh():
    assert "get_market_data(force=True)" in inspect.getsource(server.daily_collector_daemon), \
        "后台收盘采集必须强制刷新行情"


if __name__ == "__main__":
    test_cold_cache_requests_are_coalesced()
    print("✅ 冷缓存六并发请求合并通过")
    test_cache_ttl_starts_when_refresh_finishes()
    print("✅ 缓存 TTL 从刷新完成时起算通过")
    test_refresh_exception_releases_lock()
    print("✅ 刷新异常后锁释放通过")
    test_symbol_refresh_and_market_refresh_share_lock()
    print("✅ 标的管理刷新与行情刷新共用锁通过")
    test_daily_collector_requests_forced_refresh()
    print("✅ 后台收盘采集强刷调用通过")
