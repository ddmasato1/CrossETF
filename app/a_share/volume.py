#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股重点宽基ETF一分钟成交额观察引擎
核心定位：
1. 监控 10 只重点宽基 ETF，归入 4 个指数组：
   - 上证综指: 510210, 510760
   - 沪深300: 510300, 510310, 159919, 510330
   - 创业板指: 159915, 159952
   - 科创50: 588000, 588080
2. 腾讯累计分时按同日相邻分钟差分为成交额（元），东方财富 trends2 按需备用。
3. 纯 Python 3.11 标准库实现，零第三方依赖。
4. 按交易日持久化独立存储在 data/a_share_etf_volume/{YYYY-MM-DD}.json，原子安全写入（.tmp + os.replace），写锁串行化。
5. 保存每 ETF 完整分钟标记及 complete_through 可核实边界：
   - 盘中未完成分钟或中途停机数据不会被历史回放误作“完整样本”污染基准。
   - 仅当后续完整快照覆盖或已收盘确认，才转为完整分钟。
6. 保留最近 60 个交易日，严格按文件名提取的交易日期倒序排序清理。
7. 历史同分钟基准：
   - 取当前交易日之前的最近 20 个交易日（不足20日如实显示实际N，绝不伪造）。
   - 组内成员齐备：若历史日某 ETF 缺失或在该分钟未完整，该日不计入该分钟有效样本（严禁填0）。
   - 基准取同分钟有效日组内合计金额的中位数。
   - 放大倍数 = 当前分钟组内合计金额 / 历史基准中位数。
