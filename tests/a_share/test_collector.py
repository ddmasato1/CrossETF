#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_a_share_collector.py - A股低频采集器、共享请求预算与腾讯分时快照单元测试

纯标准库 + 虚拟时钟 + Mock 网络 + 隔离临时存储；
绝不发起实际网络请求，绝不污染 data/ 生产归档。
"""

import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

from app.a_share import daily as a_share_daily
from app.a_share import requests as a_share_requests
from app.a_share import sources as a_share_sources
from app.a_share import volume as a_share_volume
from app.a_share.requests import (
    MIN_REQUEST_INTERVAL,
    REQUESTS_PER_MINUTE,
    NoRedirects,
    RequestBudget,
    SourceCoolingDown,
    read_upstream,
)
from app.a_share.sources import (
    BEIJING_TZ,
    TENCENT_SOURCE,
    parse_tencent_series,
    parse_tencent_snapshot,
)
from app.a_share.volume import (
    SOURCE_NAME,
    STANDARD_TRADING_MINUTES,
    LowRateCollector,
    get_a_share_volume_payload,
    sync_instrument,
)


class BaseCollectorTestCase(unittest.TestCase):
    """测试基类：提供隔离的 TemporaryDirectory 与运行时状态重置"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.volume_dir = os.path.join(self.tmp_dir.name, "volume")
        os.makedirs(self.volume_dir, exist_ok=True)
        setting = patch.object(a_share_volume, "VOLUME_DIR", self.volume_dir)
        setting.start()
        self.addCleanup(setting.stop)

        with a_share_volume.volume_lock:
            a_share_volume.payload_cache["payload"] = None
            a_share_volume.payload_cache["date"] = None
            a_share_volume.payload_cache["timestamp"] = 0.0
            a_share_volume.volume_runtime_state["status"] = "uninitialized"
            a_share_volume.volume_runtime_state["status_desc"] = "尚未初始化"
            a_share_volume.volume_runtime_state["last_error"] = ""
            a_share_volume.volume_runtime_state["last_success_at"] = ""
            a_share_volume.volume_runtime_state["etf_statuses"] = {}
            a_share_volume.volume_runtime_state["index_statuses"] = {}
            a_share_volume.volume_runtime_state["snapshots"] = {}
            a_share_volume.volume_runtime_state["snapshot_error"] = ""
            a_share_volume.volume_runtime_state["is_syncing"] = False

    def _make_quote(self, symbol="sh510300", point=3.85, pre_close=3.80, time_iso="2026-11-09T10:00:00+08:00", amt=1000000.0):
        return {
            "date": time_iso[:10],
            "time": time_iso,
            "point": point,
            "pre_close": pre_close,
            "change_pct": round((point / pre_close - 1) * 100, 4),
            "cumulative_amount": amt,
            "source": "tencent_quote",
        }


