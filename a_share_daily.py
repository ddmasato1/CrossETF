#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固定核心ETF池的日成交额档案；标准库实现，导入时不联网。"""

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from a_share_volume import A_SHARE_ETF_METAS, BEIJING_TZ, GROUP_VERSION, format_amount_chinese

SOURCE = "eastmoney_daily_kline"
# 基金官方停牌公告及年报目录交叉核实：11月28日开市停牌，29日公布结果后复牌。
CONFIRMED_SUSPENSIONS = {
    "159952": {
        "2023-11-28": {
            "reason": "持有人大会计票日全天停牌",
            "source": "fund_manager_notice",
            "sources": [
                "https://www.gffunds.com.cn/jjgg/zdsj/202311/t20231128_388879.shtml",
                "https://www.gffunds.com.cn/jjgg/dqgg/202403/P020240329297868886543.pdf#page=74",
            ],
        },
    },
}
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = ""
file_lock = threading.Lock()
state_lock = threading.Lock()
runtime_state = {"is_syncing": False, "last_error": ""}


def configure(data_dir=None):
    global HISTORY_FILE
    root = data_dir or os.environ.get("DATA_DIR", os.path.join(CURRENT_DIR, "data"))
    HISTORY_FILE = os.path.join(os.path.abspath(root), "a_share_etf_daily", "history.json")


configure()


def three_years_before(day):
    return day.replace(year=day.year - 3, day=min(day.day, 28) if day.month == 2 else day.day)


def completed_cutoff(now):
    # 留出收盘结算时间；盘中日K不得写成完整交易日。
    cutoff = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    return cutoff


def read_history():
    with file_lock:
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as handle:
                archive = json.load(handle)
        except FileNotFoundError:
            return None
    if not isinstance(archive, dict) or archive.get("version") != 1:
        raise ValueError("日成交额档案格式不受支持")
    try:
        if date.fromisoformat(archive["start_date"]).isoformat() != archive["start_date"]:
            raise ValueError()
        days = archive["trading_dates"]
        if not isinstance(days, list) or any(date.fromisoformat(day).isoformat() != day for day in days) or days != sorted(set(days)):
            raise ValueError()
        if not isinstance(archive["etfs"], dict) or any(not isinstance(record, dict) or not isinstance(record.get("amounts"), dict) for record in archive["etfs"].values()):
            raise ValueError()
        index = archive.get("index", {})
        if not isinstance(index, dict) or not isinstance(index.get("closes", {}), dict):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("日成交额档案结构不完整或日期无效") from exc
    return archive


