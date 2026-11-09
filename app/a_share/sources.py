#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯分时／批量快照适配，保留原始金额单位，不跨来源做累计差分。"""

import json
import math
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.a_share.requests import read_upstream

BEIJING_TZ = timezone(timedelta(hours=8))
TENCENT_SOURCE = "tencent_minute"


def finite_number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("布尔值不是行情数值")
    number = float(value)
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        raise ValueError("行情数值无效")
    return number


def parse_tencent_series(payload, symbol, minutes, is_index=False, now=None):
    now = now or datetime.now(BEIJING_TZ)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("腾讯分时请求失败")
    record = payload.get("data", {}).get(symbol)
    if not isinstance(record, dict):
        raise ValueError("腾讯未返回对应标的")
    data = record.get("data")
    days = data if isinstance(data, list) else [data]
    result, previous_closes = {}, {}
    qt = record.get("qt", {}).get(symbol, [])
    for day in days:
        if not isinstance(day, dict):
            continue
        day_str = datetime.strptime(day["date"], "%Y%m%d").date().isoformat()
        if day_str > now.date().isoformat() or day_str < "2020-01-01":
            continue
        rows = day.get("data")
        if not isinstance(rows, list):
            raise ValueError("腾讯分时记录无效")
        cumulative, points = {}, {}
        for row in rows:
            fields = row.split() if isinstance(row, str) else []
            if len(fields) < 4 or not re.fullmatch(r"\d{4}", fields[0]):
                continue
            minute = fields[0][:2] + ":" + fields[0][2:]
            if minute not in minutes:
                continue
            if minute in points:
                raise ValueError("腾讯分时分钟重复")
            points[minute] = finite_number(fields[1], positive=True)
            cumulative[minute] = finite_number(fields[3])
        if is_index:
            values = points
        else:
            values, last_total = {}, None
            for pos, minute in enumerate(minutes):
                if minute not in cumulative:
                    continue
                amount = cumulative[minute]
                if last_total is not None and amount < last_total:
                    raise ValueError("腾讯累计成交额回退，拒绝本次快照")
                last_total = amount
                if pos == 0:
                    values[minute] = round(amount, 2)
                elif minutes[pos - 1] in cumulative:
                    values[minute] = round(amount - cumulative[minutes[pos - 1]], 2)
        if values:
            result[day_str] = values
        pre_close = day.get("prec")
        if pre_close is None and len(qt) > 30 and str(qt[30]).startswith(day["date"]):
            pre_close = qt[4]
        if pre_close is not None:
            previous_closes[day_str] = finite_number(pre_close, positive=True)
    if not result:
        raise ValueError("腾讯未返回有效交易分钟")
    return result, previous_closes


def fetch_tencent_series(symbol, minutes, ndays=1, is_index=False):
    endpoint = "day" if ndays > 1 else "minute"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/{endpoint}/query?" + urllib.parse.urlencode({"code": symbol})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    payload = json.loads(read_upstream(request).decode("utf-8"))
    return parse_tencent_series(payload, symbol, minutes, is_index)


def parse_tencent_snapshot(body, symbols, now=None):
    now = now or datetime.now(BEIJING_TZ)
    quotes = {}
    for line in body.split(";"):
        match = re.match(r'\s*v_((?:sh|sz)\d{6})="([^"\r\n]*)"\s*$', line)
        if not match or match[1] not in symbols:
            continue
        fields = match[2].split("~")
        try:
            if len(fields) <= 37 or fields[2] != match[1][2:]:
                continue
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=BEIJING_TZ)
            if stamp > now + timedelta(minutes=2):
                continue
            point = finite_number(fields[3], positive=True)
            pre_close = finite_number(fields[4], positive=True)
            change_pct = (point / pre_close - 1) * 100
            amount = finite_number(finite_number(fields[37]) * 10000)
            if not math.isfinite(change_pct):
                continue
            quotes[match[1]] = {
                "date": stamp.date().isoformat(), "time": stamp.isoformat(timespec="seconds"),
                "point": point, "pre_close": pre_close,
                "change_pct": change_pct,
                "cumulative_amount": round(amount, 2),
                "source": "tencent_quote",
            }
        except (ValueError, TypeError, OverflowError):
            continue
    if not quotes:
        raise ValueError("腾讯批量快照无有效行情")
    return quotes


def fetch_tencent_snapshot(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    return parse_tencent_snapshot(read_upstream(request).decode("gbk"), symbols)