class TestRequestBudgetPacing(unittest.TestCase):
    """测试请求预算控制：<=5次/60秒、>=12.5秒间隔、并发步调及冷却退避"""

    def test_budget_pacing_minimum_interval(self):
        """连续请求严格满足 >= 12.5 秒间隔"""
        curr = [1000.0]
        budget = RequestBudget(clock=lambda: curr[0], sleep=lambda d: curr.__setitem__(0, curr[0] + d))

        budget.acquire("api.gtimg.cn")
        self.assertEqual(curr[0], 1000.0)

        # 立即发起下一次请求，应虚拟推进 12.5 秒
        budget.acquire("api.gtimg.cn")
        self.assertEqual(curr[0], 1012.5)

        budget.acquire("api.gtimg.cn")
        self.assertEqual(curr[0], 1025.0)
        self.assertEqual(budget.total, 3)

    def test_budget_pacing_rolling_window_limit(self):
        """60 秒滚动窗口内最多 5 次请求，超出则等待最早请求满 60 秒移出"""
        curr = [1000.0]
        budget = RequestBudget(clock=lambda: curr[0], sleep=lambda d: curr.__setitem__(0, curr[0] + d))

        # 模拟 5 次在 1000~1040 之间的历史请求（假设密集队列）
        budget.starts = deque([1000.0, 1010.0, 1020.0, 1030.0, 1040.0])
        curr[0] = 1040.0

        # 第 6 次请求必须等到最早请求 (1000.0) 满 60 秒 (即 1060.0)
        budget.acquire("api.gtimg.cn")
        self.assertGreaterEqual(curr[0], 1060.0)
        self.assertNotIn(1000.0, budget.starts)
        self.assertEqual(len(budget.starts), 5)

    def test_budget_concurrent_callers_pacing(self):
        """多线程并发 acquire：无真实等待完成调度，所有实际请求间隔 >= 12.5 秒"""
        curr = {"now": 2000.0}
        clock_lock = threading.Lock()
        acquired_times = []

        def fake_clock():
            with clock_lock:
                return curr["now"]

        def fake_sleep(secs):
            with clock_lock:
                curr["now"] += secs

        budget = RequestBudget(clock=fake_clock, sleep=fake_sleep)

        class RecordingStarts(deque):
            def append(self, stamp):
                acquired_times.append(stamp)
                super().append(stamp)

        budget.starts = RecordingStarts()

        def worker():
            budget.acquire("host.concurrent")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(len(acquired_times), 5)
        sorted_times = sorted(acquired_times)
        for i in range(1, len(sorted_times)):
            interval = round(sorted_times[i] - sorted_times[i - 1], 4)
            self.assertGreaterEqual(interval, 12.5)
        self.assertEqual(budget.total, 5)

    def test_budget_cooldown_403_and_429_recovery(self):
        """403/429 触发至少 300 秒冷却；冷却期内抛异常，过期后恢复"""
        curr = [5000.0]
        budget = RequestBudget(clock=lambda: curr[0], sleep=lambda d: curr.__setitem__(0, curr[0] + d))

        err403 = urllib.error.HTTPError("https://host.cool/api", 403, "Forbidden", {}, None)
        budget.failure("host.cool", err403)

        self.assertGreaterEqual(budget.cooldowns["host.cool"], 5300.0)
        with self.assertRaises(SourceCoolingDown):
            budget.acquire("host.cool")

        # 时间推进超过 300 秒，自动解除冷却
        curr[0] = 5305.0
        budget.acquire("host.cool")
        self.assertEqual(budget.total, 1)

    def test_budget_retry_after_numeric_and_http_date(self):
        """429 Retry-After 支持秒数及 HTTP 日期解析，且不低于 300 秒"""
        curr = [5000.0]
        budget = RequestBudget(clock=lambda: curr[0], sleep=lambda d: curr.__setitem__(0, curr[0] + d))

        # 秒数大于 300
        err_sec = urllib.error.HTTPError("https://host/api", 429, "Too Many", {"Retry-After": "450"}, None)
        budget.failure("host", err_sec)
        self.assertEqual(budget.cooldowns["host"], 5450.0)

        # 秒数小于 300 时兜底为 300
        err_short = urllib.error.HTTPError("https://host", 429, "Too Many", {"Retry-After": "60"}, None)
        budget.failure("host", err_short)
        self.assertEqual(budget.cooldowns["host"], 5300.0)

        # HTTP Date 格式
        future_utc = datetime.now(timezone.utc) + timedelta(seconds=600)
        err_date = urllib.error.HTTPError(
            "https://host", 429, "Too Many", {"Retry-After": format_datetime(future_utc)}, None
        )
        budget.failure("host", err_date)
        self.assertGreaterEqual(budget.cooldowns["host"], 5590.0)
        self.assertLessEqual(budget.cooldowns["host"], 5610.0)

    def test_budget_exponential_backoff_and_reset(self):
        """常规错误按 60s/120s/240s 阶梯退避；成功后重置计数器"""
        curr = [1000.0]
        budget = RequestBudget(clock=lambda: curr[0], sleep=lambda d: curr.__setitem__(0, curr[0] + d))
        err = urllib.error.URLError("Connection reset")

        budget.failure("host", err)
        self.assertEqual(budget.cooldowns["host"], 1000.0)  # 前两次不冷却
        budget.failure("host", err)
        self.assertEqual(budget.cooldowns["host"], 1000.0)
        budget.failure("host", err)
        self.assertEqual(budget.cooldowns["host"], 1060.0)  # 3次：60s
        budget.failure("host", err)
        self.assertEqual(budget.cooldowns["host"], 1120.0)  # 4次：120s

        # 成功调用重置失败计数
        budget.success("host")
        self.assertEqual(budget.failures["host"], 0)
        budget.failure("host", err)
        self.assertEqual(budget.cooldowns["host"], 1000.0)

    def test_budget_info_fields(self):
        """info() 返回正确的监控结构与冷却剩余时间"""
        curr = [1000.0]
        budget = RequestBudget(clock=lambda: curr[0], sleep=lambda d: curr.__setitem__(0, curr[0] + d))
        budget.starts.append(980.0)
        budget.total = 5
        budget.cooldowns["cool.host"] = 1045.0

        info = budget.info()
        self.assertEqual(info["limit_per_minute"], REQUESTS_PER_MINUTE)
        self.assertEqual(info["minimum_interval_seconds"], MIN_REQUEST_INTERVAL)
        self.assertEqual(info["requests_last_minute"], 1)
        self.assertEqual(info["requests_since_start"], 5)
        self.assertEqual(info["cooldowns"]["cool.host"], 45)