8. 守护线程自主运行，启动补采最近 5 日，盘中自动轮询，带重试与退避。
9. 时间戳一律使用带 +08:00 的北京时间 ISO 字符串。
10. 导入本模块绝不发起任何网络请求。
"""

import os
import sys
import json
import time
import math
import re
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from app.core.config import CURRENT_DIR
from app.a_share.requests import read_upstream, request_budget
from app.core.storage import read_json, write_json, remove_json, file_lock
from app.a_share.sources import fetch_tencent_series, fetch_tencent_snapshot, TENCENT_SOURCE

# 基础配置与时区
BEIJING_TZ = timezone(timedelta(hours=8))
MAX_BASELINE_DAYS = 20
MAX_RETENTION_DAYS = 60
GROUP_VERSION = "v2.0"
SOURCE_NAME = "eastmoney_trends2"

# 目录配置
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(CURRENT_DIR, "data")))
VOLUME_DIR = os.path.join(DATA_DIR, "a_share_etf_volume")

# 线程锁
volume_lock = threading.Lock()


def get_beijing_now_iso():
    """获取带 +08:00 的北京时间 ISO 格式字符串"""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def configure(data_dir=None):
    """显式配置持久化目录，供外部调用或随 server.py 的 DATA_DIR 动态同步"""
    global DATA_DIR, VOLUME_DIR
    if data_dir:
        DATA_DIR = os.path.abspath(data_dir)
    else:
        DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(CURRENT_DIR, "data")))
    VOLUME_DIR = os.path.join(DATA_DIR, "a_share_etf_volume")
    ensure_volume_storage(VOLUME_DIR)


# 监控标的与分组定义（当前监控 10 只 4 组）
A_SHARE_ETF_GROUPS = [
    {
        "id": "sse",
        "name": "上证综指",
        "codes": ["510210", "510760"],
        "description": "所选2只ETF合计"
    },
    {
        "id": "hs300",
        "name": "沪深300",
        "codes": ["510300", "510310", "159919", "510330"],
        "description": "所选4只ETF合计"
    },
    {
        "id": "cyb",
        "name": "创业板指",
        "codes": ["159915", "159952"],
        "description": "所选2只ETF合计"
    },
    {
        "id": "kc50",
        "name": "科创50",
        "codes": ["588000", "588080"],
        "description": "所选2只ETF合计"
    }
]

A_SHARE_ETF_METAS = {
    "510210": {"name": "上证综指ETF富国", "market": "sh", "secid": "1.510210"},
    "510760": {"name": "上证综指ETF国泰", "market": "sh", "secid": "1.510760"},
    "510300": {"name": "沪深300ETF华泰柏瑞", "market": "sh", "secid": "1.510300"},
    "510310": {"name": "沪深300ETF易方达", "market": "sh", "secid": "1.510310"},
    "159919": {"name": "沪深300ETF嘉实", "market": "sz", "secid": "0.159919"},
    "510330": {"name": "沪深300ETF华夏", "market": "sh", "secid": "1.510330"},
    "159915": {"name": "创业板ETF易方达", "market": "sz", "secid": "0.159915"},
    "159952": {"name": "创业板ETF广发", "market": "sz", "secid": "0.159952"},
    "588000": {"name": "科创50ETF华夏", "market": "sh", "secid": "1.588000"},
    "588080": {"name": "科创50ETF易方达", "market": "sh", "secid": "1.588080"}
}

# 历史归档保留元数据映射（移出当前监控但保留在旧日档中不删除）
LEGACY_ETF_METAS = {
    "159925": {"name": "沪深300ETF南方", "market": "sz", "secid": "0.159925"},
    "510050": {"name": "上证50ETF华夏", "market": "sh", "secid": "1.510050"},
    "510500": {"name": "中证500ETF南方", "market": "sh", "secid": "1.510500"},
    "512100": {"name": "中证1000ETF南方", "market": "sh", "secid": "1.512100"},
    "159845": {"name": "中证1000ETF华夏", "market": "sz", "secid": "0.159845"}
}

A_SHARE_INDEX_METAS = {
    "sse": {"code": "000001", "name": "上证综指", "secid": "1.000001"},
    "hs300": {"code": "000300", "name": "沪深300", "secid": "1.000300"},
    "cyb": {"code": "399006", "name": "创业板指", "secid": "0.399006"},
    "kc50": {"code": "000688", "name": "科创50", "secid": "1.000688"}
}

LEGACY_INDEX_METAS = {
    "sz50": {"code": "000016", "name": "上证50", "secid": "1.000016"},
    "zz500": {"code": "000905", "name": "中证500", "secid": "1.000905"},
    "zz1000": {"code": "000852", "name": "中证1000", "secid": "1.000852"}
}


# 全天标准交易分钟清单 (09:30-11:30 共121分，13:01-15:00 共120分，合计241分)
def get_standard_trading_minutes():
    minutes = []
    for h in [9, 10, 11]:
        start_m = 30 if h == 9 else 0
        end_m = 30 if h == 11 else 59
        for m in range(start_m, end_m + 1):
            minutes.append(f"{h:02d}:{m:02d}")
    for h in [13, 14, 15]:
        start_m = 1 if h == 13 else 0
        end_m = 0 if h == 15 else 59
        for m in range(start_m, end_m + 1):
            minutes.append(f"{h:02d}:{m:02d}")
    return minutes


STANDARD_TRADING_MINUTES = get_standard_trading_minutes()
STANDARD_TRADING_MINUTES_SET = set(STANDARD_TRADING_MINUTES)

# 运行时全局状态
volume_runtime_state = {
    "status": "uninitialized",
    "status_desc": "尚未初始化",
    "last_success_at": "",
    "last_attempt_at": "",
    "last_error": "",
    "etf_statuses": {},
    "index_statuses": {},
    "snapshots": {},
    "snapshot_error": "",
    "is_syncing": False
}

payload_cache = {
    "date": None,
    "timestamp": 0.0,
    "payload": None
}
PAYLOAD_CACHE_TTL = 3.0


def ensure_volume_storage(volume_dir=None):
    """确保 A 股成交数据存储目录存在"""
    vdir = volume_dir or VOLUME_DIR
    os.makedirs(vdir, exist_ok=True)


def format_amount_chinese(amount_yuan):
    """格式化金额为中文习惯：亿元或万元"""
    if amount_yuan is None or not math.isfinite(amount_yuan):
        return "--"
    abs_amt = abs(amount_yuan)
    if abs_amt >= 1e8:
        return f"{amount_yuan / 1e8:.2f} 亿"
    elif abs_amt >= 1e4:
        return f"{amount_yuan / 1e4:.0f} 万"
    else:
        return f"{amount_yuan:.0f} 元"


def load_day_file(volume_dir, date_str):
    """
    读取指定交易日的 JSON 文件，带严格结构与数值类型校验。
    包含 complete_through 完整性边界检测与老版本迁移口径。
    """
    vdir = volume_dir or VOLUME_DIR
    fpath = os.path.join(vdir, f"{date_str}.json")
    if not os.path.exists(fpath):
        return None
    try:
        data = read_json(fpath)
        if not isinstance(data, dict):
            return None
        raw_etfs = data.get("etfs")
        if not isinstance(raw_etfs, dict):
            return None

        cleaned_etfs = {}
        for code, etf_obj in raw_etfs.items():
            if not isinstance(etf_obj, dict):
                continue
            raw_min_data = etf_obj.get("data")
            if not isinstance(raw_min_data, dict):
                continue
            cleaned_data = {}
            for m_k, amt_v in raw_min_data.items():
                if m_k in STANDARD_TRADING_MINUTES_SET:
                    try:
                        fval = float(amt_v)
                        if math.isfinite(fval) and fval >= 0:
                            cleaned_data[m_k] = fval
                    except (ValueError, TypeError):
                        continue

            # 提取或推导每只 ETF 的 complete_through
            etf_cp = etf_obj.get("complete_through")
            if not isinstance(etf_cp, str) or etf_cp not in STANDARD_TRADING_MINUTES_SET:
                # 缺少完整性证据时等待上游补采，不因文件包含15:00就推定已完成。
                etf_cp = None

            meta = A_SHARE_ETF_METAS.get(code) or LEGACY_ETF_METAS.get(code, {})
            sources = etf_obj.get("minute_sources")
            sources = sources if isinstance(sources, dict) else {}
            cleaned_etfs[code] = {
                "name": str(etf_obj.get("name", meta.get("name", code))),
                "market": str(etf_obj.get("market", meta.get("market", ""))),
                "source": str(etf_obj.get("source", SOURCE_NAME)),
                "last_success_at": str(etf_obj.get("last_success_at", "")),
                "complete_through": etf_cp,
                "data": cleaned_data,
                "minute_sources": {m: str(sources.get(m, etf_obj.get("source", SOURCE_NAME))) for m in cleaned_data}
            }

        # 完整性边界按当前监控的 10 只代码集合核验，缺少新增 ETF 的历史日不伪造为齐备
        complete_boundaries = [cleaned_etfs.get(code, {}).get("complete_through") for code in A_SHARE_ETF_METAS]
        data["complete_through"] = min(complete_boundaries) if all(complete_boundaries) else None
        data["etfs"] = cleaned_etfs
        cleaned_indices = {}
        raw_indices = data.get("indices", {})
        if isinstance(raw_indices, dict):
            for group_id, obj in raw_indices.items():
                if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict):
                    continue
                meta = A_SHARE_INDEX_METAS.get(group_id) or LEGACY_INDEX_METAS.get(group_id, {
                    "code": str(obj.get("code", "")),
                    "name": str(obj.get("name", group_id))
                })
                points = {}
                for minute, value in obj["data"].items():
                    point = positive_finite(value)
                    if minute in STANDARD_TRADING_MINUTES_SET and point is not None:
                        points[minute] = point
                boundary = obj.get("complete_through")
                sources = obj.get("minute_sources")
                sources = sources if isinstance(sources, dict) else {}
                cleaned_indices[group_id] = {
                    "code": meta["code"],
                    "name": meta["name"],
                    "source": str(obj.get("source", SOURCE_NAME)),
                    "minute_sources": {m: str(sources.get(m, obj.get("source", SOURCE_NAME))) for m in points},
                    "data": points,
                    "pre_close": positive_finite(obj.get("pre_close")),
                    "last_success_at": str(obj.get("last_success_at", "")),
                    "complete_through": boundary if isinstance(boundary, str) and boundary in STANDARD_TRADING_MINUTES_SET else None
                }
        data["indices"] = cleaned_indices
        return data
    except Exception as e:
        print(f"[Warning] Failed to read {fpath}: {e}", file=sys.stderr)
        return None


def save_day_file(volume_dir, date_str, day_record):
    """原子安全写入指定交易日 JSON 文件，串行化写锁保障"""
    vdir = volume_dir or VOLUME_DIR
    target_path = os.path.join(vdir, f"{date_str}.json")
    write_json(target_path, day_record, ensure_ascii=False, indent=2)


def cleanup_old_trading_days(volume_dir=None, keep_days=MAX_RETENTION_DAYS):
    """
    清理历史交易日文件，保留最近 keep_days 个交易日。
    严格按文件名提取的交易日期倒序排序清理，超出部分删除。
    """
    vdir = volume_dir or VOLUME_DIR
    if not os.path.exists(vdir):
        return
    date_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
    valid_files = []
    for fname in os.listdir(vdir):
        m = date_pattern.match(fname)
        if m:
            valid_files.append((m.group(1), fname))

    valid_files.sort(key=lambda x: x[0], reverse=True)
    if len(valid_files) > keep_days:
        for trade_date, fname in valid_files[keep_days:]:
            fpath = os.path.join(vdir, fname)
            try:
                remove_json(fpath)
            except OSError as e:
                print(f"[Warning] Failed to remove expired volume file {fpath}: {e}", file=sys.stderr)


def get_available_dates(volume_dir=None):
    """获取本地所有已保存的有效交易日期列表（最新在前）"""
    vdir = volume_dir or VOLUME_DIR
    if not os.path.exists(vdir):
        return []
    date_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
    dates = []
    for fname in os.listdir(vdir):
        m = date_pattern.match(fname)
        if m:
            dates.append(m.group(1))
    dates.sort(reverse=True)
    return dates


def get_prior_trading_dates(volume_dir=None, target_date="", max_days=MAX_BASELINE_DAYS):
    """
    获取严格位于 target_date 之前的最近 max_days 个交易日列表（倒序排列）。
    不足 max_days 时如实返回已有数量，绝不伪造。
    """
    vdir = volume_dir or VOLUME_DIR
    dates = [d for d in get_available_dates(vdir) if d < target_date]
    return dates[:max_days]


def fetch_trends_raw(secid, ndays=1, timeout=5):
    """
    从东方财富 trends2 接口拉取一分钟原始分时数据（纯 Python 标准库）。
    安全处理异常和非字典响应。
    """
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        f"secid={secid}&ndays={ndays}&iscr=0&"
        f"fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11&"
        f"fields2=f51,f52,f53,f54,f55,f56,f57,f58"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        raw = read_upstream(req, timeout=timeout)
        parsed_json = None
        for enc in ["utf-8", "gbk"]:
            try:
                parsed_json = json.loads(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        if parsed_json is None:
            parsed_json = json.loads(raw.decode("utf-8", errors="ignore"))
        return parsed_json if isinstance(parsed_json, dict) else None
    except Exception as e:
        print(f"[Warning] Failed to fetch trends for secid {secid}: {e}", file=sys.stderr)
        return None


def positive_finite(value):
    """指数点位与前收仅接受有限正数。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_trends(raw_data, value_index=6, strict_positive=False):
    """
    解析 trends2 响应，按日期归集每分钟成交额：
    严格校验：
    - 日期必须为合法的 YYYY-MM-DD，且不得大于北京时间今日（拒绝未来交易日）
    - 时间必须在 STANDARD_TRADING_MINUTES
    - 金额必须为有限正浮点数
    默认解析 ETF 成交额；指数以 value_index=2、strict_positive=True 复用相同日期/分钟校验。
    返回: { "YYYY-MM-DD": { "HH:MM": value, ... }, ... }
    """
    if not isinstance(raw_data, dict) or raw_data.get("rc") != 0:
        return {}
    data = raw_data.get("data")
    if not isinstance(data, dict):
        return {}
    trends = data.get("trends", [])
    if not isinstance(trends, list):
        return {}

    today_bj_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    result_by_date = {}
    for item in trends:
        if not isinstance(item, str):
            continue
        parts = item.split(",")
        if len(parts) < 7:
            continue
        dt_parts = parts[0].strip().split(" ")
        if len(dt_parts) != 2:
            continue
        d_str, t_str = dt_parts[0], dt_parts[1]

        # 日期合法性校验与未来日期过滤
        if not date_regex.match(d_str):
            continue
        try:
            datetime.strptime(d_str, "%Y-%m-%d")
        except ValueError:
            continue
        if d_str > today_bj_str or d_str < "2020-01-01":
            continue

        # 交易时间窗口校验
        if t_str not in STANDARD_TRADING_MINUTES_SET:
            continue

        try:
            amt = float(parts[value_index])
            if math.isfinite(amt) and (amt > 0 if strict_positive else amt >= 0):
                if d_str not in result_by_date:
                    result_by_date[d_str] = {}
                result_by_date[d_str][t_str] = amt
        except (ValueError, TypeError):
            continue
    return result_by_date


