"""日成交额回归：隔离档案、模拟上游，不读取生产数据。"""
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

from app.a_share import daily


class DailyHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "daily", "history.json")
        for name, value in (("HISTORY_FILE", self.path), ("runtime_state", {"is_syncing": False, "last_error": ""})):
            setting = patch.object(daily, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        setting = patch.object(daily.time, "sleep")
        setting.start()
        self.addCleanup(setting.stop)
        self.codes = list(daily.A_SHARE_ETF_METAS)

    def now(self, hour=16):
        return datetime(2026, 11, 9, hour, tzinfo=daily.BEIJING_TZ)

    def archive(self, days=("2026-11-05", "2026-11-06")):
        return {
            "version": 1, "start_date": "2023-11-07", "status": "ready",
            "trading_dates": list(days), "last_success_at": "old-success",
            "index": {"closes": {day: 3000 + i for i, day in enumerate(days)}},
            "etfs": {code: {"first_trade_date": "2010-01-01", "amounts": {day: (i + 1) * 100 for day in days}}
                     for i, code in enumerate(self.codes)},
        }

    def upstream(self, days=("2026-11-05", "2026-11-06")):
        def fetch(secid, start, end, value_index=6):
            if secid == "1.000001":
                return {day: 3000 + i for i, day in enumerate(days)}, None
            return {day: 100 for day in days}, "2010-01-01"
        return fetch

    def test_close_cutoff_and_weekends(self):
        for stamp, expected in (
            ("2026-11-06T15:59:00", "2026-11-05"),
            ("2026-11-06T16:00:00", "2026-11-06"),
            ("2026-11-07T18:00:00", "2026-11-06"),
            ("2026-11-08T12:00:00", "2026-11-06"),
            ("2026-11-09T09:00:00", "2026-11-06"),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(daily.completed_cutoff(datetime.fromisoformat(stamp)).isoformat(), expected)
        self.assertEqual(daily.three_years_before(date(2024, 2, 29)), date(2021, 2, 28))

    def test_daily_amount_and_index_close_are_distinct_fields(self):
        raw = {"rc": 0, "data": {"code": "510300", "dktotal": 2, "klines": [
            "2026-11-05,3,3.5,4,2,123,987654.32,1",
            "2026-11-06,3,3.6,4,2,0,0,1",
        ]}}
        with patch.object(daily, "read_upstream", return_value=json.dumps(raw).encode()):
            amounts, origin = daily.fetch_daily("1.510300", "2026-11-05", "2026-11-06")
            closes, _ = daily.fetch_daily("1.510300", "2026-11-05", "2026-11-06", value_index=2)
        self.assertEqual(amounts, {"2026-11-05": 987654.32, "2026-11-06": 0})
        self.assertEqual(closes, {"2026-11-05": 3.5, "2026-11-06": 3.6})
        self.assertEqual(origin, "2026-11-05")

    def test_bad_upstream_values_and_wrong_security_are_rejected(self):
        for code, amount in (("WRONG", "100"), ("510300", "NaN"), ("510300", "inf"), ("510300", "-1")):
            with self.subTest(code=code, amount=amount):
                raw = {"rc": 0, "data": {"code": code, "klines": [f"2026-11-06,3,4,4,2,5,{amount},1"]}}
                with patch.object(daily, "read_upstream", return_value=json.dumps(raw).encode()):
                    with self.assertRaises(ValueError):
                        daily.fetch_daily("1.510300", "2026-11-06", "2026-11-06")
        raw["data"]["code"] = "510300"
        raw["data"]["klines"] = ["2026-11-06,3,0,4,2,5,100,1"]
        with patch.object(daily, "read_upstream", return_value=json.dumps(raw).encode()):
            with self.assertRaises(ValueError):
                daily.fetch_daily("1.510300", "2026-11-06", "2026-11-06", value_index=2)

    def test_rollup_preserves_zero_and_missing_is_not_partial_sum(self):
        archive = self.archive()
        archive["etfs"][self.codes[0]]["amounts"]["2026-11-05"] = 0
        del archive["etfs"][self.codes[-1]]["amounts"]["2026-11-06"]
        first, last = daily.daily_rows(archive)
        self.assertEqual(first["amount"], 5400)
        self.assertTrue(first["is_complete"])
        self.assertIsNone(last["amount"])
        self.assertEqual(last["missing_codes"], [self.codes[-1]])
        self.assertEqual(last["index_close"], 3001)

    def test_not_yet_traded_requires_verified_origin(self):
        archive = self.archive()
        record = archive["etfs"][self.codes[0]]
        record["first_trade_date"] = "2026-11-06"
        record["amounts"].pop("2026-11-05")
        row = daily.daily_rows(archive)[0]
        self.assertEqual((row["amount"], row["expected_etf_count"]), (5400, 9))
        self.assertEqual(row["not_yet_traded_codes"], [self.codes[0]])
        record.pop("first_trade_date")
        self.assertIsNone(daily.daily_rows(archive)[0]["amount"])

    def test_preclose_sync_never_saves_intraday_daily_bars(self):
        days = ("2026-11-06", "2026-11-09")
        with patch.object(daily, "fetch_daily", side_effect=self.upstream(days)):
            self.assertTrue(daily.sync_daily_history(self.now(12)))
        archive = daily.read_history()
        self.assertEqual(archive["trading_dates"], ["2026-11-06"])
        self.assertEqual(archive["days"][0]["amount"], 1000)
        for record in archive["etfs"].values():
            self.assertNotIn("2026-11-09", record["amounts"])
        self.assertNotIn("2026-11-09", archive["index"]["closes"])

    def test_known_full_day_suspension_has_zero_and_provenance(self):
        days = ("2023-11-27", "2023-11-28", "2023-11-29")
        base = self.upstream(days)
        def fetch(secid, *args, **kwargs):
            values, origin = base(secid, *args, **kwargs)
            if secid == "0.159952":
                values.pop("2023-11-28")
            return values, origin
        now = datetime(2023, 11, 30, 16, tzinfo=daily.BEIJING_TZ)
        with patch.object(daily, "fetch_daily", side_effect=fetch):
            self.assertTrue(daily.sync_daily_history(now))
        row = daily.get_daily_history_payload("2023-11-28", "2023-11-28")["days"][0]
        self.assertEqual(row["amount"], 900)
        self.assertEqual(row["confirmed_suspended_codes"], ["159952"])
        evidence = daily.read_history()["etfs"]["159952"]["confirmed_zero_days"]["2023-11-28"]
        self.assertTrue(evidence["sources"])

    def test_unknown_missing_day_remains_incomplete(self):
        base = self.upstream()
        def fetch(secid, *args, **kwargs):
            values, origin = base(secid, *args, **kwargs)
            if secid == daily.A_SHARE_ETF_METAS[self.codes[0]]["secid"]:
                values.pop("2026-11-05")
            return values, origin
        with patch.object(daily, "fetch_daily", side_effect=fetch):
            self.assertFalse(daily.sync_daily_history(self.now()))
        row = daily.get_daily_history_payload()["days"][0]
        self.assertIsNone(row["amount"])
        self.assertEqual(row["confirmed_suspended_codes"], [])

    def test_etf_dates_keep_index_gaps_visible_on_first_backfill(self):
        base = self.upstream()
        def fetch(secid, *args, **kwargs):
            values, origin = base(secid, *args, **kwargs)
            if secid == "1.000001":
                values.pop("2026-11-06")
            return values, origin
        with patch.object(daily, "fetch_daily", side_effect=fetch):
            self.assertFalse(daily.sync_daily_history(self.now()))
        payload = daily.get_daily_history_payload()
        self.assertEqual(payload["day_count"], 2)
        self.assertEqual(payload["days"][-1]["amount"], 1000)
        self.assertIsNone(payload["days"][-1]["index_close"])
        self.assertEqual(payload["incomplete_index_day_count"], 1)

    def test_failed_refresh_preserves_amounts_closes_and_success_time(self):
        original = self.archive()
        daily.save_history(original)
        with patch.object(daily, "fetch_daily", side_effect=OSError("upstream unavailable")):
            self.assertFalse(daily.sync_daily_history(self.now()))
        result = daily.read_history()
        self.assertEqual(result["last_success_at"], "old-success")
        self.assertEqual(result["index"]["closes"], original["index"]["closes"])
        for code in self.codes:
            self.assertEqual(result["etfs"][code]["amounts"], original["etfs"][code]["amounts"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["errors"]), 11)

    def test_old_gap_is_repaired_without_moving_archive_start(self):
        archive = self.archive(("2024-02-01", "2026-11-06"))
        archive["etfs"][self.codes[0]]["amounts"].pop("2024-02-01")
        archive["index"]["closes"].pop("2024-02-01")
        daily.save_history(archive)
        with patch.object(daily, "fetch_daily", side_effect=self.upstream(("2024-02-01", "2026-11-06"))) as fetch:
            self.assertTrue(daily.sync_daily_history(self.now()))
        requests = {call.args[0]: call.args[1] for call in fetch.call_args_list}
        self.assertEqual(requests["1.000001"], "2024-02-01")
        self.assertEqual(requests[daily.A_SHARE_ETF_METAS[self.codes[0]]["secid"]], "2024-02-01")
        self.assertEqual(daily.read_history()["start_date"], "2023-11-07")

    def test_local_query_filters_dates_and_does_not_fetch(self):
        daily.save_history(self.archive())
        with patch.object(daily, "fetch_daily") as fetch:
            payload = daily.get_daily_history_payload("2026-11-06", "2026-11-06")
        fetch.assert_not_called()
        self.assertEqual(payload["day_count"], 1)
        self.assertEqual(payload["days"][0]["amount"], 5500)
        for start, end in (("2026-13-01", None), ("", None), ("20261106", None), ("2026-11-07", "2026-11-06")):
            self.assertEqual(daily.get_daily_history_payload(start, end)["code"], 400)

    def test_legacy_archive_keeps_amounts_and_reports_index_not_ready(self):
        archive = self.archive()
        archive.pop("index")
        daily.save_history(archive)
        payload = daily.get_daily_history_payload()
        self.assertEqual(payload["days"][0]["amount"], 5500)
        self.assertEqual(payload["incomplete_index_day_count"], 2)
        self.assertEqual(payload["status"], "partial")

    def test_corrupt_or_malformed_archive_returns_503(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("{broken", '{"version":2}', '{"version":1}'):
            with self.subTest(text=text):
                with open(self.path, "w") as handle:
                    handle.write(text)
                self.assertEqual(daily.get_daily_history_payload()["code"], 503)

    def test_atomic_replace_failure_keeps_previous_archive(self):
        archive = self.archive()
        daily.save_history(archive)
        changed = copy.deepcopy(archive)
        changed["etfs"][self.codes[0]]["amounts"]["2026-11-06"] = 12345
        with patch.object(daily.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                daily.save_history(changed)
        self.assertEqual(daily.read_history(), archive)


if __name__ == "__main__":
    unittest.main()