class TestTransportAndNoRedirects(unittest.TestCase):
    """测试统一传输层：禁止自动重定向，模块间共用同一单例预算"""

    def test_no_redirects_blocks_all_http_redirects(self):
        """NoRedirects 阻断 301/302/303/307/308，不产生预算外的派生请求"""
        handler = NoRedirects()
        req = urllib.request.Request("https://example.com/query")
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.assertIsNone(handler.redirect_request(req, None, code, "Redirect", {}, "https://new.com"))

    def test_shared_transport_identity(self):
        """volume、sources、daily 模块均共用同一 read_upstream 与全局 request_budget"""
        self.assertIs(a_share_volume.read_upstream, a_share_requests.read_upstream)
        self.assertIs(a_share_sources.read_upstream, a_share_requests.read_upstream)
        self.assertIs(a_share_daily.read_upstream, a_share_requests.read_upstream)
        self.assertIs(a_share_volume.request_budget, a_share_requests.request_budget)

    def test_read_upstream_budget_integration_success(self):
        """read_upstream 成功时正确调用 acquire 与 success"""
        req = urllib.request.Request("https://test.upstream.com/data")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with patch("urllib.request.build_opener", return_value=mock_opener), \
             patch.object(a_share_requests.request_budget, "acquire") as mock_acq, \
             patch.object(a_share_requests.request_budget, "success") as mock_succ:
            body = read_upstream(req, timeout=9)
            self.assertEqual(body, b'{"ok": true}')
            mock_acq.assert_called_once_with("test.upstream.com")
            mock_succ.assert_called_once_with("test.upstream.com")

    def test_read_upstream_budget_integration_failure(self):
        """read_upstream 异常时记录 failure 并重新抛出"""
        req = urllib.request.Request("https://test.upstream.com/data")
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("Socket timeout")

        with patch("urllib.request.build_opener", return_value=mock_opener), \
             patch.object(a_share_requests.request_budget, "acquire") as mock_acq, \
             patch.object(a_share_requests.request_budget, "failure") as mock_fail:
            with self.assertRaises(urllib.error.URLError):
                read_upstream(req)
            mock_acq.assert_called_once_with("test.upstream.com")
            mock_fail.assert_called_once()
            self.assertEqual(mock_fail.call_args[0][0], "test.upstream.com")