def determine_complete_through_for_date(d_str, minute_dict):
    """
    根据抓取到的数据分布及当前北京时间，精确判断该日该标的已完整收盘/走完的截止分钟 complete_through。
    - 历史日（d_str < today）：若达到 15:00，则完整至 15:00；若提前中断，则完整至倒数第二分钟或未完成。
    - 今日（d_str == today）：
      若当前时刻 > 15:00 且包含 15:00，完整至 15:00；
      若处于盘中交易时刻，当前最新分钟正在进行，其上一分钟为已走完的完整分钟。
    """
    if not minute_dict:
        return None
    sorted_mins = sorted(minute_dict.keys())
    now_bj = datetime.now(BEIJING_TZ)
    today_bj = now_bj.strftime("%Y-%m-%d")
    current_hhmm = now_bj.strftime("%H:%M")

    if d_str < today_bj:
        if "15:00" in minute_dict:
            return "15:00"
        # 历史日如果未到15:00，属于中途停机或早停，仅倒数第二分钟前确认为走完
        return sorted_mins[-2] if len(sorted_mins) >= 2 else None

    # 今日处理
    if current_hhmm > "15:00" and "15:00" in minute_dict:
        return "15:00"

    last_min = sorted_mins[-1]
    if current_hhmm > last_min:
        return last_min
    else:
        # 当前分钟正在进行中
        return sorted_mins[-2] if len(sorted_mins) >= 2 else None


