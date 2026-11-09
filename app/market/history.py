#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨境ETF收盘档案的初始化、归档和只读查询。"""

import os
import sys
import time

from app.core.config import DATA_DIR
from app.core.storage import file_lock, read_json, read_bytes, write_json, initialize_json

HISTORY_FILE = os.path.join(DATA_DIR, "history_daily.json")

def ensure_data_storage():
    """确保持久化数据目录存在，且冷启动时由种子模板初始化历史数据"""
    os.makedirs(DATA_DIR, exist_ok=True)
    example_file = os.path.join(DATA_DIR, "history_daily.json.example")
    try:
        if initialize_json(HISTORY_FILE, example_file):
            print(f"[Init] 已从种子文件初始化收盘历史档案: {os.path.basename(HISTORY_FILE)}", flush=True)
    except Exception as e:
        print(f"[Warning] Failed to initialize history from example: {e}", file=sys.stderr)


def record_daily_history(payload):
    """
    静默沉淀每日收盘切片至 history_daily.json
    - 条件：当前已到交易日收盘时间（15:00之后），且今日尚未归档
    - 幂等性：同一交易日仅归档一次
    - 安全性：写入临时文件后原子替换，防止断电导致 JSON 损坏
    """
    if not payload:
        return

    # 1. 从标的行情中探测有效交易日和收盘状态
    all_etfs = (payload.get("nasdaq100_etfs") or []) + (payload.get("sp500_etfs") or []) + (payload.get("sector_etfs") or [])
    if not all_etfs:
        return

    trade_date = ""
    is_closed = False

    for item in all_etfs:
        tt = item.get("trade_time") or ""
        # 腾讯行情时间格式示例: "20261016161436" (14位)
        if len(tt) >= 8 and tt[:8].isdigit():
            trade_date = f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]}"
            if len(tt) >= 12 and tt[8:12].isdigit():
                hour_min = int(tt[8:12])
                if hour_min >= 1500:
                    is_closed = True
            break

    # 容错：若未提取到 trade_time，则基于系统时间判定（工作日15点之后）
    if not trade_date:
        now_lt = time.localtime()
        if now_lt.tm_wday < 5:
            trade_date = time.strftime("%Y-%m-%d", now_lt)
            if now_lt.tm_hour >= 15:
                is_closed = True

    # 仅在确认收盘且日期有效时归档
    if not trade_date or not is_closed:
        return

    with file_lock(HISTORY_FILE):
        _record_closed_day(payload, trade_date)


def _record_closed_day(payload, trade_date):
    # 2. 读取现有历史记录
    history_data = {}
    if os.path.exists(HISTORY_FILE):
        try:
            history_data = read_json(HISTORY_FILE)
        except Exception as e:
            print(f"[Warning] Failed to load history file: {e}", file=sys.stderr)
            history_data = {}

    # 3. 幂等检查：今日已归档则直接返回，不重复写
    if trade_date in history_data:
        return

    # 4. 组装归档条目
    macro = payload.get("macro", {})
    min_nas = macro.get("min_premium_etf")
    min_sp = macro.get("sp500_min_premium_etf")

    def build_etf_record(etf, category):
        return {
            "code": etf.get("code"),
            "name": etf.get("name"),
            "category": category,
            "close": etf.get("price"),
            "open": etf.get("open"),
            "high": etf.get("high"),
            "low": etf.get("low"),
            "iopv": etf.get("iopv"),
            "premium": etf.get("est_premium"),
            "change_pct": etf.get("change_pct"),
            "amount_wan": etf.get("amount_wan")
        }

    etfs_archive = []
    for x in (payload.get("nasdaq100_etfs") or []):
        etfs_archive.append(build_etf_record(x, "nasdaq100"))

    for x in (payload.get("sector_etfs") or []):
        etfs_archive.append(build_etf_record(x, "nasdaq_tech"))

    for x in (payload.get("sp500_etfs") or []):
        etfs_archive.append(build_etf_record(x, "sp500"))

    record = {
        "date": trade_date,
        "archive_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "macro": {
            "nasdaq_avg_premium": macro.get("avg_premium"),
            "sp500_avg_premium": macro.get("sp500_avg_premium"),
            "nasdaq_best": {
                "code": min_nas["code"],
                "name": min_nas["name"],
                "premium": min_nas.get("est_premium"),
                "close": min_nas.get("price"),
                "iopv": min_nas.get("iopv")
            } if min_nas else None,
            "sp500_best": {
                "code": min_sp["code"],
                "name": min_sp["name"],
                "premium": min_sp.get("est_premium"),
                "close": min_sp.get("price"),
                "iopv": min_sp.get("iopv")
            } if min_sp else None,
        },
        "etfs": etfs_archive
    }

    # 5. 原子安全写入
    history_data[trade_date] = record
    try:
        write_json(HISTORY_FILE, history_data, ensure_ascii=False, indent=2)
        print(f"[{time.strftime('%H:%M:%S')}] 💾 [收盘归档] 已成功沉淀 {trade_date} 收盘历史数据至 {os.path.basename(HISTORY_FILE)}", flush=True)
    except Exception as e:
        print(f"[Error] Failed to write history file: {e}", file=sys.stderr)


def read_history(days_limit=None):
    try:
        history = read_json(HISTORY_FILE, default={})
    except Exception as exc:
        print(f"[Error] Reading history file: {exc}", file=sys.stderr)
        history = {}
    dates = sorted(history, reverse=True)
    if days_limit and days_limit > 0:
        dates = dates[:days_limit]
    return {day: history[day] for day in dates}


def has_archived_date(day):
    try:
        return day in read_json(HISTORY_FILE, default={})
    except Exception:
        return False


def read_history_bytes():
    return read_bytes(HISTORY_FILE)