class TestTencentSeriesParsing(unittest.TestCase):
    """测试腾讯分时解析：累计差分换算、09:30、午间、缺失前序、回退拦截及前收隔离"""

    def test_series_0930_exact_amount(self):
        """09:30 作为交易起点，其当分金额等于累计成交额本身"""
        payload = {
            "code": 0,
            "data": {
                "sh510300": {
                    "data": [{
                        "date": "20261109",
                        "data": ["0930 3.800 100 500000.00", "0931 3.810 120 850000.00"]
                    }]
                }
            }
        }
        res, _ = parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertEqual(res["2026-11-09"]["09:30"], 500000.00)
        self.assertEqual(res["2026-11-09"]["09:31"], 350000.00)

    def test_series_lunch_break_subtraction(self):
        """13:01 正确以 11:30 累计金额为基准计算当分差值"""
        payload = {
            "code": 0,
            "data": {
                "sh510300": {
                    "data": [{
                        "date": "20261109",
                        "data": [
                            "0930 3.800 100 500000.00",
                            "1130 3.820 500 5000000.00",
                            "1301 3.825 150 5300000.00"
                        ]
                    }]
                }
            }
        }
        res, _ = parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertEqual(res["2026-11-09"]["13:01"], 300000.00)

    def test_series_missing_predecessor_cannot_span_gap(self):
        """若前序分钟缺失，后序分钟严禁跨间隔直接差分（防止将多分钟总额误记入单一分钟）"""
        payload = {
            "code": 0,
            "data": {
                "sh510300": {
                    "data": [{
                        "date": "20261109",
                        "data": [
                            "0930 3.800 100 500000.00",
                            # 09:31 缺失
                            "0932 3.820 150 900000.00"
                        ]
                    }]
                }
            }
        }
        res, _ = parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertIn("09:30", res["2026-11-09"])
        self.assertNotIn("09:32", res["2026-11-09"])

    def test_series_rollback_rejected(self):
        """累计成交额出现倒退时直接拒绝本次快照（抛出 ValueError）"""
        payload = {
            "code": 0,
            "data": {
                "sh510300": {
                    "data": [{
                        "date": "20261109",
                        "data": ["0930 3.800 100 500000.00", "0931 3.810 100 480000.00"]
                    }]
                }
            }
        }
        with self.assertRaises(ValueError) as ctx:
            parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertIn("回退", str(ctx.exception))

    def test_series_nonfinite_and_duplicates(self):
        """非有限数值、负数及重复分钟记录均被严格拒绝"""
        bad_rows = [
            ["0930 3.800 100 500000.00", "0930 3.810 100 600000.00"],  # 重复分钟
            ["0930 NaN 100 500000.00"],                                # 非有限点位
            ["0930 3.800 100 -10.00"],                                 # 负成交额
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                payload = {"code": 0, "data": {"sh510300": {"data": [{"date": "20261109", "data": rows}]}}}
                with self.assertRaises(ValueError):
                    parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))

    def test_series_1500_plus_and_offmarket_filtered(self):
        """非标准分钟（如 09:25 集合竞价、15:01 收盘撮合）被过滤丢弃"""
        payload = {
            "code": 0,
            "data": {
                "sh510300": {
                    "data": [{
                        "date": "20261109",
                        "data": [
                            "0925 3.800 50 100000.00",
                            "0930 3.800 100 500000.00",
                            "1501 3.810 20 520000.00",
                            "1505 3.810 0 520000.00"
                        ]
                    }]
                }
            }
        }
        res, _ = parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertEqual(list(res["2026-11-09"].keys()), ["09:30"])

    def test_series_index_points_vs_etf_amounts(self):
        """指数解析返回原始点位，不执行差分"""
        payload = {
            "code": 0,
            "data": {
                "sh000300": {
                    "data": [{
                        "date": "20261109",
                        "data": ["0930 3950.20 100 1000000.00", "0931 3952.80 120 2000000.00"]
                    }]
                }
            }
        }
        res, _ = parse_tencent_series(payload, "sh000300", STANDARD_TRADING_MINUTES, is_index=True, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertEqual(res["2026-11-09"]["09:30"], 3950.20)
        self.assertEqual(res["2026-11-09"]["09:31"], 3952.80)

    def test_series_historical_prec_isolation(self):
        """历史日前收独立保留，最新日的 qt 前收绝不跨日串染旧日"""
        qt_data = [""] * 35
        qt_data[4] = "3.850"
        qt_data[30] = "20261109150000"
        payload = {
            "code": 0,
            "data": {
                "sh510300": {
                    "data": [
                        {"date": "20261106", "prec": "3.800", "data": ["0930 3.800 100 10000.00"]},
                        {"date": "20261109", "data": ["0930 3.850 100 10000.00"]}
                    ],
                    "qt": {"sh510300": qt_data}
                }
            }
        }
        _, pre_closes = parse_tencent_series(payload, "sh510300", STANDARD_TRADING_MINUTES, now=datetime(2026, 11, 9, 16, 0, tzinfo=BEIJING_TZ))
        self.assertEqual(pre_closes["2026-11-06"], 3.800)
        self.assertEqual(pre_closes["2026-11-09"], 3.850)


class TestTencentSnapshotParsing(unittest.TestCase):
    """测试腾讯批量快照解析：万元转元、标的过滤、溢出防范与有效性校验"""

    def _build_snapshot_line(self, symbol, code, point, pre_close, time_str, amt_wan):
        fields = [""] * 40
        fields[1] = "标的名"
        fields[2] = code
        fields[3] = str(point)
        fields[4] = str(pre_close)
        fields[30] = time_str
        fields[37] = str(amt_wan)
        return f'v_{symbol}="{"~".join(fields)}";'

    def test_snapshot_amount_units_conversion(self):
        """字段 37（万元）必须精确乘以 10000 转换为元"""
        line = self._build_snapshot_line("sh510300", "510300", 3.850, 3.800, "20261109145500", "12345.67")
        quotes = parse_tencent_snapshot(line, ["sh510300"], now=datetime(2026, 11, 9, 15, 0, tzinfo=BEIJING_TZ))

        q = quotes["sh510300"]
        self.assertEqual(q["point"], 3.85)
        self.assertEqual(q["pre_close"], 3.80)
        self.assertEqual(q["cumulative_amount"], 123456700.0)
        self.assertEqual(q["source"], "tencent_quote")
        self.assertEqual(q["time"], "2026-11-09T14:55:00+08:00")

    def test_snapshot_post_conversion_overflow_rejected(self):
        """换算后溢出（如 1e305 * 10000 -> inf）或非有限数值行被跳过丢弃"""
        line_overflow = self._build_snapshot_line("sh510300", "510300", 3.85, 3.80, "20261109145500", "1e305")
        line_valid = self._build_snapshot_line("sz159919", "159919", 3.90, 3.85, "20261109145500", "1000")

        # 单独溢出行触发 ValueError("腾讯批量快照无有效行情")
        with self.assertRaises(ValueError):
            parse_tencent_snapshot(line_overflow, ["sh510300"], now=datetime(2026, 11, 9, 15, 0, tzinfo=BEIJING_TZ))

        # 混合行中溢出行被忽略，有效行正常保留
        quotes = parse_tencent_snapshot(line_overflow + line_valid, ["sh510300", "sz159919"], now=datetime(2026, 11, 9, 15, 0, tzinfo=BEIJING_TZ))
        self.assertNotIn("sh510300", quotes)
        self.assertIn("sz159919", quotes)
        self.assertEqual(quotes["sz159919"]["point"], 3.90)

    def test_snapshot_symbol_filtering_and_code_validation(self):
        """仅解析请求的标的，代码不匹配与未请求标的均被丢弃"""
        l1 = self._build_snapshot_line("sh510300", "510300", 3.85, 3.80, "20261109145500", "1000")
        l2 = self._build_snapshot_line("sz159919", "159919", 3.90, 3.85, "20261109145500", "2000")
        l_mismatch = self._build_snapshot_line("sh510210", "999999", 3.00, 3.00, "20261109145500", "1000")

        body = l1 + l2 + l_mismatch
        quotes = parse_tencent_snapshot(body, ["sh510300"], now=datetime(2026, 11, 9, 15, 0, tzinfo=BEIJING_TZ))
        self.assertIn("sh510300", quotes)
        self.assertNotIn("sz159919", quotes)
        self.assertNotIn("sh510210", quotes)

    def test_snapshot_future_timestamp_skipped(self):
        """未来超过 2 分钟的异常快照记录被丢弃"""
        line = self._build_snapshot_line("sh510300", "510300", 3.85, 3.80, "20261109151000", "1000")
        with self.assertRaises(ValueError):
            parse_tencent_snapshot(line, ["sh510300"], now=datetime(2026, 11, 9, 15, 0, tzinfo=BEIJING_TZ))

    def test_snapshot_empty_or_invalid_raises(self):
        """无任何有效行情时抛出 ValueError"""
        with self.assertRaises(ValueError):
            parse_tencent_snapshot("", ["sh510300"])


class TestLowRateCollectorScheduler(BaseCollectorTestCase):
    """测试低频采集调度器：单步轮询、快照 60s、分时间隔 20s 无补发风暴、周末静默与按标的合并"""

    def test_scheduler_one_instrument_per_tick(self):
        """每次 tick 仅同步一个标的并推进游标"""
        collector = LowRateCollector(self.volume_dir)
        now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        with patch("app.a_share.volume.fetch_tencent_snapshot", return_value={"sh510300": self._make_quote("sh510300")}), \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}) as mock_sync:
            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                collector.tick(now)
            self.assertEqual(collector.cursor, 1)
            self.assertEqual(mock_sync.call_count, 1)

            with patch("app.a_share.volume.time.monotonic", return_value=125.0):
                collector.tick(now)
            self.assertEqual(collector.cursor, 2)
            self.assertEqual(mock_sync.call_count, 2)

    def test_scheduler_bootstrap_ndays_5_then_1(self):
        """冷启动初始轮次使用 ndays=5，成功后移出 history_pending 后续使用 ndays=1"""
        collector = LowRateCollector(self.volume_dir)
        now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        with patch("app.a_share.volume.fetch_tencent_snapshot", return_value={"sh510300": self._make_quote("sh510300")}), \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}) as mock_sync:
            first_inst = collector.instruments[0]

            # 初始轮次
            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                collector.tick(now)
            mock_sync.assert_called_with(self.volume_dir, first_inst[0], first_inst[1], first_inst[2], 5)

            # 模拟全部 14 个标的跑完首轮
            for i in range(1, 14):
                with patch("app.a_share.volume.time.monotonic", return_value=100.0 + i * 25):
                    collector.tick(now)
            self.assertEqual(collector.cursor, 0)
            self.assertEqual(collector.bootstrap_remaining, 0)
            self.assertEqual(len(collector.history_pending), 0)

            # 次轮回到首个标的，ndays 降为 1
            with patch("app.a_share.volume.time.monotonic", return_value=1000.0):
                collector.tick(now)
            mock_sync.assert_called_with(self.volume_dir, first_inst[0], first_inst[1], first_inst[2], 1)

    def test_scheduler_snapshot_max_60s_interval(self):
        """批量快照严格遵循 >= 60 秒上限，未满 60 秒时不请求快照"""
        collector = LowRateCollector(self.volume_dir)
        now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        with patch("app.a_share.volume.fetch_tencent_snapshot", return_value={"sh510300": self._make_quote("sh510300")}) as mock_snap, \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}):
            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                collector.tick(now)
            self.assertEqual(mock_snap.call_count, 1)

            # 30 秒后再次 tick，快照未达 60s 间隔不触发
            with patch("app.a_share.volume.time.monotonic", return_value=130.0):
                collector.tick(now)
            self.assertEqual(mock_snap.call_count, 1)

            # 61 秒后触发下一次快照
            with patch("app.a_share.volume.time.monotonic", return_value=161.0):
                collector.tick(now)
            self.assertEqual(mock_snap.call_count, 2)

    def test_scheduler_series_spacing_20s_and_no_catchup_burst(self):
        """分时严格保持 20 秒间隔；即使时间突进跳跃，也绝不并发补发多轮"""
        collector = LowRateCollector(self.volume_dir)
        now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        with patch("app.a_share.volume.fetch_tencent_snapshot", return_value={"sh510300": self._make_quote("sh510300")}), \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}) as mock_sync:
            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                collector.tick(now)
            self.assertEqual(mock_sync.call_count, 1)
            self.assertEqual(collector.next_series, 120.0)

            # 停机或时间跳跃 500 秒后唤醒，单次 tick 依然只执行 1 次同步，绝无密集突发
            with patch("app.a_share.volume.time.monotonic", return_value=600.0):
                collector.tick(now)
            self.assertEqual(mock_sync.call_count, 2)
            self.assertEqual(collector.next_series, 620.0)

            # 605 秒唤醒未达 20 秒，跳过
            with patch("app.a_share.volume.time.monotonic", return_value=605.0):
                collector.tick(now)
            self.assertEqual(mock_sync.call_count, 2)

    def test_scheduler_weekends_idle_after_bootstrap(self):
        """周末非交易日：冷启动完成后 tick 立即返回 False 保持静默"""
        collector = LowRateCollector(self.volume_dir)
        sunday = datetime(2026, 11, 8, 11, 0, tzinfo=BEIJING_TZ)

        with patch("app.a_share.volume.fetch_tencent_snapshot") as mock_snap, \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}) as mock_sync:
            collector.bootstrap_remaining = 0
            collector.history_pending.clear()

            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                worked = collector.tick(sunday)

            self.assertFalse(worked)
            mock_snap.assert_not_called()
            mock_sync.assert_not_called()

    def test_scheduler_failed_snapshot_retains_previous_readings(self):
        """快照网络异常时记录错误信息，已有快照数据完整保留不被冲掉"""
        collector = LowRateCollector(self.volume_dir)
        now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        with a_share_volume.volume_lock:
            a_share_volume.volume_runtime_state["snapshots"] = {
                "sh510300": self._make_quote("sh510300", time_iso="2026-11-09T09:35:00+08:00")
            }
            a_share_volume.volume_runtime_state["snapshot_error"] = ""

        with patch("app.a_share.volume.fetch_tencent_snapshot", side_effect=Exception("Snapshot 502 Bad Gateway")), \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}):
            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                collector.tick(now)

        with a_share_volume.volume_lock:
            self.assertIn("502", a_share_volume.volume_runtime_state["snapshot_error"])
            self.assertIn("sh510300", a_share_volume.volume_runtime_state["snapshots"])
            self.assertEqual(a_share_volume.volume_runtime_state["snapshots"]["sh510300"]["point"], 3.85)

    def test_scheduler_snapshot_merges_per_symbol_and_rejects_outdated(self):
        """快照按标的独立合并：保留部分响应中未返回的已有行情，并丢弃时间戳倒退的陈旧行情"""
        collector = LowRateCollector(self.volume_dir)
        now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        # 1. 预置两个标的的有效快照
        quote_sh = self._make_quote("sh510300", point=3.80, time_iso="2026-11-09T10:00:00+08:00")
        quote_sz = self._make_quote("sz159919", point=3.90, time_iso="2026-11-09T10:00:00+08:00")
        with a_share_volume.volume_lock:
            a_share_volume.volume_runtime_state["snapshots"] = {
                "sh510300": dict(quote_sh),
                "sz159919": dict(quote_sz)
            }

        # 2. 新快照只包含 sh510300（更新到 10:01），缺少 sz159919（部分返回）
        newer_sh = self._make_quote("sh510300", point=3.82, time_iso="2026-11-09T10:01:00+08:00")
        with patch("app.a_share.volume.fetch_tencent_snapshot", return_value={"sh510300": newer_sh}), \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}):
            with patch("app.a_share.volume.time.monotonic", return_value=100.0):
                collector.tick(now)

        with a_share_volume.volume_lock:
            snaps = a_share_volume.volume_runtime_state["snapshots"]
            self.assertEqual(snaps["sh510300"]["point"], 3.82)
            self.assertEqual(snaps["sh510300"]["time"], "2026-11-09T10:01:00+08:00")
            # sz159919 在部分响应中未出现，但应被完整保留
            self.assertIn("sz159919", snaps)
            self.assertEqual(snaps["sz159919"]["point"], 3.90)

        # 3. 若后续上游返回更旧时间戳（如 09:59:00），拒绝覆盖已有的 10:01 行情
        outdated_sh = self._make_quote("sh510300", point=3.70, time_iso="2026-11-09T09:59:00+08:00")
        with patch("app.a_share.volume.fetch_tencent_snapshot", return_value={"sh510300": outdated_sh}), \
             patch("app.a_share.volume.sync_instrument", return_value={"status": "ok", "dates": []}):
            with patch("app.a_share.volume.time.monotonic", return_value=170.0):
                collector.tick(now)

        with a_share_volume.volume_lock:
            snaps = a_share_volume.volume_runtime_state["snapshots"]
            # sh510300 保持 10:01 的 3.82，不被 09:59 的 3.70 倒退覆盖
            self.assertEqual(snaps["sh510300"]["point"], 3.82)
            self.assertEqual(snaps["sh510300"]["time"], "2026-11-09T10:01:00+08:00")