def merge_and_save_etf_data(volume_dir, etf_code, trends_by_date, source=SOURCE_NAME):
    """
    将某 ETF 抓取到的多日分钟数据合并落盘至各交易日 JSON。
    - 沿用保留先前已采集好的完整分钟，不被旧上游覆盖冲掉。
    - 持久化保留 source、group_version、complete_through、更新时间及单标的完整元信息。
    """
    vdir = volume_dir or VOLUME_DIR
    meta = A_SHARE_ETF_METAS.get(etf_code) or LEGACY_ETF_METAS.get(etf_code, {"name": etf_code, "market": ""})
    now_iso = get_beijing_now_iso()

    for d_str, minute_dict in trends_by_date.items():
        if not minute_dict:
            continue
        with file_lock(os.path.join(vdir, f"{d_str}.json")):
            day_rec = load_day_file(vdir, d_str)
            if day_rec is None:
                day_rec = {
                    "version": 1,
                    "group_version": GROUP_VERSION,
                    "source": SOURCE_NAME,
                    "date": d_str,
                    "updated_at": now_iso,
                    "data_time": max(minute_dict.keys()),
                    "complete_through": None,
                    "etfs": {}
                }
            else:
                day_rec["group_version"] = GROUP_VERSION
                day_rec["updated_at"] = now_iso
                curr_max = max(minute_dict.keys())
                if "data_time" not in day_rec or curr_max > day_rec["data_time"]:
                    day_rec["data_time"] = curr_max

            # 测算该 ETF 在该日的新 complete_through
            new_cp = determine_complete_through_for_date(d_str, minute_dict)

            if etf_code not in day_rec["etfs"]:
                day_rec["etfs"][etf_code] = {
                    "name": meta["name"],
                    "market": meta.get("market", ""),
                    "source": SOURCE_NAME,
                    "last_success_at": now_iso,
                    "complete_through": new_cp,
                    "data": {}
                }
            else:
                day_rec["etfs"][etf_code]["name"] = meta["name"]
                day_rec["etfs"][etf_code]["market"] = meta.get("market", "")
                day_rec["etfs"][etf_code]["last_success_at"] = now_iso
                # complete_through 保持单调推进，不倒退
                old_cp = day_rec["etfs"][etf_code].get("complete_through")
                if new_cp:
                    if old_cp is None or new_cp > old_cp:
                        day_rec["etfs"][etf_code]["complete_through"] = new_cp

            # 未结束的上游快照不能覆盖已经确认完整的同一分钟。
            day_rec["etfs"][etf_code]["source"] = source
            minute_sources = day_rec["etfs"][etf_code].setdefault("minute_sources", {})
            confirmed_through = day_rec["etfs"][etf_code].get("complete_through")
            for m_str, amt in minute_dict.items():
                if confirmed_through and m_str <= confirmed_through and (not new_cp or m_str > new_cp):
                    continue
                day_rec["etfs"][etf_code]["data"][m_str] = amt
                minute_sources[m_str] = source

            # 更新该日总体的 complete_through（全组成员的最小完整边界）
            etfs_cps = [day_rec["etfs"].get(code, {}).get("complete_through") for code in A_SHARE_ETF_METAS]
            day_rec["complete_through"] = min(etfs_cps) if all(etfs_cps) else None

            save_day_file(vdir, d_str, day_rec)


def merge_and_save_index_data(volume_dir, group_id, trends_by_date, latest_pre_close=None, source=SOURCE_NAME, previous_closes=None):
    """独立保存真实指数点位；前收不跨日期套用，完整边界只按新快照推进。"""
    vdir = volume_dir or VOLUME_DIR
    meta = A_SHARE_INDEX_METAS.get(group_id) or LEGACY_INDEX_METAS.get(group_id, {"code": group_id, "name": group_id})
    dates = sorted(trends_by_date)
    now_iso = get_beijing_now_iso()
    for pos, date_str in enumerate(dates):
        points = trends_by_date[date_str]
        if not points:
            continue
        with file_lock(os.path.join(vdir, f"{date_str}.json")):
            day_rec = load_day_file(vdir, date_str)
            if day_rec is None:
                day_rec = {
                    "version": 1, "group_version": GROUP_VERSION, "source": SOURCE_NAME,
                    "date": date_str, "updated_at": "", "data_time": "",
                    "complete_through": None, "etfs": {}, "indices": {}
                }
            else:
                day_rec["group_version"] = GROUP_VERSION
                day_rec["updated_at"] = now_iso
            indices = day_rec.setdefault("indices", {})
            obj = indices.setdefault(group_id, {
                "code": meta["code"], "name": meta["name"], "source": SOURCE_NAME,
                "data": {}, "pre_close": None, "last_success_at": "", "complete_through": None
            })
            # 历史日仅取前一返回交易日的真实收盘；最早日无依据时不覆盖已有可靠前收。
            pre_close = trends_by_date[dates[pos - 1]].get("15:00") if pos else None
            if pos == len(dates) - 1:
                pre_close = positive_finite(latest_pre_close) or pre_close
            if previous_closes and date_str in previous_closes:
                pre_close = positive_finite(previous_closes[date_str])
            if pre_close is not None:
                obj["pre_close"] = pre_close
            obj["source"] = source
            obj.setdefault("minute_sources", {})
            new_cp = determine_complete_through_for_date(date_str, points)
            old_cp = obj.get("complete_through")
            if new_cp and (old_cp is None or new_cp > old_cp):
                obj["complete_through"] = new_cp
            confirmed_through = obj.get("complete_through")
            for minute, point in points.items():
                if confirmed_through and minute <= confirmed_through and (not new_cp or minute > new_cp):
                    continue
                obj["data"][minute] = point
                obj["minute_sources"][minute] = source
            obj["last_success_at"] = now_iso
            save_day_file(vdir, date_str, day_rec)


def collector_instruments():
    return [("etf", code, meta) for code, meta in A_SHARE_ETF_METAS.items()] + [
        ("index", group_id, meta) for group_id, meta in A_SHARE_INDEX_METAS.items()
    ]


def instrument_symbol(meta):
    market, code = meta["secid"].split(".", 1)
    return ("sh" if market == "1" else "sz") + code


