#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CrossETF - A股跨境ETF实时溢价率追踪与量化监控服务
标的池机制：在线优先拉取 + 每日持久化缓存 + 自动降级使用前日缓存，零第三方依赖。
"""

import json
import time
import math
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import os
import sys
import re
import threading
from datetime import datetime, timedelta

# 基础配置
APP_VERSION = "v1.5.1"
PORT = int(os.environ.get("PORT", 8000))
CACHE_TTL = 3  # 行情缓存时间（秒），防止高频刷新打爆上游接口
FEAR_GREED_URL = "https://feargreedflow.com/api/fear-greed"
FEAR_GREED_CACHE_TTL = 3600
FEAR_GREED_RETRY_TTL = 600
FEAR_GREED_TIMEOUT = 8

# 缓存与数据文件路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(CURRENT_DIR, "data")))
SYMBOLS_CACHE_FILE = os.path.join(DATA_DIR, "nasdaq_symbols_cache.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history_daily.json")
FEAR_GREED_CACHE_FILE = os.path.join(DATA_DIR, "fear_greed_cache.json")


def ensure_data_storage():
    """确保持久化数据目录存在"""
    os.makedirs(DATA_DIR, exist_ok=True)


# 跨境 ETF 官方真实综合运作年费率字典（管理费 + 托管费，按日从基金净值计提）
ETF_TOTAL_FEES = {
    # 1.00% 梯队 (管理费 0.80% + 托管费 0.20%)
    "159941": "1.00%",
    "513110": "1.00%",
    "159513": "1.00%",
    "159509": "1.00%",
    # 0.80% 梯队 (管理费 0.60% + 托管费 0.20%)
    "159632": "0.80%",
    "513300": "0.80%",
    "513100": "0.80%",
    "513500": "0.80%",
    # 0.75% 梯队 (管理费 0.60% + 托管费 0.15%)
    "159612": "0.75%",
    "159655": "0.75%",
    "513650": "0.75%",
    # 0.65% 梯队 (管理费 0.50% + 托管费 0.15%)
    "513390": "0.65%",
    "159659": "0.65%",
    "159660": "0.65%",
    "513290": "0.65%",
    # 0.60% 极低费率梯队 (管理费 0.50% + 托管费 0.10%)
    "513870": "0.60%",
    "159696": "0.60%",
    "159501": "0.60%",
}

# 出厂内置保底名单（仅在首次启动无网络且本地没有任何历史缓存文件时极罕见触发）
DEFAULT_BROAD_ETFS = [
    {"code": "513390", "name": "纳指100ETF博时", "symbol": "sh513390", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513110", "name": "纳指ETF华泰柏瑞", "symbol": "sh513110", "fee": "1.00%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159513", "name": "纳斯达克100ETF大成", "symbol": "sz159513", "fee": "1.00%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159632", "name": "纳斯达克ETF华安", "symbol": "sz159632", "fee": "0.80%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159659", "name": "纳斯达克100ETF招商", "symbol": "sz159659", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159660", "name": "纳指ETF汇添富", "symbol": "sz159660", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513870", "name": "纳指ETF富国", "symbol": "sh513870", "fee": "0.60%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513300", "name": "纳斯达克ETF华夏", "symbol": "sh513300", "fee": "0.80%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159696", "name": "纳指ETF易方达", "symbol": "sz159696", "fee": "0.60%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159941", "name": "纳指ETF广发", "symbol": "sz159941", "fee": "1.00%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159501", "name": "纳指ETF嘉实", "symbol": "sz159501", "fee": "0.60%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513100", "name": "纳指ETF国泰", "symbol": "sh513100", "fee": "0.80%", "index_name": "纳斯达克100指数(^NDX)"},
]

DEFAULT_SECTOR_ETFS = [
    {
        "code": "159509",
        "name": "纳指科技ETF景顺",
        "symbol": "sz159509",
        "fee": "1.00%",
        "index_name": "纳斯达克科技市值加权指数(NDXT)",
        "note": "跟踪科技细分指数，非纳指100宽基"
    },
    {
        "code": "513290",
        "name": "纳指生物科技ETF汇添富",
        "symbol": "sh513290",
        "fee": "0.65%",
        "index_name": "纳斯达克生物科技指数(NBI)",
        "note": "跟踪生物科技细分指数，非纳指100宽基"
    }
]

DEFAULT_SP500_ETFS = [
    {"code": "513500", "name": "标普500ETF博时", "symbol": "sh513500", "fee": "0.80%", "index_name": "标普500指数(^SPX)"},
    {"code": "159612", "name": "标普500ETF国泰", "symbol": "sz159612", "fee": "0.75%", "index_name": "标普500指数(^SPX)"},
    {"code": "159655", "name": "标普500ETF华夏", "symbol": "sz159655", "fee": "0.75%", "index_name": "标普500指数(^SPX)"},
    {"code": "513650", "name": "标普500ETF南方", "symbol": "sh513650", "fee": "0.75%", "index_name": "标普500指数(^SPX)"},
]

# 行情内存缓存
cache_data = {
    "timestamp": 0,
    "payload": None
}

# 恐惧贪婪指数使用完全独立的低频缓存与刷新锁，不参与 ETF 行情链路。
fear_greed_cache = {
    "initialized": False,
    "payload": None,
    "last_success_epoch": 0.0,
    "last_success_at": "",
    "last_attempt_epoch": 0.0,
    "last_error": "",
}
fear_greed_lock = threading.Lock()

# 标的池内存缓存
symbols_registry = {
    "status": "uninitialized",
    "status_desc": "尚未初始化",
    "cached_date": "",
    "updated_at": "",
    "broad_etfs": [],
    "sector_etfs": [],
    "sp500_etfs": []
}


def fetch_url(url, referer=None, timeout=4):
    """通用 HTTP 请求封装"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            for encoding in ["gbk", "utf-8", "gb2312"]:
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[Warning] Fetching {url}: {e}", file=sys.stderr)
        return ""