class TestSyncInstrumentFallback(BaseCollectorTestCase):
    """测试分时校准：腾讯优先且成功时不调用东财；腾讯故障时东财备用独立完整解析"""

    def test_sync_instrument_primary_tencent_avoids_eastmoney(self):
        """腾讯主数据源成功时，完全不请求东财备用接口"""
        meta = a_share_volume.A_SHARE_ETF_METAS["510300"]
        with patch("app.a_share.volume.fetch_tencent_series") as mock_tencent, \
             patch("app.a_share.volume.fetch_trends_raw") as mock_eastmoney, \
             patch("app.a_share.volume.merge_and_save_etf_data") as mock_save:
            mock_tencent.return_value = ({"2026-11-09": {"09:30": 500000.0}}, {})

            report = sync_instrument(self.volume_dir, "etf", "510300", meta, ndays=1)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["source"], TENCENT_SOURCE)
            self.assertEqual(report["primary_error"], "")
            mock_tencent.assert_called_once()
            mock_eastmoney.assert_not_called()
            mock_save.assert_called_once()

    def test_sync_instrument_tencent_failure_uses_eastmoney_complete_response(self):
        """腾讯失败时回退东财，独立提取并使用东财完整响应（含 preClose）"""
        meta = a_share_volume.A_SHARE_INDEX_METAS["hs300"]
        eastmoney_payload = {
            "rc": 0,
            "data": {
                "code": "000300",
                "preClose": 3950.0,
                "trends": ["2026-11-09 09:30,3955.0,3960.0,3950.0,3955.0,100,5000000.0,1"]
            }
        }
        with patch("app.a_share.volume.fetch_tencent_series", side_effect=Exception("Tencent timeout")), \
             patch("app.a_share.volume.fetch_trends_raw", return_value=eastmoney_payload) as mock_eastmoney, \
             patch("app.a_share.volume.merge_and_save_index_data") as mock_save_index:
            report = sync_instrument(self.volume_dir, "index", "hs300", meta, ndays=1)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["source"], SOURCE_NAME)
            self.assertIn("timeout", report["primary_error"])
            mock_eastmoney.assert_called_once()
            mock_save_index.assert_called_once()
            # 确认将东财的 preClose (3950.0) 传入
            self.assertEqual(mock_save_index.call_args[0][3], 3950.0)

    def test_sync_instrument_both_fail_records_failed_report(self):
        """腾讯与东财备用均故障时，如实记录失败状态与合并错误信息"""
        meta = a_share_volume.A_SHARE_ETF_METAS["510300"]
        with patch("app.a_share.volume.fetch_tencent_series", side_effect=Exception("Tencent 500")), \
             patch("app.a_share.volume.fetch_trends_raw", return_value=None):
            report = sync_instrument(self.volume_dir, "etf", "510300", meta, ndays=1)

            self.assertEqual(report["status"], "failed")
            self.assertIn("Tencent 500", report["error"])
            self.assertIn("东方财富备用暂不可用", report["error"])