def sync_instrument(volume_dir, kind, key, meta, ndays=1):
    """每次只校准一个标的；备用请求同样受共享预算约束，无密集重试。"""
    stamp = get_beijing_now_iso()
    previous_closes, latest_pre_close = {}, None
    source, primary_error = TENCENT_SOURCE, ""
    try:
        try:
            parsed, previous_closes = fetch_tencent_series(
                instrument_symbol(meta), STANDARD_TRADING_MINUTES, ndays=ndays, is_index=(kind == "index")
            )
        except Exception as exc:
            primary_error = str(exc)
            source = SOURCE_NAME
            raw = fetch_trends_raw(meta["secid"], ndays=ndays, timeout=8)
            parsed = parse_trends(raw, value_index=2 if kind == "index" else 6, strict_positive=(kind == "index"))
            if not parsed:
                raise ValueError(f"腾讯：{primary_error}；东方财富备用暂不可用")
            response_dates = [row.split(" ", 1)[0] for row in raw["data"]["trends"] if isinstance(row, str)]
            if max(response_dates, default="") == max(parsed):
                latest_pre_close = positive_finite(raw["data"].get("preClose"))
        if kind == "index":
            merge_and_save_index_data(volume_dir, key, parsed, latest_pre_close, source, previous_closes)
        else:
            merge_and_save_etf_data(volume_dir, key, parsed, source)
        report = {
            "status": "ok", "source": source, "last_success_at": get_beijing_now_iso(),
            "last_attempt_at": stamp, "dates": sorted(parsed), "dates_count": len(parsed),
            "primary_error": primary_error,
        }
    except Exception as exc:
        report = {"status": "failed", "error": str(exc), "last_attempt_at": stamp}
        print(f"[AShareVolume] {key}: {exc}", file=sys.stderr)
    with volume_lock:
        field = "index_statuses" if kind == "index" else "etf_statuses"
        volume_runtime_state[field][key] = report
        if report["status"] == "ok":
            volume_runtime_state["last_success_at"] = report["last_success_at"]
        payload_cache["payload"] = None
    return report


def update_collector_status():
    with volume_lock:
        etfs = volume_runtime_state["etf_statuses"]
        indices = volume_runtime_state["index_statuses"]
        reports = list(etfs.values()) + list(indices.values())
        failures = [r for r in reports if r["status"] != "ok"]
        successes = len(reports) - len(failures)
        if failures:
            status = "partial_degraded" if successes else "cached_fallback"
            desc = f"低频采集：分时校准成功{successes}/14，部分请求暂不可用，保留已有数据"
        elif len(reports) < 14:
            status = "syncing"
            desc = f"低频采集：正在分散补齐分时（{successes}/14），允许数分钟延迟"
        else:
            status = "online_fresh"
            fallback = sum(r.get("source") == SOURCE_NAME for r in reports)
            desc = "腾讯优先 · 分时低频校准完成，允许数分钟延迟"
            if fallback:
                desc += f" · 东方财富备用{fallback}个标的"
        volume_runtime_state["status"] = status
        volume_runtime_state["status_desc"] = desc
        volume_runtime_state["last_error"] = "; ".join(r["error"] for r in failures)
        payload_cache["payload"] = None
        return status, desc


def sync_all_etfs(volume_dir=None, ndays=1, throttle_delay=0.25):
    """完整校准入口；所有请求仍经过预算，后台改为逐标的分散调度。"""
    vdir, dates = volume_dir or VOLUME_DIR, set()
    for kind, key, meta in collector_instruments():
        report = sync_instrument(vdir, kind, key, meta, ndays)
        dates.update(report.get("dates", []))
        if throttle_delay:
            time.sleep(throttle_delay)
    cleanup_old_trading_days(vdir, keep_days=MAX_RETENTION_DAYS)
    status, desc = update_collector_status()
    with volume_lock:
        reports = dict(volume_runtime_state["etf_statuses"])
    return status, dates, desc, reports