def classify_fear_greed(score):
    """将 0～100 分数映射为唯一、无重叠的市场情绪等级。"""
    if score < 25:
        return "extreme_fear", "极度恐惧"
    if score < 45:
        return "fear", "恐惧"
    if score < 55:
        return "neutral", "中性"
    if score < 75:
        return "greed", "贪婪"
    return "extreme_greed", "极度贪婪"


def _parse_iso_datetime(value):
    """验证 ISO 时间；返回原值，避免把上游时间伪装成本地当前时间。"""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        return candidate
    except ValueError:
        return None


def normalize_fear_greed_payload(raw):
    """校验 FearGreedFlow 响应并转换为 CrossETF 的稳定数据结构。"""
    if not isinstance(raw, dict):
        raise ValueError("upstream payload must be an object")

    dates = raw.get("dates")
    values = raw.get("values")
    if not isinstance(dates, list) or not isinstance(values, list):
        raise ValueError("dates and values must be arrays")
    if not dates or len(dates) != len(values):
        raise ValueError("dates and values must be non-empty and have equal length")

    points_by_date = {}
    for raw_date, raw_value in zip(dates, values):
        try:
            parsed_date = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
            if isinstance(raw_value, bool):
                raise ValueError("boolean is not a score")
            score = float(raw_value)
            if not math.isfinite(score) or score < 0 or score > 100:
                raise ValueError("score is outside 0-100")
        except (TypeError, ValueError):
            continue
        points_by_date[parsed_date] = score

    if not points_by_date:
        raise ValueError("upstream payload has no valid data points")

    ordered_points = sorted(points_by_date.items())
    latest_date, latest_score = ordered_points[-1]
    cutoff = latest_date - timedelta(days=29)
    history = [
        {"date": point_date.isoformat(), "value": round(point_value, 1)}
        for point_date, point_value in ordered_points
        if point_date >= cutoff
    ]

    rating, rating_label = classify_fear_greed(latest_score)
    as_of = _parse_iso_datetime(raw.get("asOf")) or latest_date.isoformat()
    return {
        "source": "feargreedflow",
        "score": round(latest_score, 1),
        "rating": rating,
        "rating_label": rating_label,
        "as_of": as_of,
        "history": history,
    }


def _valid_cached_fear_greed(payload):
    """对持久化的标准化数据做最小但严格的结构校验。"""
    if not isinstance(payload, dict) or payload.get("source") != "feargreedflow":
        return False
    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    if not math.isfinite(score) or score < 0 or score > 100:
        return False
    rating, label = classify_fear_greed(score)
    if payload.get("rating") != rating or payload.get("rating_label") != label:
        return False
    if not payload.get("as_of") or not isinstance(payload.get("history"), list) or not payload["history"]:
        return False
    try:
        for point in payload["history"]:
            datetime.strptime(point["date"], "%Y-%m-%d")
            value = float(point["value"])
            if not math.isfinite(value) or value < 0 or value > 100:
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def load_fear_greed_cache():
    """读取最后有效持久化缓存；损坏文件会被忽略，不影响服务启动。"""
    if not os.path.exists(FEAR_GREED_CACHE_FILE):
        return None
    try:
        with open(FEAR_GREED_CACHE_FILE, "r", encoding="utf-8") as cache_file:
            saved = json.load(cache_file)
        payload = saved.get("payload")
        if not _valid_cached_fear_greed(payload):
            raise ValueError("invalid cached payload")
        success_epoch = float(saved.get("last_success_epoch") or os.path.getmtime(FEAR_GREED_CACHE_FILE))
        success_at = saved.get("last_success_at") or datetime.fromtimestamp(success_epoch).astimezone().isoformat(timespec="seconds")
        return {
            "payload": payload,
            "last_success_epoch": success_epoch,
            "last_success_at": success_at,
        }
    except Exception as exc:
        print(f"[Warning] 读取恐惧贪婪指数缓存失败: {exc}", file=sys.stderr)
        return None


def save_fear_greed_cache(payload, last_success_epoch, last_success_at):
    """以临时文件 + 原子替换方式保存最后有效数据。"""
    ensure_data_storage()
    temp_file = FEAR_GREED_CACHE_FILE + ".tmp"
    saved = {
        "schema_version": 1,
        "last_success_epoch": last_success_epoch,
        "last_success_at": last_success_at,
        "payload": payload,
    }
    try:
        with open(temp_file, "w", encoding="utf-8") as cache_file:
            json.dump(saved, cache_file, ensure_ascii=False, indent=2)
            cache_file.flush()
            os.fsync(cache_file.fileno())
        os.replace(temp_file, FEAR_GREED_CACHE_FILE)
    except Exception:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
        raise


