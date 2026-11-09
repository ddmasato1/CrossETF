#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_a_share_volume.py - A股重点宽基ETF一分钟成交额观察引擎单元测试

测试使用纯标准库、模拟上游与隔离临时档案，不写生产运行数据。

重点测试范围：
   - 未结束分钟及持久化 complete_through 边界检测与推进
   - 完整快照不被不完整快照覆盖
   - 10只ETF全齐备合计/累计与排行完全一致（单只累计之和等于总累计，占比和为100%）
   - 缺失数据严禁补0（组内、顶层、历史基准中位数及排行）
   - 历史基准算法严格为“逐日合计取中位数”，而非“单只中位数之和”
   - 历史指数前收绝不跨日套用最新日，最早日无可靠前收时涨跌幅保持 None
   - 交易日清理（保留最近60日）及 API 参数合法性校验
"""

import os
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import a_share_volume


class BaseVolumeTestCase(unittest.TestCase):
    """测试基类：提供隔离临时存储及运行时状态清理"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="crossetf_test_vol_")
        self.volume_dir = os.path.join(self.test_dir, "a_share_etf_volume")
        os.makedirs(self.volume_dir, exist_ok=True)
        a_share_volume.configure(self.test_dir)

        # 重置全局缓存和运行时状态
        with a_share_volume.volume_lock:
            a_share_volume.payload_cache["payload"] = None
            a_share_volume.payload_cache["date"] = None
            a_share_volume.payload_cache["timestamp"] = 0.0
            a_share_volume.volume_runtime_state["status"] = "uninitialized"
            a_share_volume.volume_runtime_state["status_desc"] = "测试初始化"
            a_share_volume.volume_runtime_state["last_error"] = ""
            a_share_volume.volume_runtime_state["last_success_at"] = ""
            a_share_volume.volume_runtime_state["etf_statuses"] = {}
            a_share_volume.volume_runtime_state["index_statuses"] = {}
            a_share_volume.volume_runtime_state["is_syncing"] = False

    def tearDown(self):
        # 恢复默认目录并清理临时文件夹
        a_share_volume.configure(None)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def create_mock_etf_day(self, date_str, complete_through="15:00", etf_amounts=None, missing_codes=None):
        """
        生成指定日期的 10 只 ETF 测试数据并保存。
        etf_amounts: { code: { minute: amount } } 或 { code: default_minute_amount }
        """
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        missing_set = set(missing_codes or [])
        etfs = {}

        for code in all_codes:
            if code in missing_set:
                continue
            meta = a_share_volume.A_SHARE_ETF_METAS[code]
            if isinstance(etf_amounts, dict):
                amt_spec = etf_amounts.get(code, 1000.0)
            elif etf_amounts is not None:
                amt_spec = etf_amounts
            else:
                amt_spec = 1000.0

            if isinstance(amt_spec, dict):
                data = dict(amt_spec)
            else:
                amt_val = float(amt_spec)
                data = {m: amt_val for m in a_share_volume.STANDARD_TRADING_MINUTES}

            etfs[code] = {
                "name": meta["name"],
                "market": meta["market"],
                "source": a_share_volume.SOURCE_NAME,
                "last_success_at": f"{date_str}T15:00:00+08:00",
                "complete_through": complete_through,
                "data": data
            }

        rec = {
            "version": 1,
            "group_version": a_share_volume.GROUP_VERSION,
            "source": a_share_volume.SOURCE_NAME,
            "date": date_str,
            "updated_at": f"{date_str}T15:00:00+08:00",
            "data_time": complete_through or "15:00",
            "complete_through": complete_through if len(etfs) == len(all_codes) else None,
            "etfs": etfs,
            "indices": {}
        }
        a_share_volume.save_day_file(self.volume_dir, date_str, rec)
        return rec


class TestTradingMinutesAndFormat(BaseVolumeTestCase):
    """交易分钟定义、金额格式化及基础正数校验测试"""

    def test_standard_trading_minutes_structure(self):
        """全天 241 个标准交易分钟结构：早盘 121 分钟，午盘 120 分钟，午休断开"""
        mins = a_share_volume.STANDARD_TRADING_MINUTES
        self.assertEqual(len(mins), 241)
        self.assertEqual(mins[0], "09:30")
        self.assertEqual(mins[120], "11:30")
        self.assertEqual(mins[121], "13:01")
        self.assertEqual(mins[-1], "15:00")

        # 严禁包含午休非交易分钟
        self.assertNotIn("11:31", mins)
        self.assertNotIn("12:00", mins)
        self.assertNotIn("13:00", mins)
        self.assertNotIn("09:29", mins)
        self.assertNotIn("15:01", mins)

    def test_format_amount_chinese(self):
        """中文金额格式化口径：亿元保留2位，万元四舍五入整取，元整取，异常值返回 --"""
        fmt = a_share_volume.format_amount_chinese
        self.assertEqual(fmt(None), "--")
        self.assertEqual(fmt(float("nan")), "--")
        self.assertEqual(fmt(float("inf")), "--")
        self.assertEqual(fmt(41203000000.0), "412.03 亿")
        self.assertEqual(fmt(123456789.0), "1.23 亿")
        self.assertEqual(fmt(50000.0), "5 万")
        self.assertEqual(fmt(12345.0), "1 万")
        self.assertEqual(fmt(800.0), "800 元")
        self.assertEqual(fmt(0.0), "0 元")

    def test_positive_finite(self):
        """数值过滤：仅接受有限正数，拒绝 0、负数、布尔值及非数字"""
        pf = a_share_volume.positive_finite
        self.assertEqual(pf(3500.5), 3500.5)
        self.assertEqual(pf("4113.41"), 4113.41)
        self.assertIsNone(pf(0))
        self.assertIsNone(pf(-100))
        self.assertIsNone(pf("-5.2"))
        self.assertIsNone(pf(True))   # Python 中 bool 是 int 子类，必须拦截
        self.assertIsNone(pf(False))
        self.assertIsNone(pf(float("nan")))
        self.assertIsNone(pf(float("inf")))
        self.assertIsNone(pf("invalid"))