def save_history(archive):
    with file_lock:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        temporary = HISTORY_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(archive, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(temporary, HISTORY_FILE)


def fetch_daily(secid, start, end, value_index=6):
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101", "fqt": "0", "beg": start.replace("-", ""),
        "end": end.replace("-", ""), "lmt": "10000",
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/",
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or str(data.get("code")) != secid.split(".", 1)[1]:
        raise ValueError("日线接口未返回对应标的")
    rows = data.get("klines")
    if not isinstance(rows, list) or not rows:
        raise ValueError("日线接口返回空数据")
    values = {}
    for row in rows:
        parts = row.split(",")
        day = date.fromisoformat(parts[0]).isoformat()
        value = float(parts[value_index])
        if day in values or not math.isfinite(value) or value < 0 or (value_index == 2 and value == 0):
            raise ValueError("日线日期或数值异常")
        values[day] = round(value, 2)
    # 只有完整返回该证券全部日K时，才把第一条记录当作首个交易日。
    full_origin = min(values) if len(values) == data.get("dktotal") else None
    return values, full_origin


def daily_rows(archive):
    rows = []
    for day in archive.get("trading_dates", []):
        missing, not_yet_traded, suspended, values = [], [], [], []
        for code in A_SHARE_ETF_METAS:
            record = archive.get("etfs", {}).get(code, {})
            first = record.get("first_trade_date")
            if first and day < first:
                not_yet_traded.append(code)
                continue
            value = record.get("amounts", {}).get(day)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                values.append(value)
                if value == 0 and day in record.get("confirmed_zero_days", {}):
                    suspended.append(code)
            else:
                missing.append(code)
        expected = len(A_SHARE_ETF_METAS) - len(not_yet_traded)
        complete = not missing and expected > 0
        amount = round(sum(values), 2) if complete else None
        close = archive.get("index", {}).get("closes", {}).get(day)
        if not isinstance(close, (int, float)) or isinstance(close, bool) or not math.isfinite(close) or close <= 0:
            close = None
        rows.append({
            "date": day, "amount": amount, "amount_str": format_amount_chinese(amount),
            "index_close": close,
            "is_complete": complete, "expected_etf_count": expected,
            "available_etf_count": len(values), "missing_codes": missing,
            "not_yet_traded_codes": not_yet_traded, "confirmed_suspended_codes": suspended,
        })
    return rows


def sync_daily_history(now=None):
    now = now or datetime.now(BEIJING_TZ)
    cutoff = completed_cutoff(now).isoformat()
    stamp = now.isoformat(timespec="seconds")
    archive = read_history() or {
        "version": 1, "source": SOURCE, "start_date": three_years_before(now.date()).isoformat(),
        "trading_dates": [], "etfs": {},
    }
    archive["group_version"] = GROUP_VERSION
    archive["codes"] = list(A_SHARE_ETF_METAS)
    archive["last_attempt_at"] = stamp
    archive["requested_through"] = cutoff
    errors = {}
    start = archive["start_date"]
    calendar = archive.get("trading_dates", [])
    index = archive.setdefault("index", {"code": "000001", "name": "上证指数", "secid": "1.000001", "closes": {}})
    closes = index["closes"]
    index_gaps = [day for day in calendar if day not in closes]
    overlap = (date.fromisoformat(calendar[-1]) - timedelta(days=14)).isoformat() if calendar else start
    calendar_start = max(start, min([overlap] + index_gaps)) if closes else start
    index["last_attempt_at"] = stamp
    try:
        index_days, _ = fetch_daily("1.000001", calendar_start, cutoff, value_index=2)
        received = {day: value for day, value in index_days.items() if start <= day <= cutoff}
        closes.update(received)
        archive["trading_dates"] = sorted(set(calendar) | set(received))
        archive["calendar_last_success_at"] = stamp
        index["last_success_at"] = stamp
        index["last_error"] = ""
    except Exception as exc:
        index["last_error"] = str(exc)
        errors["calendar"] = str(exc)
    calendar = archive["trading_dates"]
    for code, meta in A_SHARE_ETF_METAS.items():
        record = archive["etfs"].setdefault(code, {"amounts": {}})
        record["name"] = meta["name"]
        record["last_attempt_at"] = stamp
        first = record.get("first_trade_date")
        stored = record["amounts"]
        if first:
            gaps = [day for day in calendar if day >= first and day not in stored]
            overlap = (date.fromisoformat(max(stored)) - timedelta(days=14)).isoformat() if stored else start
            request_start = max(start, min([overlap] + gaps))
            request_end = cutoff
        else:
            # 初次读取全历史以核实首个交易日，只保存需求起点之后的日线。
            request_start, request_end = "1990-01-01", "2050-01-01"
        try:
            amounts, origin = fetch_daily(meta["secid"], request_start, request_end)
            received = {day: value for day, value in amounts.items() if start <= day <= cutoff}
            stored.update(received)
            # ETF实际成交日期也证明开市，避免指数源缺日时整天被合计静默漏掉。
            calendar = sorted(set(calendar) | set(received))
            archive["trading_dates"] = calendar
            for day in received:
                record.get("confirmed_zero_days", {}).pop(day, None)
            if origin and request_start == "1990-01-01":
                record["first_trade_date"] = origin
            record["last_success_at"] = stamp
            record["last_error"] = ""
        except Exception as exc:
            record["last_error"] = str(exc)
            errors[code] = str(exc)
        for day, evidence in CONFIRMED_SUSPENSIONS.get(code, {}).items():
            if day in calendar and day not in stored:
                stored[day] = 0.0
                record.setdefault("confirmed_zero_days", {})[day] = evidence
        # 每只独立落盘，补采中断也保留已取得的数据及同口径合计。
        archive["days"] = daily_rows(archive)
        archive["status"] = "partial"
        archive["errors"] = dict(errors)
        archive["updated_at"] = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
        save_history(archive)
        time.sleep(.25)
    rows = daily_rows(archive)
    complete = bool(rows) and all(row["is_complete"] and row["index_close"] is not None for row in rows) and not errors
    archive["days"] = rows
    archive["status"] = "ready" if complete else "partial"
    archive["errors"] = errors
    if complete:
        archive["last_success_at"] = stamp
    archive["updated_at"] = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    save_history(archive)
    print(f"[AShareDaily] {archive['status']}: {len(rows)}日，完整{sum(row['is_complete'] for row in rows)}日，截止{calendar[-1] if calendar else '--'}", flush=True)
    return complete


def get_daily_history_payload(start=None, end=None):
    for value in (start, end):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                return {"code": 400, "message": "日期须为YYYY-MM-DD"}
    if start and end and start > end:
        return {"code": 400, "message": "起始日期不能晚于结束日期"}
    with state_lock:
        state = dict(runtime_state)
    try:
        archive = read_history()
    except (OSError, ValueError) as exc:
        return {"code": 503, "status": "error", "message": f"读取日成交额档案失败：{exc}"}
    if not archive:
        return {"code": 200, "status": "syncing" if state["is_syncing"] else "uninitialized", "days": [], "runtime": state}
    rows = daily_rows(archive)
    selected = [row for row in rows if (not start or row["date"] >= start) and (not end or row["date"] <= end)]
    incomplete = sum(not row["is_complete"] for row in selected)
    index = archive.get("index", {})
    closes = index.get("closes", {})
    index_info = {
        "code": "000001", "name": "上证指数", "source": SOURCE,
        "data_through": max(closes) if closes else None,
        "last_success_at": index.get("last_success_at"), "last_error": index.get("last_error", ""),
    }
    etfs = []
    for code, meta in A_SHARE_ETF_METAS.items():
        record = archive.get("etfs", {}).get(code, {})
        amounts = record.get("amounts", {})
        etfs.append({
            "code": code, "name": meta["name"], "first_trade_date": record.get("first_trade_date"),
            "data_through": max(amounts) if amounts else None,
            "last_success_at": record.get("last_success_at"), "last_error": record.get("last_error", ""),
            "confirmed_zero_days": record.get("confirmed_zero_days", {}),
        })
    status = archive.get("status", "partial")
    if status == "ready" and (not rows or any(not row["is_complete"] or row["index_close"] is None for row in rows)):
        status = "partial"
    return {
        "code": 200, "status": status, "source": SOURCE,
        "group_version": GROUP_VERSION, "unit": "CNY", "etf_count": len(A_SHARE_ETF_METAS),
        "codes": list(A_SHARE_ETF_METAS), "start_date": archive["start_date"],
        "data_through": rows[-1]["date"] if rows else None,
        "latest_complete_date": next((row["date"] for row in reversed(rows) if row["is_complete"]), None),
        "requested_through": archive.get("requested_through"),
        "updated_at": archive.get("updated_at"), "last_attempt_at": archive.get("last_attempt_at"),
        "last_success_at": archive.get("last_success_at"),
        "calendar_last_success_at": archive.get("calendar_last_success_at"),
        "day_count": len(selected), "incomplete_day_count": incomplete,
        "index_info": index_info,
        "incomplete_index_day_count": sum(row["index_close"] is None for row in selected),
        "errors": archive.get("errors", {}), "runtime": state, "etfs": etfs, "days": selected,
    }


def daily_collector_daemon():
    last_cutoff, last_attempt, successful = None, 0, False
    while True:
        now = datetime.now(BEIJING_TZ)
        cutoff = completed_cutoff(now).isoformat()
        if cutoff != last_cutoff or (not successful and time.monotonic() - last_attempt >= 300):
            with state_lock:
                runtime_state["is_syncing"] = True
            try:
                successful = sync_daily_history(now)
                with state_lock:
                    runtime_state["last_error"] = "" if successful else "部分日线待补采，保留已有档案"
            except Exception as exc:
                successful = False
                with state_lock:
                    runtime_state["last_error"] = str(exc)
                print(f"[AShareDaily] 同步失败：{exc}", flush=True)
            finally:
                last_cutoff, last_attempt = cutoff, time.monotonic()
                with state_lock:
                    runtime_state["is_syncing"] = False
        time.sleep(60)


def start_a_share_daily_daemon():
    threading.Thread(target=daily_collector_daemon, daemon=True, name="AShareDailyDaemon").start()