def compute_baseline_median(hist_day_records, group_codes, minute):
    """
    计算某组在指定分钟的历史同分钟成交额中位数与实际样本数。
    严禁填 0：历史日某 ETF 缺失、金额无效或超过该 ETF 的 complete_through 边界，
    均直接剔除出该分钟样本池，绝不把半截停机样本混作完整样本。
    """
    valid_daily_sums = []
    for day_rec in hist_day_records:
        etfs_dict = day_rec.get("etfs", {})
        day_amounts = []
        is_complete = True

        for code in group_codes:
            etf_obj = etfs_dict.get(code, {})
            etf_data = etf_obj.get("data", {})
            etf_cp = etf_obj.get("complete_through")

            # 完整性边界核验：该分钟必须 <= 该 ETF 在该日的 complete_through
            if etf_cp is None or minute > etf_cp:
                is_complete = False
                break

            val = etf_data.get(minute)
            if val is None or not math.isfinite(val) or val < 0:
                is_complete = False
                break
            day_amounts.append(val)

        if is_complete and len(day_amounts) == len(group_codes):
            valid_daily_sums.append(sum(day_amounts))

    n = len(valid_daily_sums)
    if n == 0:
        return None, 0

    valid_daily_sums.sort()
    if n % 2 == 1:
        med = valid_daily_sums[n // 2]
    else:
        med = (valid_daily_sums[n // 2 - 1] + valid_daily_sums[n // 2]) / 2.0
    return round(med, 2), n


def calculate_minute_volume_payload(volume_dir=None, target_date=None):
    """
    计算指定交易日（默认最新）的全部分组一分钟成交额、历史基准中位数及放大倍数。
    参数校验：
    - 日期格式非法直接返回 400
    - 日期不在本地历史归档中返回 404
    - 缺省 target_date 时安全默认展示最新归档日
    """
    vdir = volume_dir or VOLUME_DIR
    avail_dates = get_available_dates(vdir)

    # 1. 目标日期校验
    if target_date is not None and str(target_date).strip():
        date_cand = str(target_date).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_cand):
            return {
                "code": 400,
                "status": "error",
                "message": f"日期格式错误: {date_cand}，正确格式应为 YYYY-MM-DD"
            }
        if date_cand not in avail_dates:
            return {
                "code": 404,
                "status": "error",
                "message": f"未找到交易日 {date_cand} 的成交归档数据"
            }
        effective_date = date_cand
    else:
        if not avail_dates:
            with volume_lock:
                st = volume_runtime_state["status"]
                desc = volume_runtime_state["status_desc"]
                last_err = volume_runtime_state["last_error"]
            return {
                "code": 200,
                "status": "initial_loading" if st == "uninitialized" else st,
                "status_desc": desc if st != "error" else f"数据未就绪: {last_err}",
                "date": "",
                "data_time": "",
                "last_success_at": "",
                "available_dates": [],
                "baseline_info": {
                    "max_days": MAX_BASELINE_DAYS,
                    "actual_days": 0,
                    "description": "暂无历史基准数据"
                },
                "groups": [],
                "aggregate": None
            }
        effective_date = avail_dates[0]

    target_day_data = load_day_file(vdir, effective_date)
    if not target_day_data:
        return {
            "code": 404,
            "status": "error",
            "message": f"未找到交易日 {effective_date} 数据文件"
        }

    # 取前至多 20 个交易日作为历史基准（排除自身，绝不跨越无限填补）
    prior_dates = get_prior_trading_dates(vdir, effective_date, max_days=MAX_BASELINE_DAYS)
    hist_day_records = []
    for pd in prior_dates:
        rec = load_day_file(vdir, pd)
        if rec:
            hist_day_records.append(rec)

    actual_baseline_days = len(hist_day_records)
    if actual_baseline_days == 0:
        baseline_desc = "基于前 0 个交易日（暂无历史基准）"
    elif actual_baseline_days < MAX_BASELINE_DAYS:
        baseline_desc = f"基于前 {actual_baseline_days} 个有效交易日同分钟中位数 (积累中，上限20日)"
    else:
        baseline_desc = f"基于前 {MAX_BASELINE_DAYS} 个有效交易日同分钟中位数"

    # 全天 241 个标准交易分钟横轴口径不变，缺失与未到达点留空
    minutes_to_process = STANDARD_TRADING_MINUTES
    latest_data_minute = target_day_data.get("data_time", "15:00")

    now_bj = datetime.now(BEIJING_TZ)
    is_today = (effective_date == now_bj.strftime("%Y-%m-%d"))
    current_hhmm = now_bj.strftime("%H:%M")
    is_active_trading_session = is_today and (("09:30" <= current_hhmm <= "11:30") or ("13:00" <= current_hhmm <= "15:00"))

    # 严格确定截至当前应已完成走完的标准交易分钟清单
    if not is_today or current_hhmm > "15:00":
        expected_finished_minutes = STANDARD_TRADING_MINUTES
    elif current_hhmm < "09:30":
        expected_finished_minutes = []
    elif "09:30" <= current_hhmm <= "11:30":
        expected_finished_minutes = [m for m in STANDARD_TRADING_MINUTES if m < current_hhmm]
    elif "11:30" < current_hhmm <= "13:00":
        expected_finished_minutes = [m for m in STANDARD_TRADING_MINUTES if m <= "11:30"]
    else:  # 13:00 < current_hhmm <= 15:00
        expected_finished_minutes = [m for m in STANDARD_TRADING_MINUTES if m < current_hhmm]

    expected_finished_set = set(expected_finished_minutes)

    with volume_lock:
        index_reports = dict(volume_runtime_state["index_statuses"])
        snapshots = dict(volume_runtime_state["snapshots"])
    groups_output = []
    for grp in A_SHARE_ETF_GROUPS:
        grp_codes = grp["codes"]
        grp_minutes = []
        finished_complete_minutes = []
        max_ratio_item = None
        index_meta = A_SHARE_INDEX_METAS[grp["id"]]
        index_obj = target_day_data.get("indices", {}).get(grp["id"], {})
        index_data = index_obj.get("data", {})
        index_cp = index_obj.get("complete_through")
        pre_close = index_obj.get("pre_close")
        index_report = index_reports.get(grp["id"], {})
        if not index_data:
            index_status, index_desc = "unavailable", "暂无有效指数点位"
        elif index_report.get("status") == "failed":
            index_status, index_desc = "cached_fallback", "指数同步失败，显示已保存点位"
        elif any(index_cp is None or minute > index_cp or minute not in index_data for minute in expected_finished_minutes):
            index_status, index_desc = "partial_degraded", "部分指数分钟缺失或未确认完成"
        elif effective_date in index_report.get("dates", []):
            index_status, index_desc = "online_fresh", "指数同步成功"
        else:
            index_status, index_desc = "cached_fallback", "读取已保存指数点位"
        snapshot = snapshots.get(instrument_symbol(index_meta), {})
        if not is_today or snapshot.get("date") != effective_date:
            snapshot = None
        index_info = {
            "code": index_meta["code"], "name": index_meta["name"], "source": index_obj.get("source", SOURCE_NAME),
            "pre_close": pre_close, "last_success_at": index_obj.get("last_success_at", ""),
            "data_time": max(index_data, default=""), "status": index_status,
            "status_desc": index_desc, "complete_through": index_cp,
            "snapshot": snapshot,
        }

        for m_str in minutes_to_process:
            # 判断当前分钟是否属于正在进行中的未完成分钟
            is_unf = (is_active_trading_session and m_str == current_hhmm)

            med, n = compute_baseline_median(hist_day_records, grp_codes, m_str)

            # 组内齐备校验与当前分钟求和
            etfs_dict = target_day_data.get("etfs", {})
            amounts = []
            is_comp = True

            # 若属于未来尚未到达的分钟，直接置空，不参与齐备校验
            if is_today and m_str > current_hhmm:
                is_comp = False
            else:
                for code in grp_codes:
                    etf_obj = etfs_dict.get(code, {})
                    # 任何非进行中分钟，必须受该 ETF 的 complete_through 约束（防止今日中途故障缓存半额被误作完整样本）
                    if not is_unf:
                        etf_cp = etf_obj.get("complete_through")
                        if etf_cp is None or m_str > etf_cp:
                            is_comp = False
                            break

                    val = etf_obj.get("data", {}).get(m_str)
                    if val is None or not math.isfinite(val) or val < 0:
                        is_comp = False
                        break
                    amounts.append(val)

            if is_comp and len(amounts) == len(grp_codes):
                m_amt = round(sum(amounts), 2)
            else:
                m_amt = None
                is_comp = False

            # 倍数计算：仅对已完成、组内齐备且基准有效的分钟
            ratio = None
            if is_comp and not is_unf and m_amt is not None and med is not None and med > 0 and n > 0:
                ratio = round(m_amt / med, 2)

            index_value = index_data.get(m_str)
            index_complete = index_value is not None and index_cp is not None and m_str <= index_cp
            # 仅当前进行中的点位可越过已保存边界；停机留下的半分钟不能随时钟变成完整值。
            if (is_today and m_str > current_hhmm) or (not index_complete and not is_unf):
                index_value = None
                index_complete = False
            index_change_pct = ((index_value / pre_close - 1) * 100.0) if index_value is not None and pre_close else None
            if index_change_pct is not None and not math.isfinite(index_change_pct):
                index_change_pct = None
            minute_obj = {
                "time": m_str,
                "index_value": index_value,
                "index_change_pct": index_change_pct,
                "index_is_complete": index_complete,
                "index_is_unfinished": index_value is not None and not index_complete,
                "amount": m_amt,
                "amount_str": format_amount_chinese(m_amt),
                "baseline": med,
                "baseline_str": format_amount_chinese(med),
                "sample_n": n,
                "ratio": ratio,
                "is_complete": is_comp,
                "is_unfinished": is_unf
            }
            grp_minutes.append(minute_obj)

            # 仅统计应完成且组内齐备的有效分钟参与累计与极值
            if is_comp and not is_unf and m_amt is not None and m_str in expected_finished_set:
                finished_complete_minutes.append(minute_obj)
                if ratio is not None:
                    if max_ratio_item is None or ratio > max_ratio_item["ratio"]:
                        max_ratio_item = {
                            "time": m_str,
                            "amount": m_amt,
                            "amount_str": minute_obj["amount_str"],
                            "ratio": ratio,
                            "sample_n": n
                        }

        # 累计金额口径：严格统计已完成且组内齐备的分钟总和，绝不隐藏尾部缺失
        cum_amount = round(sum(m["amount"] for m in finished_complete_minutes), 2)
        expected_count = len(expected_finished_minutes)
        completed_count = len(finished_complete_minutes)
        is_cumulative_complete = (completed_count == expected_count) and (expected_count > 0)

        # 组内各 ETF 累计金额与占比：与组累计保持严格同一分钟口径，占比之和恒为 100%
        etfs_breakdown = []
        for code in grp_codes:
            meta = A_SHARE_ETF_METAS.get(code) or LEGACY_ETF_METAS.get(code, {"name": code, "market": ""})
            etf_obj = target_day_data.get("etfs", {}).get(code, {})
            etf_data = etf_obj.get("data", {})
            etf_cum = sum(
                etf_data.get(m["time"], 0.0)
                for m in finished_complete_minutes
                if etf_data.get(m["time"]) is not None and math.isfinite(etf_data.get(m["time"]))
            )
            share_pct = round((etf_cum / cum_amount * 100.0), 1) if cum_amount > 0 else 0.0
            etfs_breakdown.append({
                "code": code,
                "name": meta["name"],
                "market": meta.get("market", ""),
                "last_success_at": etf_obj.get("last_success_at", ""),
                "cumulative_amount": round(etf_cum, 2),
                "cumulative_amount_str": format_amount_chinese(etf_cum),
                "share_pct": share_pct
            })

        latest_min_obj = finished_complete_minutes[-1] if finished_complete_minutes else None

        groups_output.append({
            "id": grp["id"],
            "name": grp["name"],
            "description": grp["description"],
            "etf_count": len(grp_codes),
            "codes": grp_codes,
            "index_info": index_info,
            "cumulative_amount": cum_amount,
            "cumulative_amount_str": format_amount_chinese(cum_amount),
            "is_cumulative_complete": is_cumulative_complete,
            "completed_minutes_count": completed_count,
            "expected_minutes_count": expected_count,
            "latest_minute": latest_min_obj,
            "max_ratio_minute": max_ratio_item,
            "etfs": etfs_breakdown,
            "minutes": grp_minutes
        })

    # 计算顶层 10 只重点 ETF 合计聚合对象 aggregate
    aggregate_output = None
    if groups_output:
        all_monitored_codes = [code for grp in A_SHARE_ETF_GROUPS for code in grp["codes"]]
        agg_minutes = []
        for i, m_str in enumerate(minutes_to_process):
            grp_mins = [g["minutes"][i] for g in groups_output]
            is_unf = (is_active_trading_session and m_str == current_hhmm)

            if is_today and m_str > current_hhmm:
                agg_amt = None
                agg_comp = False
                agg_unf = False
            elif is_unf:
                # 盘中当前未完成分钟：各组如均有暂态金额则如实合计，否则置空；is_complete 置 False，is_unfinished 如实置 True
                if all(gm.get("amount") is not None and math.isfinite(gm["amount"]) for gm in grp_mins):
                    agg_amt = round(sum(gm["amount"] for gm in grp_mins), 2)
                else:
                    agg_amt = None
                agg_comp = False
                agg_unf = True
            else:
                # 已走完的分钟：所有指数组必须全部完整且有效，任一组缺失或不完整则合计置空，严禁填0或混不同时间
                if all(gm.get("is_complete") and not gm.get("is_unfinished", False) and gm.get("amount") is not None and math.isfinite(gm["amount"]) for gm in grp_mins):
                    agg_amt = round(sum(gm["amount"] for gm in grp_mins), 2)
                    agg_comp = True
                else:
                    agg_amt = None
                    agg_comp = False
                agg_unf = False

            agg_minutes.append({
                "time": m_str,
                "amount": agg_amt,
                "amount_str": format_amount_chinese(agg_amt),
                "is_complete": agg_comp,
                "is_unfinished": agg_unf
            })

        # 累计金额口径：仅统计已完成且全部 10 只 ETF 齐备的分钟
        agg_finished_complete_minutes = [
            m for m in agg_minutes
            if m["is_complete"] and not m["is_unfinished"] and m["amount"] is not None and m["time"] in expected_finished_set
        ]
        agg_cum_amount = round(sum(m["amount"] for m in agg_finished_complete_minutes), 2)
        agg_expected_count = len(expected_finished_minutes)
        agg_completed_count = len(agg_finished_complete_minutes)
        agg_is_cumulative_complete = (agg_completed_count == agg_expected_count) and (agg_expected_count > 0)
        agg_data_time = agg_finished_complete_minutes[-1]["time"] if agg_finished_complete_minutes else ""
        aggregate_etfs = []
        for code in all_monitored_codes:
            etf_data = target_day_data["etfs"].get(code, {}).get("data", {})
            etf_amount = round(sum(etf_data[m["time"]] for m in agg_finished_complete_minutes), 2) if agg_completed_count else None
            aggregate_etfs.append({
                "code": code,
                "name": A_SHARE_ETF_METAS[code]["name"],
                "cumulative_amount": etf_amount,
                "cumulative_amount_str": format_amount_chinese(etf_amount),
                "share_pct": round(etf_amount / agg_cum_amount * 100, 2) if etf_amount is not None and agg_cum_amount > 0 else None
            })
        aggregate_etfs.sort(key=lambda etf: (etf["cumulative_amount"] is None, -(etf["cumulative_amount"] or 0), etf["code"]))

        aggregate_output = {
            "name": "10只重点ETF合计",
            "codes": all_monitored_codes,
            "etf_count": len(all_monitored_codes),
            "data_time": agg_data_time,
            "cumulative_amount": agg_cum_amount,
            "cumulative_amount_str": format_amount_chinese(agg_cum_amount),
            "is_cumulative_complete": agg_is_cumulative_complete,
            "completed_minutes_count": agg_completed_count,
            "expected_minutes_count": agg_expected_count,
            "latest_minute": agg_finished_complete_minutes[-1] if agg_finished_complete_minutes else None,
            "etfs": aggregate_etfs,
            "minutes": agg_minutes
        }

    with volume_lock:
        st = volume_runtime_state["status"]
        st_desc = volume_runtime_state["status_desc"]
        last_succ = volume_runtime_state["last_success_at"]

    return {
        "code": 200,
        "status": st if st != "uninitialized" else "cached_fallback",
        "status_desc": st_desc if st != "uninitialized" else "读取本地历史存储",
        "date": effective_date,
        "data_time": latest_data_minute,
        "last_success_at": last_succ or target_day_data.get("updated_at", ""),
        "available_dates": avail_dates,
        "baseline_info": {
            "max_days": MAX_BASELINE_DAYS,
            "actual_days": actual_baseline_days,
            "description": baseline_desc
        },
        "groups": groups_output,
        "aggregate": aggregate_output
    }


def get_a_share_volume_payload(target_date=None, force=False, volume_dir=None):
    """只读本地档案；force仅跳过计算缓存，不触发上游请求。"""
    vdir = volume_dir or VOLUME_DIR
    now = time.time()
    payload = None
    with volume_lock:
        if not force and payload_cache["payload"] and payload_cache["date"] == target_date:
            if (now - payload_cache["timestamp"]) < PAYLOAD_CACHE_TTL:
                payload = payload_cache["payload"]
    if payload is None:
        payload = calculate_minute_volume_payload(vdir, target_date=target_date)
        if isinstance(payload, dict) and payload.get("code") == 200:
            with volume_lock:
                payload_cache["date"] = target_date
                payload_cache["timestamp"] = now
                payload_cache["payload"] = payload
    with volume_lock:
        snapshot_error = volume_runtime_state["snapshot_error"]
    return {**payload, "collector": {
        **request_budget.info(), "scope": "a_share_volume_and_daily",
        "snapshot_interval_seconds": 60, "series_interval_seconds": 20,
        "snapshot_error": snapshot_error,
    }}


class LowRateCollector:
    """快照至多每60秒一次；完整分时逐个发送，正常约5分钟覆盖一轮。"""
    def __init__(self, volume_dir=None):
        self.volume_dir = volume_dir or VOLUME_DIR
        self.instruments = collector_instruments()
        self.cursor = 0
        self.bootstrap_remaining = len(self.instruments)
        self.history_pending = {(kind, key) for kind, key, _ in self.instruments}
        self.next_snapshot = 0
        self.next_series = 0
        self.report_date = None

    def tick(self, now=None):
        now = now or datetime.now(BEIJING_TZ)
        hhmm = now.strftime("%H:%M")
        active = now.weekday() < 5 and ("09:25" <= hhmm <= "11:35" or "12:55" <= hhmm <= "15:35")
        if not active and not self.bootstrap_remaining:
            return False
        if self.report_date != now.date():
            self.report_date = now.date()
            with volume_lock:
                volume_runtime_state["etf_statuses"] = {}
                volume_runtime_state["index_statuses"] = {}
        worked = False
        if time.monotonic() >= self.next_snapshot and (active or self.bootstrap_remaining == len(self.instruments)):
            try:
                symbols = [instrument_symbol(meta) for _, _, meta in self.instruments]
                snapshots = fetch_tencent_snapshot(symbols)
                with volume_lock:
                    for symbol, quote in snapshots.items():
                        previous = volume_runtime_state["snapshots"].get(symbol, {})
                        if quote["time"] >= previous.get("time", ""):
                            volume_runtime_state["snapshots"][symbol] = quote
                    volume_runtime_state["snapshot_error"] = ""
                    payload_cache["payload"] = None
            except Exception as exc:
                with volume_lock:
                    volume_runtime_state["snapshot_error"] = str(exc)
                print(f"[AShareVolume Snapshot] {exc}", file=sys.stderr)
            self.next_snapshot = time.monotonic() + 60
            worked = True
        if time.monotonic() >= self.next_series:
            kind, key, meta = self.instruments[self.cursor]
            with volume_lock:
                volume_runtime_state["is_syncing"] = True
                volume_runtime_state["last_attempt_at"] = get_beijing_now_iso()
            try:
                report = sync_instrument(self.volume_dir, kind, key, meta, 5 if (kind, key) in self.history_pending else 1)
                if report["status"] == "ok":
                    self.history_pending.discard((kind, key))
                self.cursor = (self.cursor + 1) % len(self.instruments)
                self.bootstrap_remaining = max(0, self.bootstrap_remaining - 1)
                if self.cursor == 0:
                    cleanup_old_trading_days(self.volume_dir)
                update_collector_status()
            finally:
                self.next_series = time.monotonic() + 20
                with volume_lock:
                    volume_runtime_state["is_syncing"] = False
            worked = True
        return worked


def volume_collector_daemon(volume_dir=None):
    """启动补采也分散执行；不补发错过的轮次，不绕过共享请求预算。"""
    ensure_volume_storage(volume_dir)
    collector = LowRateCollector(volume_dir)
    print("[AShareVolume] 低频采集启动：腾讯优先，共享预算最多5次/60秒", flush=True)
    while True:
        try:
            collector.tick()
        except Exception as exc:
            with volume_lock:
                volume_runtime_state["last_error"] = str(exc)
                volume_runtime_state["is_syncing"] = False
            print(f"[AShareVolume Daemon Error] {exc}", file=sys.stderr)
        time.sleep(1)


def start_a_share_volume_daemon(volume_dir=None):
    """由 server.py 的 run_server 显式调用启动后台守护线程"""
    t = threading.Thread(
        target=volume_collector_daemon,
        args=(volume_dir,),
        daemon=True,
        name="AShareVolumeDaemon"
    )
    t.start()
    print("🤖 [AShareVolume] A股一分钟成交额采集守护线程已就绪")