class TestCompleteThroughBoundaries(BaseVolumeTestCase):
    """重点验证 1：未结束分钟及持久化 complete_through 边界"""

    def test_determine_complete_through_historical_day(self):
        """历史交易日：到达 15:00 确认为 15:00；早停或中途停机仅倒数第二分钟走完，不推定为 15:00"""
        mock_now = datetime(2026, 11, 9, 16, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.strptime = datetime.strptime

            # 正常收盘
            res_full = a_share_volume.determine_complete_through_for_date("2026-11-06", {"09:30": 1, "15:00": 1})
            self.assertEqual(res_full, "15:00")

            # 历史日早停（如停在 11:30），倒数第二分钟为已确认走完，绝不冒充 15:00
            res_interrupted = a_share_volume.determine_complete_through_for_date(
                "2026-11-06", {"09:30": 1, "09:31": 1, "11:29": 1, "11:30": 1}
            )
            self.assertEqual(res_interrupted, "11:29")

            # 历史日仅有单条记录，无法证明走完任何完整分钟，返回 None
            res_single = a_share_volume.determine_complete_through_for_date("2026-11-06", {"09:30": 1})
            self.assertIsNone(res_single)

            # 空字典返回 None
            self.assertIsNone(a_share_volume.determine_complete_through_for_date("2026-11-06", {}))

    def test_determine_complete_through_today_intraday(self):
        """今日盘中：当前分钟正在进行中，其上一分钟为已走完的完整分钟"""
        today_str = "2026-11-09"

        # 场景 A: 盘中 10:30，上游返回包含 10:30（进行中）
        now_1030 = datetime(2026, 11, 9, 10, 30, 15, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1030
            mock_dt.strptime = datetime.strptime
            mins = {"09:30": 1, "10:29": 1, "10:30": 1}
            cp = a_share_volume.determine_complete_through_for_date(today_str, mins)
            self.assertEqual(cp, "10:29")

        # 场景 B: 盘中 10:35，上游最新仅到 10:30（已走完，上游延迟）
        now_1035 = datetime(2026, 11, 9, 10, 35, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1035
            mock_dt.strptime = datetime.strptime
            mins = {"09:30": 1, "10:29": 1, "10:30": 1}
            cp = a_share_volume.determine_complete_through_for_date(today_str, mins)
            self.assertEqual(cp, "10:30")

        # 场景 C: 午休 12:00，早盘 11:30 已全部走完
        now_lunch = datetime(2026, 11, 9, 12, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_lunch
            mock_dt.strptime = datetime.strptime
            mins = {"09:30": 1, "11:30": 1}
            cp = a_share_volume.determine_complete_through_for_date(today_str, mins)
            self.assertEqual(cp, "11:30")

        # 场景 D: 收盘后 15:30，全天到达 15:00
        now_after_close = datetime(2026, 11, 9, 15, 30, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_after_close
            mock_dt.strptime = datetime.strptime
            mins = {"09:30": 1, "15:00": 1}
            cp = a_share_volume.determine_complete_through_for_date(today_str, mins)
            self.assertEqual(cp, "15:00")

    def test_complete_through_monotonicity_in_merge(self):
        """持久化 complete_through 保持单调递进，绝不倒退"""
        today_str = "2026-11-09"
        code = "510300"

        # 初始采集到 11:30
        now_1135 = datetime(2026, 11, 9, 11, 35, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1135
            mock_dt.strptime = datetime.strptime
            a_share_volume.merge_and_save_etf_data(
                self.volume_dir, code,
                {today_str: {m: 100.0 for m in a_share_volume.STANDARD_TRADING_MINUTES if m <= "11:30"}}
            )

        day_rec = a_share_volume.load_day_file(self.volume_dir, today_str)
        self.assertEqual(day_rec["etfs"][code]["complete_through"], "11:30")

        # 随后收到一个由于网络瞬断仅包含到 10:00 的落后快照
        now_1136 = datetime(2026, 11, 9, 11, 36, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1136
            mock_dt.strptime = datetime.strptime
            a_share_volume.merge_and_save_etf_data(
                self.volume_dir, code,
                {today_str: {m: 90.0 for m in a_share_volume.STANDARD_TRADING_MINUTES if m <= "10:00"}}
            )

        day_rec2 = a_share_volume.load_day_file(self.volume_dir, today_str)
        # 边界绝不倒退回 10:00
        self.assertEqual(day_rec2["etfs"][code]["complete_through"], "11:30")

    def test_day_complete_through_requires_all_10_etfs(self):
        """交易日总体的 complete_through 必须是全组 10 只 ETF 的最小完整边界，缺少任意一只即为 None"""
        hist_str = "2026-11-05"
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())

        # 仅写入 9 只 ETF（缺少 588080）
        self.create_mock_etf_day(hist_str, complete_through="15:00", missing_codes=["588080"])
        loaded = a_share_volume.load_day_file(self.volume_dir, hist_str)
        self.assertIsNone(loaded["complete_through"])

        # 补齐第 10 只，但其边界仅为 11:30（历史日未到15:00，倒数第二分钟11:29确认为走完）
        now_after = datetime(2026, 11, 9, 16, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_after
            mock_dt.strptime = datetime.strptime
            a_share_volume.merge_and_save_etf_data(
                self.volume_dir, "588080",
                {hist_str: {m: 50.0 for m in a_share_volume.STANDARD_TRADING_MINUTES if m <= "11:30"}}
            )

        loaded2 = a_share_volume.load_day_file(self.volume_dir, hist_str)
        # 总体边界取全组成员最小值 11:29（历史日前半段截断时倒数第二分钟确认走完）
        self.assertEqual(loaded2["complete_through"], "11:29")

    def test_load_day_file_sanitizes_unverified_complete_through(self):
        """老版本或外部文件若无有效 complete_through，绝不凭 15:00 键存在就推定完整"""
        today_str = "2026-11-05"
        raw_rec = {
            "version": 1,
            "date": today_str,
            "etfs": {
                "510300": {
                    "complete_through": "bad_string",
                    "data": {"15:00": 100.0}
                }
            }
        }
        fpath = os.path.join(self.volume_dir, f"{today_str}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(raw_rec, f)

        loaded = a_share_volume.load_day_file(self.volume_dir, today_str)
        self.assertIsNone(loaded["etfs"]["510300"]["complete_through"])
        self.assertIsNone(loaded["complete_through"])


class TestSnapshotOverwriteProtection(BaseVolumeTestCase):
    """重点验证 2：完整快照不被不完整快照覆盖"""

    def test_etf_confirmed_minutes_not_overwritten_by_incomplete_snapshot(self):
        """已确认完整的 ETF 分钟数据在后续接收到不完整快照时不得被冲掉"""
        date_str = "2026-11-06"
        code = "510300"

        # 1. 建立完整交易日（241分钟），每个分钟金额为 1000.0
        full_trends = {date_str: {m: 1000.0 for m in a_share_volume.STANDARD_TRADING_MINUTES}}
        now_close = datetime(2026, 11, 6, 15, 30, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_close
            mock_dt.strptime = datetime.strptime
            a_share_volume.merge_and_save_etf_data(self.volume_dir, code, full_trends)

        day_before = a_share_volume.load_day_file(self.volume_dir, date_str)
        self.assertEqual(len(day_before["etfs"][code]["data"]), 241)
        self.assertEqual(day_before["etfs"][code]["complete_through"], "15:00")
        self.assertEqual(day_before["etfs"][code]["data"]["14:59"], 1000.0)

        # 2. 模拟新快照发生故障：仅返回 09:30~10:00，且 10:00 正在进行中（金额变为半额 500.0）
        now_interrupted = datetime(2026, 11, 6, 10, 0, 30, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_interrupted
            mock_dt.strptime = datetime.strptime
            incomplete_trends = {date_str: {m: 500.0 for m in a_share_volume.STANDARD_TRADING_MINUTES if m <= "10:00"}}
            a_share_volume.merge_and_save_etf_data(self.volume_dir, code, incomplete_trends)

        day_after = a_share_volume.load_day_file(self.volume_dir, date_str)
        # 10:01 之后的分钟依然完整保留
        self.assertIn("14:59", day_after["etfs"][code]["data"])
        self.assertEqual(day_after["etfs"][code]["data"]["14:59"], 1000.0)
        self.assertEqual(len(day_after["etfs"][code]["data"]), 241)
        # 10:00 是新快照中的未确认分钟（new_cp 仅为 09:59），绝不能冲掉此前已确认的 1000.0 完整值
        self.assertEqual(day_after["etfs"][code]["data"]["10:00"], 1000.0)

    def test_index_confirmed_points_not_overwritten_by_incomplete_snapshot(self):
        """已确认完整的指数分时点位不被不完整快照覆盖"""
        date_str = "2026-11-06"
        group_id = "hs300"

        # 初始完整指数落盘（241分钟点位）
        full_points = {date_str: {m: 4000.0 + i for i, m in enumerate(a_share_volume.STANDARD_TRADING_MINUTES)}}
        now_close = datetime(2026, 11, 6, 15, 30, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_close
            mock_dt.strptime = datetime.strptime
            a_share_volume.merge_and_save_index_data(self.volume_dir, group_id, full_points, latest_pre_close=3950.0)

        # 随后收到残缺的指数快照
        partial_points = {date_str: {"09:30": 3999.0, "10:00": 4010.0}}
        now_midday = datetime(2026, 11, 6, 10, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_midday
            mock_dt.strptime = datetime.strptime
            a_share_volume.merge_and_save_index_data(self.volume_dir, group_id, partial_points)

        loaded = a_share_volume.load_day_file(self.volume_dir, date_str)
        idx_obj = loaded["indices"][group_id]
        self.assertEqual(len(idx_obj["data"]), 241)
        self.assertEqual(idx_obj["complete_through"], "15:00")
        self.assertEqual(idx_obj["data"]["15:00"], 4240.0)
        self.assertEqual(idx_obj["pre_close"], 3950.0)


class TestTenEtfsAggregateAndRankConsistency(BaseVolumeTestCase):
    """重点验证 3：10只ETF齐备合计/累计和排行一致性红线"""

    def test_10_etfs_all_present_aggregate_and_ranking_consistency(self):
        """
        10只ETF全部齐备时：
        - 顶层 aggregate 分钟金额 = 4 个指数组分钟金额之和
        - 顶层 aggregate.cumulative_amount = 共同完整分钟合计之和
        - aggregate.etfs 包含全部 10 只 ETF，按 cumulative_amount 降序排序
        - 核心红线：所有 10 只 ETF 的 cumulative_amount 之和必须严格等于 aggregate.cumulative_amount
        - 各 ETF 占比 share_pct 之和约等于 100%
        """
        date_str = "2026-11-06"
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        self.assertEqual(len(all_codes), 10)

        # 为 10 只 ETF 分配不同量级的每分钟成交额，以产生明确排行
        # 510300: 1000元/分, 510310: 900元/分, 159919: 800元/分, ...
        etf_rates = {code: float(1000 - i * 50) for i, code in enumerate(all_codes)}
        self.create_mock_etf_day(date_str, complete_through="15:00", etf_amounts=etf_rates)

        # 读取 payload（查询历史日）
        now_test = datetime(2026, 11, 9, 18, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_test
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_str)

        self.assertEqual(payload["code"], 200)
        groups = payload["groups"]
        agg = payload["aggregate"]
        self.assertIsNotNone(agg)

        # 1. 监控标的名单齐备性
        self.assertEqual(agg["etf_count"], 10)
        self.assertEqual(set(agg["codes"]), set(all_codes))

        # 2. 逐分钟组加总与 aggregate 加总严格相等
        for m_idx in range(241):
            m_str = a_share_volume.STANDARD_TRADING_MINUTES[m_idx]
            group_sum = round(sum(g["minutes"][m_idx]["amount"] for g in groups), 2)
            agg_amt = agg["minutes"][m_idx]["amount"]
            self.assertIsNotNone(agg_amt)
            self.assertEqual(agg_amt, group_sum, f"Minute {m_str} aggregate mismatch")
            self.assertTrue(agg["minutes"][m_idx]["is_complete"])

        # 3. 累计完整性与完成分钟数
        self.assertEqual(agg["expected_minutes_count"], 241)
        self.assertEqual(agg["completed_minutes_count"], 241)
        self.assertTrue(agg["is_cumulative_complete"])
        self.assertEqual(agg["data_time"], "15:00")

        # 4. 核心一致性红线：单只 ETF 累计金额之和严格等于 aggregate 总累计金额
        ranking_etfs = agg["etfs"]
        self.assertEqual(len(ranking_etfs), 10)
        ranking_sum = round(sum(item["cumulative_amount"] for item in ranking_etfs), 2)
        self.assertEqual(ranking_sum, agg["cumulative_amount"])

        # 5. 排行降序校验与代码平局升序
        prev_amt = float("inf")
        for item in ranking_etfs:
            self.assertIsNotNone(item["cumulative_amount"])
            self.assertLessEqual(item["cumulative_amount"], prev_amt)
            prev_amt = item["cumulative_amount"]

        # 首名应为单分钟额度最大的 510210 或 510300
        expected_sorted_codes = sorted(all_codes, key=lambda c: (-etf_rates[c] * 241, c))
        actual_codes = [item["code"] for item in ranking_etfs]
        self.assertEqual(actual_codes, expected_sorted_codes)

        # 6. 占比之和约 100%
        share_sum = sum(item["share_pct"] for item in ranking_etfs)
        self.assertAlmostEqual(share_sum, 100.0, places=1)

    def test_ranking_tie_breaker_by_code_ascending(self):
        """排行平局：累计金额相同时严格按代码升序排序"""
        date_str = "2026-11-06"
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        # 所有 10 只 ETF 均设定为相同成交额
        self.create_mock_etf_day(date_str, complete_through="15:00", etf_amounts=500.0)

        now_test = datetime(2026, 11, 9, 18, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_test
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_str)

        ranking_codes = [e["code"] for e in payload["aggregate"]["etfs"]]
        self.assertEqual(ranking_codes, sorted(all_codes))

    def test_legacy_etfs_excluded_from_aggregate_and_groups(self):
        """旧日档中遗留的旧 ETF 记录保留在 etfs 字典中，但绝不计入当前 10 只合计或任何指数组"""
        date_str = "2026-11-06"
        day_rec = self.create_mock_etf_day(date_str, complete_through="15:00", etf_amounts=1000.0)

        # 向日档中人为注入旧监控标的 159925 (沪深300ETF南方，已移出监控)
        day_rec["etfs"]["159925"] = {
            "name": "沪深300ETF南方",
            "market": "sz",
            "source": a_share_volume.SOURCE_NAME,
            "last_success_at": "2026-11-06T15:00:00+08:00",
            "complete_through": "15:00",
            "data": {m: 99999999.0 for m in a_share_volume.STANDARD_TRADING_MINUTES}
        }
        a_share_volume.save_day_file(self.volume_dir, date_str, day_rec)

        now_test = datetime(2026, 11, 9, 18, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_test
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_str)

        agg = payload["aggregate"]
        self.assertEqual(agg["etf_count"], 10)
        self.assertNotIn("159925", agg["codes"])
        self.assertNotIn("159925", [e["code"] for e in agg["etfs"]])

        hs300_grp = [g for g in payload["groups"] if g["id"] == "hs300"][0]
        self.assertEqual(hs300_grp["etf_count"], 4)
        self.assertNotIn("159925", hs300_grp["codes"])


class TestMissingDataNotZeroFilled(BaseVolumeTestCase):
    """重点验证 4：缺失数据绝不补 0"""

    def test_missing_minute_in_single_etf_makes_group_and_aggregate_none(self):
        """若 10 只中任意 1 只 ETF 某分钟缺失或无效，组和顶层合计该分钟均置为 None，严禁填0"""
        date_str = "2026-11-06"
        # 510300 (沪深300中1只) 缺少 10:00 分钟数据
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        custom_amounts = {}
        for code in all_codes:
            m_data = {m: 1000.0 for m in a_share_volume.STANDARD_TRADING_MINUTES}
            if code == "510300":
                del m_data["10:00"]
            custom_amounts[code] = m_data

        self.create_mock_etf_day(date_str, complete_through="15:00", etf_amounts=custom_amounts)

        now_test = datetime(2026, 11, 9, 18, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_test
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_str)

        # 沪深300 在 10:00 必须为 None
        hs300 = [g for g in payload["groups"] if g["id"] == "hs300"][0]
        min_1000_hs300 = [m for m in hs300["minutes"] if m["time"] == "10:00"][0]
        self.assertIsNone(min_1000_hs300["amount"])
        self.assertEqual(min_1000_hs300["amount_str"], "--")
        self.assertFalse(min_1000_hs300["is_complete"])

        # 顶层 aggregate 在 10:00 必须为 None，严禁把其余9只相加冒充合计
        agg = payload["aggregate"]
        min_1000_agg = [m for m in agg["minutes"] if m["time"] == "10:00"][0]
        self.assertIsNone(min_1000_agg["amount"])
        self.assertEqual(min_1000_agg["amount_str"], "--")
        self.assertFalse(min_1000_agg["is_complete"])

        # 该分钟不计入累计完成分钟
        self.assertEqual(agg["completed_minutes_count"], 240)
        self.assertFalse(agg["is_cumulative_complete"])

        # 排行榜依然保持严格一致：单只累计之和依然等于 aggregate 累计总额（排除 10:00）
        ranking_sum = round(sum(e["cumulative_amount"] for e in agg["etfs"]), 2)
        self.assertEqual(ranking_sum, agg["cumulative_amount"])

    def test_missing_etf_in_historical_day_excluded_from_baseline_not_zero(self):
        """历史日某 ETF 缺失时，该日直接剔除出历史基准样本池，严禁按 0 算入中位数"""
        hist_day_1 = {
            "etfs": {
                "510210": {"complete_through": "15:00", "data": {"10:00": 100.0}},
                "510760": {"complete_through": "15:00", "data": {"10:00": 200.0}}
            }
        }
        # hist_day_2 中 510760 缺失
        hist_day_2 = {
            "etfs": {
                "510210": {"complete_through": "15:00", "data": {"10:00": 500.0}}
            }
        }
        # hist_day_3 完整
        hist_day_3 = {
            "etfs": {
                "510210": {"complete_through": "15:00", "data": {"10:00": 300.0}},
                "510760": {"complete_through": "15:00", "data": {"10:00": 300.0}}
            }
        }

        med, n = a_share_volume.compute_baseline_median(
            [hist_day_1, hist_day_2, hist_day_3],
            ["510210", "510760"],
            "10:00"
        )
        # 有效样本仅 2 天（Day 1: 300, Day 3: 600），中位数为 (300 + 600) / 2 = 450.0
        # 若错误将 Day 2 补 0 视作 500+0=500，则样本会变成 [300, 500, 600]，中位数会错判为 500
        self.assertEqual(n, 2)
        self.assertEqual(med, 450.0)

    def test_zero_completed_minutes_produces_none_ranking_not_fake_zero(self):
        """全天无共同完整分钟时，单只累计金额为 None，绝不伪造 0.0 排行"""
        date_str = "2026-11-06"
        # 建立一个完全没有完整分钟的交易日文件
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        etfs = {}
        for code in all_codes:
            etfs[code] = {
                "name": code,
                "market": "sh",
                "source": "test",
                "complete_through": None,
                "data": {}
            }
        rec = {
            "version": 1, "group_version": "v2.0", "source": "test",
            "date": date_str, "updated_at": "", "data_time": "",
            "complete_through": None, "etfs": etfs, "indices": {}
        }
        a_share_volume.save_day_file(self.volume_dir, date_str, rec)

        now_test = datetime(2026, 11, 9, 18, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_test
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=date_str)

        agg = payload["aggregate"]
        self.assertEqual(agg["completed_minutes_count"], 0)
        self.assertEqual(agg["cumulative_amount"], 0.0)
        for item in agg["etfs"]:
            self.assertIsNone(item["cumulative_amount"])
            self.assertEqual(item["cumulative_amount_str"], "--")
            self.assertIsNone(item["share_pct"])


class TestHistoricalBaselineMedianCalculation(BaseVolumeTestCase):
    """重点验证 5：历史基准严格按逐日合计取中位数"""

    def test_daily_sum_then_median_vs_sum_of_medians_mathematical_proof(self):
        """
        数学证明级验证：先逐日合计取中位数 != 先取单只中位数再相加
        以创业板指组（159915, 159952）为例，设置 3 个历史交易日：
          - Day 1: 159915 = 10.0,  159952 = 100.0 -> 日合计 = 110.0
          - Day 2: 159915 = 20.0,  159952 = 20.0  -> 日合计 = 40.0
          - Day 3: 159915 = 100.0, 159952 = 10.0  -> 日合计 = 110.0

        正确算法（逐日合计的中位数）：
          日合计序列为 [40.0, 110.0, 110.0]，中位数严格为 110.0。

        错误算法（各 ETF 中位数之和）：
          159915 中位数：[10, 20, 100] -> 20.0
          159952 中位数：[10, 20, 100] -> 20.0
          中位数之和 = 20.0 + 20.0 = 40.0！

        断言：引擎必须返回 110.0，绝不能返回 40.0！
        """
        hist_records = [
            {
                "etfs": {
                    "159915": {"complete_through": "15:00", "data": {"10:00": 10.0}},
                    "159952": {"complete_through": "15:00", "data": {"10:00": 100.0}}
                }
            },
            {
                "etfs": {
                    "159915": {"complete_through": "15:00", "data": {"10:00": 20.0}},
                    "159952": {"complete_through": "15:00", "data": {"10:00": 20.0}}
                }
            },
            {
                "etfs": {
                    "159915": {"complete_through": "15:00", "data": {"10:00": 100.0}},
                    "159952": {"complete_through": "15:00", "data": {"10:00": 10.0}}
                }
            }
        ]

        med, n = a_share_volume.compute_baseline_median(hist_records, ["159915", "159952"], "10:00")
        self.assertEqual(n, 3)
        self.assertEqual(med, 110.0)
        self.assertNotEqual(med, 40.0)

    def test_baseline_even_number_of_days_average(self):
        """偶数天历史基准取两中间值平均"""
        hist_records = [
            {"etfs": {"510210": {"complete_through": "15:00", "data": {"10:00": 10.0}}}},
            {"etfs": {"510210": {"complete_through": "15:00", "data": {"10:00": 20.0}}}},
            {"etfs": {"510210": {"complete_through": "15:00", "data": {"10:00": 30.0}}}},
            {"etfs": {"510210": {"complete_through": "15:00", "data": {"10:00": 40.0}}}}
        ]
        med, n = a_share_volume.compute_baseline_median(hist_records, ["510210"], "10:00")
        self.assertEqual(n, 4)
        self.assertEqual(med, 25.0)

    def test_baseline_excludes_target_day_itself(self):
        """历史基准严格取 target_date 之前的有效交易日，排除自身与未来日期"""
        dates = ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"]
        for d in dates:
            self.create_mock_etf_day(d)

        prior = a_share_volume.get_prior_trading_dates(self.volume_dir, target_date="2026-11-06", max_days=20)
        self.assertEqual(prior, ["2026-11-05", "2026-11-04", "2026-11-03"])
        self.assertNotIn("2026-11-06", prior)
        self.assertNotIn("2026-11-07", prior)

    def test_baseline_insufficient_days_honest_reporting(self):
        """历史日不足 20 日时如实展示实际样本 N，绝不伪造 20 日"""
        self.create_mock_etf_day("2026-11-04")
        self.create_mock_etf_day("2026-11-05")
        self.create_mock_etf_day("2026-11-06")

        now_test = datetime(2026, 11, 9, 18, 0, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_test
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date="2026-11-06")

        base_info = payload["baseline_info"]
        self.assertEqual(base_info["max_days"], 20)
        self.assertEqual(base_info["actual_days"], 2)
        self.assertIn("基于前 2 个有效交易日", base_info["description"])
        self.assertIn("上限20日", base_info["description"])

    def test_baseline_complete_through_boundary_filtering(self):
        """历史日某 ETF 若早停（complete_through=11:30），则 13:00 之后的分钟自动剔除该日样本"""
        hist_rec_full = {
            "etfs": {"510210": {"complete_through": "15:00", "data": {"11:00": 100.0, "14:00": 200.0}}}
        }
        hist_rec_interrupted = {
            "etfs": {"510210": {"complete_through": "11:30", "data": {"11:00": 300.0, "14:00": 999.0}}}
        }

        # 11:00 时两者均完整，样本为 2
        med_11, n_11 = a_share_volume.compute_baseline_median([hist_rec_full, hist_rec_interrupted], ["510210"], "11:00")
        self.assertEqual(n_11, 2)
        self.assertEqual(med_11, 200.0)

        # 14:00 时早停日超过边界被剔除，样本仅为 1（仅取 200.0，绝不把 999.0 当作完整样本）
        med_14, n_14 = a_share_volume.compute_baseline_median([hist_rec_full, hist_rec_interrupted], ["510210"], "14:00")
        self.assertEqual(n_14, 1)
        self.assertEqual(med_14, 200.0)


class TestHistoricalIndexPreviousClose(BaseVolumeTestCase):
    """重点验证 6：历史指数前收绝不能跨日套用最新日"""

    def test_merge_and_save_index_data_pre_close_isolation(self):
        """
        多日指数同步时：
        - latest_pre_close 仅且只能赋予最新交易日
        - 历史交易日的前收，只能来源于前一个返回交易日的 15:00 收盘点位
        - 最早历史交易日（pos == 0）由于没有更早一日，前收必须保持 None，绝不能套用最新日前收
        """
        group_id = "hs300"
        trends_by_date = {
            "2026-11-04": {"09:30": 3800.0, "15:00": 3850.0},
            "2026-11-05": {"09:30": 3860.0, "15:00": 3900.0},
            "2026-11-06": {"09:30": 3920.0, "15:00": 3950.0}
        }
        now_close = datetime(2026, 11, 6, 15, 30, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_close
            mock_dt.strptime = datetime.strptime
            # 上游返回的 latest_pre_close 为 3910.0（仅适用于 2026-11-06）
            a_share_volume.merge_and_save_index_data(
                self.volume_dir, group_id, trends_by_date, latest_pre_close=3910.0
            )

        day_04 = a_share_volume.load_day_file(self.volume_dir, "2026-11-04")
        day_05 = a_share_volume.load_day_file(self.volume_dir, "2026-11-05")
        day_06 = a_share_volume.load_day_file(self.volume_dir, "2026-11-06")

        # 1. 最早交易日 11-04 无前一日真实收盘，pre_close 严格保持 None，绝不能被 3910.0 污染
        self.assertIsNone(day_04["indices"][group_id]["pre_close"])

        # 2. 历史交易日 11-05 取 11-04 的 15:00 收盘点位 3850.0
        self.assertEqual(day_05["indices"][group_id]["pre_close"], 3850.0)

        # 3. 最新交易日 11-06 取 latest_pre_close 3910.0
        self.assertEqual(day_06["indices"][group_id]["pre_close"], 3910.0)

    def test_earliest_historical_day_index_change_pct_is_none(self):
        """最早历史交易日由于前收为 None，涨跌幅必须明确留空（None），绝不以开盘价代替或套用最新日前收"""
        group_id = "hs300"
        # 写入 11-04 数据，无可靠前收
        trends_04 = {"2026-11-04": {m: 4000.0 for m in a_share_volume.STANDARD_TRADING_MINUTES}}
        now_close = datetime(2026, 11, 6, 15, 30, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_close
            mock_dt.strptime = datetime.strptime
            self.create_mock_etf_day("2026-11-04")
            a_share_volume.merge_and_save_index_data(self.volume_dir, group_id, trends_04, latest_pre_close=None)

            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date="2026-11-04")

        hs300_grp = [g for g in payload["groups"] if g["id"] == "hs300"][0]
        idx_info = hs300_grp["index_info"]
        self.assertIsNone(idx_info["pre_close"])

        for minute_obj in hs300_grp["minutes"]:
            self.assertEqual(minute_obj["index_value"], 4000.0)
            self.assertIsNone(minute_obj["index_change_pct"])

    def test_existing_reliable_pre_close_not_overwritten_by_none(self):
        """如果某日已有落盘的可靠前收，后续如果同步返回 None，绝不冲掉已有的可靠前收"""
        group_id = "hs300"
        now_close = datetime(2026, 11, 6, 15, 30, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_close
            mock_dt.strptime = datetime.strptime
            # 首次建立，带有前收 3880.0
            a_share_volume.merge_and_save_index_data(
                self.volume_dir, group_id,
                {"2026-11-05": {"15:00": 3900.0}},
                latest_pre_close=3880.0
            )

            # 再次合并时作为最早日传入（pre_close 计算为 None）
            a_share_volume.merge_and_save_index_data(
                self.volume_dir, group_id,
                {"2026-11-05": {"15:00": 3900.0}},
                latest_pre_close=None
            )

        loaded = a_share_volume.load_day_file(self.volume_dir, "2026-11-05")
        self.assertEqual(loaded["indices"][group_id]["pre_close"], 3880.0)


class TestApiValidationAndStorageManagement(BaseVolumeTestCase):
    """API 参数合法性、数据生命周期与异常防御"""

    def test_invalid_target_date_format_returns_400(self):
        """日期格式非法直接返回 HTTP 400"""
        for bad_date in ["20261106", "2026/11/06", "invalid-date", "2026-1-1"]:
            with self.subTest(date=bad_date):
                res = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=bad_date)
                self.assertEqual(res["code"], 400)
                self.assertEqual(res["status"], "error")
                self.assertIn("日期格式错误", res["message"])

    def test_nonexistent_target_date_returns_404(self):
        """请求未归档的日期返回 HTTP 404"""
        self.create_mock_etf_day("2026-11-06")
        res = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date="2020-01-01")
        self.assertEqual(res["code"], 404)
        self.assertEqual(res["status"], "error")
        self.assertIn("未找到交易日", res["message"])

    def test_empty_storage_default_payload(self):
        """本地无任何档案时返回友好就绪载荷"""
        res = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=None)
        self.assertEqual(res["code"], 200)
        self.assertEqual(res["available_dates"], [])
        self.assertEqual(res["groups"], [])
        self.assertIsNone(res["aggregate"])
        self.assertEqual(res["baseline_info"]["actual_days"], 0)

    def test_retention_cleanup_preserves_most_recent_60_days(self):
        """历史日文件清理严格保留按日期倒序排名的最近 60 个交易日，删除超出部分"""
        # 生成 65 个连续日文件
        base_date = datetime(2026, 1, 1)
        created_dates = []
        for i in range(65):
            d_str = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
            created_dates.append(d_str)
            self.create_mock_etf_day(d_str)

        files_before = a_share_volume.get_available_dates(self.volume_dir)
        self.assertEqual(len(files_before), 65)

        # 执行清理
        a_share_volume.cleanup_old_trading_days(self.volume_dir, keep_days=60)

        files_after = a_share_volume.get_available_dates(self.volume_dir)
        self.assertEqual(len(files_after), 60)
        # 保留的必须是最新（最大）的 60 个
        expected_kept = sorted(created_dates, reverse=True)[:60]
        self.assertEqual(files_after, expected_kept)

        # 最旧的 5 个必须被删除
        for old_d in sorted(created_dates, reverse=True)[60:]:
            self.assertFalse(os.path.exists(os.path.join(self.volume_dir, f"{old_d}.json")))

    def test_parse_trends_strict_validations(self):
        """parse_trends 边界校验：过滤未来日期、非法时间窗口、负数及畸形记录"""
        today_bj_str = datetime.now(a_share_volume.BEIJING_TZ).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now(a_share_volume.BEIJING_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

        raw_resp = {
            "rc": 0,
            "data": {
                "trends": [
                    f"2026-11-06 09:30,1,2,3,4,100,5000000",             # 正常
                    f"2026-11-06 09:29,1,2,3,4,100,5000000",             # 非法时间（开盘前）
                    f"2026-11-06 11:31,1,2,3,4,100,5000000",             # 非法时间（午休）
                    f"2026-11-06 15:01,1,2,3,4,100,5000000",             # 非法时间（收盘后）
                    f"2026-11-06 10:00,1,2,3,4,100,-1000",               # 负成交额
                    f"2026-11-06 10:01,1,2,3,4,100,nan",                 # NaN
                    f"{tomorrow_str} 10:00,1,2,3,4,100,5000000",         # 未来交易日
                    f"2019-12-31 10:00,1,2,3,4,100,5000000",             # 2020 之前早于支持范围
                    "malformed_string_without_commas"                     # 畸形字符串
                ]
            }
        }
        parsed = a_share_volume.parse_trends(raw_resp)
        self.assertIn("2026-11-06", parsed)
        self.assertEqual(list(parsed["2026-11-06"].keys()), ["09:30"])
        self.assertEqual(parsed["2026-11-06"]["09:30"], 5000000.0)


class TestIntradayActiveSessionBehavior(BaseVolumeTestCase):
    """盘中交易时段进行中分钟状态与累计边界测试"""

    def test_intraday_ongoing_minute_excluded_from_finished_and_cumulative(self):
        """
        盘中交易时段（如 10:30）：
        - 10:30 作为当前进行中分钟，is_unfinished 置为 True，is_complete 置为 False
        - 10:30 不计算放大倍数 ratio（ratio=None）
        - 10:30 不参与 finished_complete_minutes 累计
        - completed_minutes_count 与 expected_minutes_count 均为 60（09:30~10:29 共60分钟），is_cumulative_complete 为 True
        - 未来分钟（10:31起）amount 必须为 None，绝不显示脏数据
        """
        today_str = "2026-11-09"
        # 准备数据至 10:30，10:30 前 complete_through 标记为 10:29
        all_codes = list(a_share_volume.A_SHARE_ETF_METAS.keys())
        custom_amounts = {}
        for code in all_codes:
            m_data = {m: 1000.0 for m in a_share_volume.STANDARD_TRADING_MINUTES if m <= "10:30"}
            custom_amounts[code] = m_data

        self.create_mock_etf_day(today_str, complete_through="10:29", etf_amounts=custom_amounts)

        now_1030 = datetime(2026, 11, 9, 10, 30, 20, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1030
            mock_dt.strptime = datetime.strptime
            payload = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=today_str)

        agg = payload["aggregate"]
        # 1. 10:29 为走完的完整分钟
        min_1029 = [m for m in agg["minutes"] if m["time"] == "10:29"][0]
        self.assertTrue(min_1029["is_complete"])
        self.assertFalse(min_1029["is_unfinished"])
        self.assertEqual(min_1029["amount"], 10000.0)

        # 2. 10:30 为当前进行中分钟
        min_1030 = [m for m in agg["minutes"] if m["time"] == "10:30"][0]
        self.assertFalse(min_1030["is_complete"])
        self.assertTrue(min_1030["is_unfinished"])
        self.assertEqual(min_1030["amount"], 10000.0)  # 进行中有暂态金额

        # 3. 10:31 为未来未到达分钟
        min_1031 = [m for m in agg["minutes"] if m["time"] == "10:31"][0]
        self.assertFalse(min_1031["is_complete"])
        self.assertFalse(min_1031["is_unfinished"])
        self.assertIsNone(min_1031["amount"])

        # 4. 累计完成统计：截至 10:29 共 60 分钟全部齐备
        self.assertEqual(agg["expected_minutes_count"], 60)
        self.assertEqual(agg["completed_minutes_count"], 60)
        self.assertTrue(agg["is_cumulative_complete"])
        self.assertEqual(agg["data_time"], "10:29")
        self.assertEqual(agg["cumulative_amount"], 60 * 10000.0)

    def test_intraday_at_1500_vs_1501_boundary_state(self):
        """15:00仍只要求240分钟；15:01须收到完整快照，不能仅靠时间升级半分钟。"""
        today_str = "2026-11-09"
        snapshot = {today_str: {m: 1000.0 for m in a_share_volume.STANDARD_TRADING_MINUTES}}
        now_1500 = datetime(2026, 11, 9, 15, 0, 30, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1500
            mock_dt.strptime = datetime.strptime
            for code in a_share_volume.A_SHARE_ETF_METAS:
                a_share_volume.merge_and_save_etf_data(self.volume_dir, code, snapshot)
            saved = a_share_volume.load_day_file(self.volume_dir, today_str)
            self.assertEqual(saved["complete_through"], "14:59")
            payload_1500 = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=today_str)

        agg_1500 = payload_1500["aggregate"]
        self.assertTrue(agg_1500["minutes"][-1]["is_unfinished"])
        self.assertFalse(agg_1500["minutes"][-1]["is_complete"])
        self.assertEqual(agg_1500["expected_minutes_count"], 240)
        self.assertEqual(agg_1500["completed_minutes_count"], 240)
        self.assertTrue(agg_1500["is_cumulative_complete"])

        now_1501 = datetime(2026, 11, 9, 15, 1, 0, tzinfo=a_share_volume.BEIJING_TZ)
        with patch("a_share_volume.datetime") as mock_dt:
            mock_dt.now.return_value = now_1501
            mock_dt.strptime = datetime.strptime
            stale = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=today_str)["aggregate"]
            self.assertEqual(stale["expected_minutes_count"], 241)
            self.assertEqual(stale["completed_minutes_count"], 240)
            self.assertFalse(stale["is_cumulative_complete"])
            self.assertFalse(stale["minutes"][-1]["is_complete"])
            for code in a_share_volume.A_SHARE_ETF_METAS:
                a_share_volume.merge_and_save_etf_data(self.volume_dir, code, snapshot)
            payload_1501 = a_share_volume.calculate_minute_volume_payload(self.volume_dir, target_date=today_str)

        agg_1501 = payload_1501["aggregate"]
        self.assertFalse(agg_1501["minutes"][-1]["is_unfinished"])
        self.assertTrue(agg_1501["minutes"][-1]["is_complete"])
        self.assertEqual(agg_1501["expected_minutes_count"], 241)
        self.assertEqual(agg_1501["completed_minutes_count"], 241)
        self.assertTrue(agg_1501["is_cumulative_complete"])


class TestSyncAllEtfsMockPipeline(BaseVolumeTestCase):
    """sync_all_etfs 调度编排与错误状态降级测试（纯 mock，零网络）"""

    def test_sync_all_etfs_success(self):
        """全部 10 只 ETF 与 4 个指数正常返回时，状态为 online_fresh，数据落盘并按日可用"""
        today_str = datetime.now(a_share_volume.BEIJING_TZ).strftime("%Y-%m-%d")

        def mock_fetch(secid, ndays=1, timeout=5):
            # 构造 trends 响应，ETF 与指数统一满足解析字段长度
            trends = [f"{today_str} {m},3000,3001,3002,2999,100,5000000" for m in ["09:30", "15:00"]]
            return {
                "rc": 0,
                "data": {
                    "preClose": 2990.0,
                    "trends": trends
                }
            }

        with patch("a_share_volume.fetch_trends_raw", side_effect=mock_fetch):
            status_enum, updated_dates, msg, etf_rep = a_share_volume.sync_all_etfs(
                self.volume_dir, ndays=1, throttle_delay=0
            )

        self.assertEqual(status_enum, "online_fresh")
        self.assertIn("全部10只ETF同步成功", msg)
        self.assertIn("全部4个指数同步成功", msg)
        self.assertIn(today_str, updated_dates)
        self.assertEqual(len(etf_rep), 10)
        for code, rep in etf_rep.items():
            self.assertEqual(rep["status"], "ok")

        day_file = a_share_volume.load_day_file(self.volume_dir, today_str)
        self.assertIsNotNone(day_file)
        self.assertEqual(len(day_file["etfs"]), 10)
        self.assertEqual(len(day_file["indices"]), 4)

    def test_sync_all_etfs_partial_failure(self):
        """部分标的上游故障时降级为 partial_degraded，成功标的依然正常落盘"""
        today_str = datetime.now(a_share_volume.BEIJING_TZ).strftime("%Y-%m-%d")

        def mock_fetch(secid, ndays=1, timeout=5):
            # 令 510300 失败
            if "510300" in secid:
                return None
            trends = [f"{today_str} 09:30,3000,3001,3002,2999,100,5000000"]
            return {"rc": 0, "data": {"preClose": 3000.0, "trends": trends}}

        with patch("a_share_volume.fetch_trends_raw", side_effect=mock_fetch):
            status_enum, updated_dates, msg, etf_rep = a_share_volume.sync_all_etfs(
                self.volume_dir, ndays=1, throttle_delay=0
            )

        self.assertEqual(status_enum, "partial_degraded")
        self.assertEqual(etf_rep["510300"]["status"], "failed")
        self.assertEqual(etf_rep["510210"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