class TestCollectorAPIIntegration(BaseCollectorTestCase):
    """测试成交额接口集成：包含 collector 预算元数据、index_info 匹配日 snapshot 与 force 缓存穿透"""

    def test_volume_payload_includes_collector_fields(self):
        """get_a_share_volume_payload 返回包含 collector 共享预算监控字段"""
        payload = get_a_share_volume_payload(volume_dir=self.volume_dir)
        self.assertIn("collector", payload)
        col = payload["collector"]
        self.assertEqual(col["limit_per_minute"], 5)
        self.assertEqual(col["minimum_interval_seconds"], 12.5)
        self.assertEqual(col["snapshot_interval_seconds"], 60)
        self.assertEqual(col["series_interval_seconds"], 20)
        self.assertEqual(col["scope"], "a_share_volume_and_daily")

    def test_volume_payload_force_bypasses_cache_without_network(self):
        """force=True 跳过计算缓存重新读取归档，不触发任何上游网络调用"""
        # 初始填充缓存
        get_a_share_volume_payload(volume_dir=self.volume_dir)

        with patch("app.a_share.volume.calculate_minute_volume_payload") as mock_calc:
            mock_calc.return_value = {"code": 200, "status": "ok"}

            # force=False 命中缓存，不执行计算
            get_a_share_volume_payload(volume_dir=self.volume_dir, force=False)
            mock_calc.assert_not_called()

            # force=True 穿透缓存，重新计算本地数据
            get_a_share_volume_payload(volume_dir=self.volume_dir, force=True)
            mock_calc.assert_called_once()

    def test_volume_payload_index_info_snapshot_and_saved_source(self):
        """index_info 仅在匹配当天时附加 snapshot，且保留使用已保存的 source"""
        date_today = "2026-11-09"
        date_hist = "2026-11-08"

        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        for d_str in (date_hist, date_today):
            day_rec = {
                "version": 1,
                "group_version": a_share_volume.GROUP_VERSION,
                "source": a_share_volume.SOURCE_NAME,
                "date": d_str,
                "updated_at": f"{d_str}T15:00:00+08:00",
                "data_time": "15:00",
                "complete_through": "15:00",
                "etfs": {
                    c: {
                        "name": a_share_volume.A_SHARE_ETF_METAS[c]["name"],
                        "market": a_share_volume.A_SHARE_ETF_METAS[c]["market"],
                        "source": a_share_volume.SOURCE_NAME,
                        "last_success_at": f"{d_str}T15:00:00+08:00",
                        "complete_through": "15:00",
                        "data": {m: 1000.0 for m in a_share_volume.STANDARD_TRADING_MINUTES}
                    }
                    for c in all_codes
                },
                "indices": {
                    "hs300": {
                        "code": "000300",
                        "name": "沪深300",
                        "source": "tencent_minute",
                        "pre_close": 3950.0,
                        "data": {"09:30": 3955.0},
                        "last_success_at": f"{d_str}T15:00:00+08:00",
                        "complete_through": "15:00"
                    }
                }
            }
            a_share_volume.save_day_file(self.volume_dir, d_str, day_rec)

        snap_hs300 = {
            "date": date_today,
            "time": f"{date_today}T10:00:00+08:00",
            "point": 3960.0,
            "pre_close": 3950.0,
            "change_pct": 0.25,
            "cumulative_amount": 5000000.0,
            "source": "tencent_quote"
        }
        with a_share_volume.volume_lock:
            a_share_volume.volume_runtime_state["snapshots"] = {"sh000300": snap_hs300}

        mock_now = datetime(2026, 11, 9, 10, 0, tzinfo=BEIJING_TZ)

        class MockDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return mock_now

        with patch("app.a_share.volume.datetime", MockDatetime):
            # 1. 历史日：snapshot 必须为 None
            res_hist = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_hist)
            hs300_hist = next(g for g in res_hist["groups"] if g["id"] == "hs300")
            self.assertIsNone(hs300_hist["index_info"]["snapshot"])
            self.assertEqual(hs300_hist["index_info"]["source"], "tencent_minute")

            # 2. 当天：snapshot 必须带入，且保留 saved source
            res_today = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_today)
            hs300_today = next(g for g in res_today["groups"] if g["id"] == "hs300")
            self.assertIsNotNone(hs300_today["index_info"]["snapshot"])
            self.assertEqual(hs300_today["index_info"]["snapshot"]["point"], 3960.0)
            self.assertEqual(hs300_today["index_info"]["source"], "tencent_minute")


if __name__ == "__main__":
    unittest.main()