def fetch_fear_greed_upstream():
    """获取并解码在线情绪数据；失败时抛出异常交给缓存状态机处理。"""
    last_error = None
    attempt_timeout = max(2.0, FEAR_GREED_TIMEOUT / 3.0)
    for _ in range(3):
        request = urllib.request.Request(
            FEAR_GREED_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": f"CrossETF/{APP_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=attempt_timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"upstream HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("upstream request failed")


def _fear_greed_api_payload(status, stale):
    payload = dict(fear_greed_cache["payload"])
    payload.update({
        "valid": True,
        "status": status,
        "stale": stale,
        "last_success_at": fear_greed_cache["last_success_at"],
    })
    return {"code": 200, "message": "success", "data": payload}


def _fear_greed_unavailable_payload():
    return {
        "code": 503,
        "message": "fear and greed data unavailable",
        "data": {
            "valid": False,
            "status": "unavailable",
            "stale": False,
            "source": "feargreedflow",
        },
    }


def reset_fear_greed_runtime_cache():
    """清空情绪内存状态，供测试与受控重载使用。"""
    fear_greed_cache.update({
        "initialized": False,
        "payload": None,
        "last_success_epoch": 0.0,
        "last_success_at": "",
        "last_attempt_epoch": 0.0,
        "last_error": "",
    })


def get_fear_greed_data(now=None):
    """执行独立的成功缓存、失败重试与最后有效数据降级状态机。"""
    now = time.time() if now is None else float(now)
    with fear_greed_lock:
        if not fear_greed_cache["initialized"]:
            saved = load_fear_greed_cache()
            if saved:
                fear_greed_cache.update(saved)
            fear_greed_cache["initialized"] = True

        payload = fear_greed_cache["payload"]
        success_age = now - fear_greed_cache["last_success_epoch"]
        if payload and 0 <= success_age < FEAR_GREED_CACHE_TTL:
            return 200, _fear_greed_api_payload("fresh", False)

        attempt_age = now - fear_greed_cache["last_attempt_epoch"]
        if fear_greed_cache["last_attempt_epoch"] and 0 <= attempt_age < FEAR_GREED_RETRY_TTL:
            if payload:
                return 200, _fear_greed_api_payload("stale", True)
            return 503, _fear_greed_unavailable_payload()

        fear_greed_cache["last_attempt_epoch"] = now
        try:
            normalized = normalize_fear_greed_payload(fetch_fear_greed_upstream())
            success_at = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
            fear_greed_cache.update({
                "payload": normalized,
                "last_success_epoch": now,
                "last_success_at": success_at,
                "last_error": "",
            })
            try:
                save_fear_greed_cache(normalized, now, success_at)
            except Exception as persist_error:
                print(f"[Warning] 保存恐惧贪婪指数缓存失败: {persist_error}", file=sys.stderr)
            return 200, _fear_greed_api_payload("fresh", False)
        except Exception as exc:
            fear_greed_cache["last_error"] = str(exc)
            print(f"[Warning] 恐惧贪婪指数刷新失败: {exc}", file=sys.stderr)
            if fear_greed_cache["payload"]:
                return 200, _fear_greed_api_payload("stale", True)
            return 503, _fear_greed_unavailable_payload()


def discover_etfs_online(force=False):
    """
    标的池管理状态机：
    1. 首选尝试在线抓取天天基金官方全量公募库；
    2. 在线拉取成功后，自动持久化写入本地 nasdaq_symbols_cache.json；
    3. 若当天在线获取失败，自动读取前日缓存兜底，保障服务平稳可用。
    """
    today_str = time.strftime("%Y-%m-%d")
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # 如果非强制刷新，且内存中已是当日最新在线数据，直接复用
    if not force and symbols_registry["broad_etfs"] and (symbols_registry["cached_date"] == today_str) and (symbols_registry["status"] == "online_fresh"):
        return symbols_registry

    # 尝试在线抓取
    url = "http://fund.eastmoney.com/js/fundcode_search.js"
    content = fetch_url(url, timeout=5)

    broad_list = []
    sector_list = []
    sp500_list = []

    if content:
        # 正则提取全量公募基金 [代码, 拼音, 名称, 类型, 全拼]
        items = re.findall(r'\[\"(\d{6})\",\"([^\"]*)\",\"([^\"]+)\",\"([^\"]+)\",\"([^\"]+)\"\]', content)
        for code, abbr, name, ftype, fullpinyin in items:
            if code.startswith(("159", "513")) and ("纳指" in name or "纳斯达克" in name) and "ETF" in name:
                symbol = ("sh" + code) if code.startswith("513") else ("sz" + code)

                # 智能识别指数属性（生物优先识别，避免生物科技被误判为科技）
                is_sector = False
                if "生物" in name or "医药" in name:
                    is_sector = True
                    index_name = "纳斯达克生物科技指数(NBI)"
                    note = "跟踪生物科技细分指数，非纳指100宽基"
                elif "科技" in name:
                    is_sector = True
                    index_name = "纳斯达克科技市值加权指数(NDXT)"
                    note = "跟踪科技细分指数，非纳指100宽基"
                elif "半导体" in name:
                    is_sector = True
                    index_name = "纳斯达克半导体细分指数"
                    note = "跟踪半导体行业细分指数，非纳指100宽基"
                else:
                    index_name = "纳斯达克100指数(^NDX)"
                    note = "纳指100核心综合宽基"

                fee = ETF_TOTAL_FEES.get(code, "0.65%")

                item = {
                    "code": code,
                    "name": name,
                    "symbol": symbol,
                    "fee": fee,
                    "index_name": index_name,
                    "is_sector": is_sector,
                    "note": note
                }

                if is_sector:
                    sector_list.append(item)
                else:
                    broad_list.append(item)

            # 标普500核心宽基 ETF 探测（严格限定标普500核心宽基，剔除油气、消费、生物等细分行业）
            if code.startswith(("159", "513")) and ("标普500" in name or "S&P500" in name or "S&P 500" in name) and "ETF" in name:
                if not any(k in name for k in ["油气", "生物", "消费", "红利", "信息", "科技", "医药"]):
                    symbol = ("sh" + code) if code.startswith("513") else ("sz" + code)
                    fee = ETF_TOTAL_FEES.get(code, "0.75%")
                    sp500_list.append({
                        "code": code,
                        "name": name,
                        "symbol": symbol,
                        "fee": fee,
                        "index_name": "标普500指数(^SPX)",
                        "is_sector": False,
                        "note": "标普500核心宽基"
                    })

    # 路径 1: 在线拉取成功 -> 更新内存并持久化写入本地缓存
    if broad_list or sp500_list:
        log_msg = f"[Sync] 标的在线拉取成功并已持久化缓存: 宽基 {len(broad_list)} 只, 细分 {len(sector_list)} 只, 标普500 {len(sp500_list)} 只"
        symbols_registry["status"] = "online_fresh"
        symbols_registry["status_desc"] = f"🟢 当日在线最新 ({now_str})"
        symbols_registry["cached_date"] = today_str
        symbols_registry["updated_at"] = now_str
        symbols_registry["broad_etfs"] = broad_list
        symbols_registry["sector_etfs"] = sector_list
        symbols_registry["sp500_etfs"] = sp500_list
        symbols_registry["last_log"] = log_msg

        try:
            cache_payload = {
                "status": "cached_fallback",
                "cached_date": today_str,
                "updated_at": now_str,
                "broad_count": len(broad_list),
                "sector_count": len(sector_list),
                "sp500_count": len(sp500_list),
                "broad_etfs": broad_list,
                "sector_etfs": sector_list,
                "sp500_etfs": sp500_list
            }
            with open(SYMBOLS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_payload, f, ensure_ascii=False, indent=2)
            print(log_msg)
        except Exception as e:
            print(f"[Warning] 写入本地缓存失败: {e}", file=sys.stderr)

        return symbols_registry

    # 路径 2: 在线获取失败（如网络中断或超时） -> 自动滑动读取前日/历史持久化缓存
    if os.path.exists(SYMBOLS_CACHE_FILE):
        try:
            with open(SYMBOLS_CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                symbols_registry["status"] = "cached_fallback"
                symbols_registry["status_desc"] = f"🟡 自动使用前日缓存 ({saved.get('cached_date', '历史')})"
                symbols_registry["cached_date"] = saved.get("cached_date", "")
                symbols_registry["updated_at"] = saved.get("updated_at", "")
                symbols_registry["broad_etfs"] = saved.get("broad_etfs") or DEFAULT_BROAD_ETFS
                symbols_registry["sector_etfs"] = saved.get("sector_etfs") or DEFAULT_SECTOR_ETFS
                symbols_registry["sp500_etfs"] = saved.get("sp500_etfs") or DEFAULT_SP500_ETFS
                log_msg = f"[Fallback] ⚠️ 在线拉取失败/未响应，已自动降级使用前日持久化缓存 (缓存日期: {symbols_registry['cached_date']})"
                symbols_registry["last_log"] = log_msg
                print(log_msg)
                return symbols_registry
        except Exception as e:
            print(f"[Warning] 读取前日缓存失败: {e}", file=sys.stderr)

    # 路径 3: 仅在首次冷启动且无网无缓存时的终极保底
    symbols_registry["status"] = "emergency_default"
    symbols_registry["status_desc"] = "🔴 极冷启动且无网，使用出厂预置标的"
    symbols_registry["cached_date"] = today_str
    symbols_registry["updated_at"] = now_str
    symbols_registry["broad_etfs"] = DEFAULT_BROAD_ETFS
    symbols_registry["sector_etfs"] = DEFAULT_SECTOR_ETFS
    symbols_registry["sp500_etfs"] = DEFAULT_SP500_ETFS
    log_msg = "[Fallback] 首次启动无网络且无前日缓存，使用出厂预置标的池"
    symbols_registry["last_log"] = log_msg
    print(log_msg)
    return symbols_registry


def get_market_data():
    """获取并综合计算所有宏观指数、期货及各 ETF 实时溢价率"""
    now = time.time()
    if cache_data["payload"] and (now - cache_data["timestamp"] < CACHE_TTL):
        return cache_data["payload"]

    # 确保标的池已就绪（在线拉取或前日缓存降级）
    registry = discover_etfs_online()
    active_broad_etfs = registry["broad_etfs"]
    active_sector_etfs = registry["sector_etfs"]
    active_sp500_etfs = registry["sp500_etfs"]

    all_symbols = [item["symbol"] for item in active_broad_etfs + active_sector_etfs + active_sp500_etfs]
    tc_url = "http://qt.gtimg.cn/q=" + ",".join(all_symbols)
    sina_url = "http://hq.sinajs.cn/list=hf_NQ,hf_ES,fx_susdcnh,gb_$ndx,gb_$spx"

    # 抓取腾讯 A 股 ETF 行情与新浪外盘期货/汇率
    tc_raw = fetch_url(tc_url)
    sina_raw = fetch_url(sina_url, referer="https://finance.sina.com.cn")

    # --- 解析宏观数据 ---
    cme_nq = {"price": 0.0, "settle": 0.0, "change": 0.0, "change_pct": 0.0, "valid": False, "time": "--"}
    cme_es = {"price": 0.0, "settle": 0.0, "change": 0.0, "change_pct": 0.0, "valid": False, "time": "--"}
    usd_cnh = {"rate": 0.0, "close": 0.0, "change": 0.0, "change_pct": 0.0, "valid": False, "time": "--"}
    ndx_index = {"price": 0.0, "change_pct": 0.0, "time": "--"}
    spx_index = {"price": 0.0, "change_pct": 0.0, "time": "--"}

    nq_valid = False
    es_valid = False
    fx_valid = False

    for line in sina_raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var_name, val = line.split("=", 1)
        content = val.strip('"\n\r ')
        fields = content.split(",")

        if "hf_NQ" in var_name and len(fields) > 8:
            try:
                p = float(fields[0]) if fields[0] else 0.0
                s = float(fields[7]) if fields[7] else 0.0
                # 基准校验：当前价与昨结价必须均为有限正数，缺少基准值不能用当前值代替
                if math.isfinite(p) and p > 0 and math.isfinite(s) and s > 0:
                    chg = p - s
                    pct = (chg / s) * 100.0
                    if math.isfinite(pct):
                        nq_valid = True
                        cme_nq = {
                            "price": round(p, 2),
                            "settle": round(s, 2),
                            "change": round(chg, 2),
                            "change_pct": round(pct, 2),
                            "valid": True,
                            "time": fields[6] if len(fields) > 6 else "--"
                        }
            except Exception as e:
                print(f"[Error] Parsing NQ: {e}", file=sys.stderr)

        elif "fx_susdcnh" in var_name and len(fields) > 3:
            try:
                r = float(fields[1]) if fields[1] else 0.0
                c = float(fields[3]) if fields[3] else 0.0
                # 基准校验：当前汇率与前收盘价必须均为有限正数
                if math.isfinite(r) and r > 0 and math.isfinite(c) and c > 0:
                    chg = r - c
                    pct = (chg / c) * 100.0
                    if math.isfinite(pct):
                        fx_valid = True
                        usd_cnh = {
                            "rate": round(r, 4),
                            "close": round(c, 4),
                            "change": round(chg, 4),
                            "change_pct": round(pct, 3),
                            "valid": True,
                            "time": fields[0] if len(fields) > 0 else "--"
                        }
            except Exception as e:
                print(f"[Error] Parsing CNH: {e}", file=sys.stderr)

        elif "gb_$ndx" in var_name and len(fields) > 2:
            try:
                price = float(fields[1]) if fields[1] else 0.0
                pct = float(fields[2]) if fields[2] else 0.0
                ndx_index = {
                    "price": round(price, 2),
                    "change_pct": round(pct, 2),
                    "time": fields[3] if len(fields) > 3 else "--"
                }
            except Exception as e:
                print(f"[Error] Parsing NDX: {e}", file=sys.stderr)

        elif "hf_ES" in var_name and len(fields) > 8:
            try:
                p = float(fields[0]) if fields[0] else 0.0
                s = float(fields[7]) if fields[7] else 0.0
                # 基准校验：当前价与昨结价必须均为有限正数
                if math.isfinite(p) and p > 0 and math.isfinite(s) and s > 0:
                    chg = p - s
                    pct = (chg / s) * 100.0
                    if math.isfinite(pct):
                        es_valid = True
                        cme_es = {
                            "price": round(p, 2),
                            "settle": round(s, 2),
                            "change": round(chg, 2),
                            "change_pct": round(pct, 2),
                            "valid": True,
                            "time": fields[6] if len(fields) > 6 else "--"
                        }
            except Exception as e:
                print(f"[Error] Parsing ES: {e}", file=sys.stderr)

        elif "gb_$spx" in var_name and len(fields) > 2:
            try:
                price = float(fields[1]) if fields[1] else 0.0
                pct = float(fields[2]) if fields[2] else 0.0
                spx_index = {
                    "price": round(price, 2),
                    "change_pct": round(pct, 2),
                    "time": fields[3] if len(fields) > 3 else "--"
                }
            except Exception as e:
                print(f"[Error] Parsing SPX: {e}", file=sys.stderr)

    # 资产缩放乘数 Scale Factor 计算（按指数独立判断，必须期指与汇率均有效，否则置空）
    if nq_valid and fx_valid:
        scale_factor = (1.0 + cme_nq["change_pct"] / 100.0) * (1.0 + usd_cnh["change_pct"] / 100.0)
    else:
        scale_factor = None

    if es_valid and fx_valid:
        sp500_scale_factor = (1.0 + cme_es["change_pct"] / 100.0) * (1.0 + usd_cnh["change_pct"] / 100.0)
    else:
        sp500_scale_factor = None

    # --- 解析各 ETF 行情 ---
    quote_map = {}
    for line in tc_raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var_name, val = line.split("=", 1)
        fields = val.strip('"\n\r ').split("~")
        if len(fields) < 30:
            continue
        code = fields[2]
        quote_map[code] = fields

    def parse_etf(item, is_sector=False, sf=None):
        code = item["code"]
        name = item["name"]
        fee = item.get("fee", "0.65%")
        market = "SH" if item["symbol"].startswith("sh") else "SZ"
        index_name = item.get("index_name", "纳斯达克100")

        fields = quote_map.get(code)
        price = None
        change = None
        change_pct = None
        amount_wan = 0.0
        amount_str = "--"
        volume_hand = 0
        iopv = None
        iopv_premium = None
        nav_t1 = None
        open_price = None
        high_price = None
        low_price = None
        trade_time = ""

        if fields:
            try:
                # 价格解析：只有价格为有限正数才有效，零成交量不阻断有效价格
                if len(fields) > 3 and fields[3]:
                    p = float(fields[3])
                    if math.isfinite(p) and p > 0:
                        price = round(p, 3)

                if len(fields) > 5 and fields[5]:
                    op = float(fields[5])
                    if math.isfinite(op) and op > 0:
                        open_price = round(op, 3)

                if len(fields) > 33 and fields[33]:
                    hp = float(fields[33])
                    if math.isfinite(hp) and hp > 0:
                        high_price = round(hp, 3)

                if len(fields) > 34 and fields[34]:
                    lp = float(fields[34])
                    if math.isfinite(lp) and lp > 0:
                        low_price = round(lp, 3)

                if len(fields) > 30 and fields[30]:
                    trade_time = str(fields[30]).strip()

                if len(fields) > 31 and fields[31]:
                    c = float(fields[31])
                    if math.isfinite(c):
                        change = round(c, 3)

                if len(fields) > 32 and fields[32]:
                    cp = float(fields[32])
                    if math.isfinite(cp):
                        change_pct = round(cp, 2)

                if len(fields) > 37 and fields[37]:
                    am = float(fields[37])
                    if math.isfinite(am) and am >= 0:
                        amount_wan = round(am, 1)
                        amount_str = f"{amount_wan / 10000:.2f} 亿" if amount_wan >= 10000 else f"{amount_wan:.0f} 万"

                if len(fields) > 36 and fields[36]:
                    vh = int(fields[36])
                    if vh >= 0:
                        volume_hand = vh

                # 官方公布单位净值 (NAV)：取消用 IOPV 隐式替代
                if len(fields) > 81 and fields[81]:
                    n = float(fields[81])
                    if math.isfinite(n) and n > 0:
                        nav_t1 = round(n, 4)

                # 交易所官方 IOPV 与官方溢价（仅作为参考对照字段）
                if len(fields) > 78 and fields[78]:
                    io = float(fields[78])
                    if math.isfinite(io) and io > 0:
                        iopv = round(io, 4)

                if len(fields) > 77 and fields[77]:
                    iop = float(fields[77])
                    if math.isfinite(iop):
                        iopv_premium = round(iop, 2)
            except Exception as e:
                print(f"[Error] Parsing quote fields for {code}: {e}", file=sys.stderr)

        # 估值状态与计算分流（券商官方 IOPV 口径）
        # 1. 现价缺失
        if price is None or price <= 0:
            val_status = "missing_quote"
            status_desc = "无有效场内现价"
            est_realtime = iopv if (iopv and iopv > 0) else nav_t1
            est_premium = None
        # 2. 官方 IOPV 有效（优先对齐券商交易盘中口径）
        elif iopv is not None and iopv > 0:
            val_status = "official_iopv"
            status_desc = "交易所官方IOPV估值"
            est_realtime = iopv
            # 若接口已自带 iopv_premium 则直接采用，否则由现价与 iopv 计算
            if iopv_premium is not None and math.isfinite(iopv_premium):
                est_premium = iopv_premium
            else:
                est_premium = round(((price - iopv) / iopv) * 100.0, 2)

        # 3. IOPV 缺失但官方公布净值有效（非交易时段或未计算 IOPV 时的回退）
        elif nav_t1 is not None and nav_t1 > 0:
            val_status = "nav_fallback"
            status_desc = "基准公布净值估算(IOPV未发布)"
            est_realtime = nav_t1
            est_premium = round(((price - nav_t1) / nav_t1) * 100.0, 2)
        # 4. 数据缺失
        else:
            val_status = "missing_nav"
            status_desc = "官方IOPV与净值数据缺失"
            est_realtime = None
            est_premium = None

        result = {
            "code": code,
            "name": name,
            "market": market,
            "fee": fee,
            "index_name": index_name,
            "val_status": val_status,
            "status_desc": status_desc,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "nav_t1": nav_t1,
            "iopv": iopv,
            "iopv_premium": iopv_premium,
            "est_realtime": est_realtime,
            "est_premium": est_premium,
            "amount_wan": amount_wan,
            "amount_str": amount_str,
            "volume_hand": volume_hand,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "trade_time": trade_time,
        }
        if is_sector:
            result["note"] = item.get("note", "细分行业标的")
        return result

    def sort_etfs_and_calc_stats(etf_list):
        """沉底排序并严格排除无效值计算统计指标（合法 0.0% 溢价予以保留）"""
        valid = [x for x in etf_list if x.get("est_premium") is not None]
        invalid = [x for x in etf_list if x.get("est_premium") is None]
        # 有效项升序，无效项沉底在后
        valid.sort(key=lambda x: x["est_premium"])
        sorted_list = valid + invalid

        if valid:
            avg_p = round(sum(x["est_premium"] for x in valid) / len(valid), 2)
            min_etf = valid[0]
            max_etf = valid[-1]
        else:
            avg_p = None
            min_etf = None
            max_etf = None

        return sorted_list, avg_p, min_etf, max_etf

    # 解析纳指 100 宽基标的
    nasdaq100_list, avg_premium, min_premium_etf, max_premium_etf = sort_etfs_and_calc_stats(
        [parse_etf(item, sf=scale_factor) for item in active_broad_etfs]
    )

    # 解析标普 500 宽基标的
    sp500_list, sp500_avg_premium, sp500_min_premium_etf, sp500_max_premium_etf = sort_etfs_and_calc_stats(
        [parse_etf(item, sf=sp500_scale_factor) for item in active_sp500_etfs]
    )

    # 解析细分行业标的（采用官方 IOPV 估值与溢价口径）
    sector_list, sector_avg_premium, _, _ = sort_etfs_and_calc_stats(
        [parse_etf(item, is_sector=True, sf=None) for item in active_sector_etfs]
    )

    payload = {
        "code": 200,
        "message": "success",
        "version": APP_VERSION,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "registry_info": {
            "status": registry["status"],
            "status_desc": registry["status_desc"],
            "cached_date": registry["cached_date"],
            "updated_at": registry["updated_at"],
            "last_log": registry.get("last_log", ""),
            "broad_count": len(nasdaq100_list),
            "sector_count": len(sector_list),
            "sp500_count": len(sp500_list),
        },
        "macro": {
            "cme_nq": cme_nq,
            "cme_es": cme_es,
            "usd_cnh": usd_cnh,
            "ndx_index": ndx_index,
            "spx_index": spx_index,
            "scale_factor": round(scale_factor, 5) if scale_factor is not None else None,
            "sp500_scale_factor": round(sp500_scale_factor, 5) if sp500_scale_factor is not None else None,
            "avg_premium": avg_premium,
            "sp500_avg_premium": sp500_avg_premium,
            "total_count": len(nasdaq100_list),
            "sp500_count": len(sp500_list),
            "min_premium_etf": min_premium_etf,
            "max_premium_etf": max_premium_etf,
            "sp500_min_premium_etf": sp500_min_premium_etf,
            "sp500_max_premium_etf": sp500_max_premium_etf,
        },
        "nasdaq100_etfs": nasdaq100_list,
        "nasdaq_tech_etfs": sector_list,
        "sector_etfs": sector_list,
        "sp500_etfs": sp500_list,
    }

    cache_data["timestamp"] = now
    cache_data["payload"] = payload

    # 静默沉淀收盘数据（仅在收盘后且今日未归档时触发，零额外开销）
    try:
        record_daily_history(payload)
    except Exception as e:
        print(f"[Error] record_daily_history error: {e}", file=sys.stderr)

    return payload


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

    # 2. 读取现有历史记录
    history_data = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history_data = json.load(f)
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
        temp_file = HISTORY_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, HISTORY_FILE)
        print(f"[{time.strftime('%H:%M:%S')}] 💾 [收盘归档] 已成功沉淀 {trade_date} 收盘历史数据至 {os.path.basename(HISTORY_FILE)}", flush=True)
    except Exception as e:
        print(f"[Error] Failed to write history file: {e}", file=sys.stderr)


def daily_collector_daemon():
    """
    后台自主收盘数据采集守护线程
    - 针对云主机/服务器常驻运行场景
    - 巡检周期：每 60 秒检查一次
    - 触发窗口：工作日 15:02 ~ 15:30（收盘后且各交易所行情已完全固化）
    - 幂等性：若今日已沉淀，绝不重复抓取
    """
    while True:
        try:
            now_lt = time.localtime()
            # 周一至周五工作日 (tm_wday: 0=周一 ... 4=周五)
            if now_lt.tm_wday < 5:
                # 下午 15:02 到 15:30 之间
                if now_lt.tm_hour == 15 and 2 <= now_lt.tm_min <= 30:
                    today_str = time.strftime("%Y-%m-%d", now_lt)

                    already_archived = False
                    if os.path.exists(HISTORY_FILE):
                        try:
                            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                                hdata = json.load(f)
                                if today_str in hdata:
                                    already_archived = True
                        except Exception:
                            pass

                    if not already_archived:
                        print(f"[{time.strftime('%H:%M:%S')}] ⏰ [Daemon] 检测到工作日已收盘且今日尚未归档，自主触发收盘采集...", flush=True)
                        cache_data["payload"] = None  # 穿透缓存，强制拉取最新收盘数据
                        get_market_data()
        except Exception as e:
            print(f"[Daemon Error] {e}", file=sys.stderr)

        time.sleep(60)


def start_daily_collector_daemon():
    """启动后台守护线程"""
    t = threading.Thread(target=daily_collector_daemon, daemon=True, name="DailyCollectorDaemon")
    t.start()
    print("🤖 [Daemon] 每日收盘自主采集守护线程已就绪 (工作日 15:02 自动巡检)")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """支持多线程并发请求的 HTTP 服务器"""
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    """请求处理器"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        req_path = parsed.path
        current_dir = os.path.dirname(os.path.abspath(__file__))

        # 路由 1: 实时行情接口
        if req_path == "/api/quotes":
            data = get_market_data()
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 路由 1.1: 独立的恐惧贪婪指数接口
        if req_path in ["/api/fear-greed", "/api/fear-greed/"]:
            status_code, data = get_fear_greed_data()
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 路由 2: 手动强制在线重新拉取标的池
        if req_path in ["/api/refresh-symbols", "/api/refresh-symbols/"]:
            cache_data["payload"] = None
            reg = discover_etfs_online(force=True)
            res = {
                "code": 200,
                "version": APP_VERSION,
                "message": reg.get("last_log", "标的池刷新完成"),
                "log": reg.get("last_log", "标的池刷新完成"),
                "status": reg["status"],
                "status_desc": reg["status_desc"],
                "cached_date": reg["cached_date"],
                "updated_at": reg["updated_at"],
                "broad_count": len(reg["broad_etfs"]),
                "sector_count": len(reg["sector_etfs"]),
                "sp500_count": len(reg.get("sp500_etfs", [])),
                "broad_etfs": reg["broad_etfs"],
                "sector_etfs": reg["sector_etfs"],
                "sp500_etfs": reg.get("sp500_etfs", [])
            }
            content = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 路由 3: 每日历史收盘归档查询接口
        if req_path in ["/api/history", "/api/history/"]:
            # 支持可选参数 days，如 /api/history?days=30
            days_limit = None
            if parsed.query:
                for param in parsed.query.split("&"):
                    if param.startswith("days="):
                        try:
                            days_limit = int(param.split("=")[1])
                        except Exception:
                            pass

            history_data = {}
            if os.path.exists(HISTORY_FILE):
                try:
                    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                        history_data = json.load(f)
                except Exception as e:
                    print(f"[Error] Reading history file: {e}", file=sys.stderr)

            # 按日期倒序排列（最新在前）
            sorted_dates = sorted(history_data.keys(), reverse=True)
            if days_limit and days_limit > 0:
                sorted_dates = sorted_dates[:days_limit]

            filtered_history = {d: history_data[d] for d in sorted_dates}

            res = {
                "code": 200,
                "version": APP_VERSION,
                "total_days": len(filtered_history),
                "data": filtered_history
            }
            content = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 路由 3.1: 导出下载历史数据文件
        if req_path in ["/api/history/download", "/api/history/download/"]:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="crossetf_history_daily.json"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "History file not found")
                return

        # 路由 4: 健康检查
        if req_path == "/health":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 路由 4: 静态页面（根路径或 index.html）
        html_file = os.path.join(current_dir, "static", "index.html")
        if req_path in ["/", "/index.html"]:
            if os.path.exists(html_file):
                with open(html_file, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "index.html not found")
                return

        # 路由 5: 网页图标支持（直接映射 favicon.ico / favicon.svg / favicon.png）
        if req_path in ["/favicon.ico", "/favicon.png", "/favicon.svg"]:
            fav_png = os.path.join(current_dir, "static", "favicon.png")
            fav_svg = os.path.join(current_dir, "static", "favicon.svg")
            if (req_path == "/favicon.png" or req_path == "/favicon.ico") and os.path.exists(fav_png):
                with open(fav_png, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            elif os.path.exists(fav_svg):
                with open(fav_svg, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        # 其它静态资源（基于 req_path）
        safe_rel = req_path.lstrip("/")
        safe_path = os.path.normpath(os.path.join(current_dir, "static", safe_rel))
        if safe_path.startswith(os.path.join(current_dir, "static")) and os.path.exists(safe_path) and os.path.isfile(safe_path):
            content_type = "application/octet-stream"
            if safe_path.endswith(".html"): content_type = "text/html; charset=utf-8"
            elif safe_path.endswith(".js"): content_type = "application/javascript; charset=utf-8"
            elif safe_path.endswith(".css"): content_type = "text/css; charset=utf-8"
            elif safe_path.endswith(".json"): content_type = "application/json; charset=utf-8"
            elif safe_path.endswith(".png"): content_type = "image/png"
            elif safe_path.endswith(".jpg") or safe_path.endswith(".jpeg"): content_type = "image/jpeg"
            elif safe_path.endswith(".svg"): content_type = "image/svg+xml"

            with open(safe_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        # 简洁打印日志
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()


def run_server():
    ensure_data_storage()
    print("🔍 正在在线检测并同步 ETF 标的池...")
    discover_etfs_online()
    start_daily_collector_daemon()
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadedHTTPServer(server_address, DashboardHandler)
    print("=" * 60)
    print(f"🚀 CrossETF 跨境ETF实时溢价追踪服务已启动！[{APP_VERSION}]")
    print(f"👉 本地访问地址: http://127.0.0.1:{PORT}")
    print(f"👉 实时行情 API: http://127.0.0.1:{PORT}/api/quotes")
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
