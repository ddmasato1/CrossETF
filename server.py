#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股纳指ETF追踪 - 实时溢价率监控服务
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

# 基础配置
APP_VERSION = "v1.2.0"
PORT = int(os.environ.get("PORT", 8000))
CACHE_TTL = 3  # 行情缓存时间（秒），防止高频刷新打爆上游接口

# 缓存文件路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SYMBOLS_CACHE_FILE = os.path.join(CURRENT_DIR, "nasdaq_symbols_cache.json")

# 出厂内置保底名单（仅在首次启动无网络且本地没有任何历史缓存文件时极罕见触发）
DEFAULT_BROAD_ETFS = [
    {"code": "513390", "name": "纳指100ETF博时", "symbol": "sh513390", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513110", "name": "纳指ETF华泰柏瑞", "symbol": "sh513110", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159513", "name": "纳斯达克100ETF大成", "symbol": "sz159513", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159632", "name": "纳斯达克ETF华安", "symbol": "sz159632", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159659", "name": "纳斯达克100ETF招商", "symbol": "sz159659", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159660", "name": "纳指ETF汇添富", "symbol": "sz159660", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513870", "name": "纳指ETF富国", "symbol": "sh513870", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513300", "name": "纳斯达克ETF华夏", "symbol": "sh513300", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159696", "name": "纳指ETF易方达", "symbol": "sz159696", "fee": "0.65%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159941", "name": "纳指ETF广发", "symbol": "sz159941", "fee": "0.80%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "159501", "name": "纳指ETF嘉实", "symbol": "sz159501", "fee": "0.60%", "index_name": "纳斯达克100指数(^NDX)"},
    {"code": "513100", "name": "纳指ETF国泰", "symbol": "sh513100", "fee": "0.80%", "index_name": "纳斯达克100指数(^NDX)"},
]

DEFAULT_SECTOR_ETFS = [
    {
        "code": "159509",
        "name": "纳指科技ETF景顺",
        "symbol": "sz159509",
        "fee": "0.80%",
        "index_name": "纳斯达克科技市值加权指数(NDXT)",
        "note": "跟踪科技细分指数，非纳指100宽基"
    },
    {
        "code": "513290",
        "name": "纳指生物科技ETF汇添富",
        "symbol": "sh513290",
        "fee": "0.80%",
        "index_name": "纳斯达克生物科技指数(NBI)",
        "note": "跟踪生物科技细分指数，非纳指100宽基"
    }
]

DEFAULT_SP500_ETFS = [
    {"code": "513500", "name": "标普500ETF博时", "symbol": "sh513500", "fee": "0.60%", "index_name": "标普500指数(^SPX)"},
    {"code": "159612", "name": "标普500ETF国泰", "symbol": "sz159612", "fee": "0.60%", "index_name": "标普500指数(^SPX)"},
    {"code": "159655", "name": "标普500ETF华夏", "symbol": "sz159655", "fee": "0.60%", "index_name": "标普500指数(^SPX)"},
    {"code": "513650", "name": "标普500ETF南方", "symbol": "sh513650", "fee": "0.60%", "index_name": "标普500指数(^SPX)"},
]

# 行情内存缓存
cache_data = {
    "timestamp": 0,
    "payload": None
}

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

                fee = "0.65%"
                if code == "159501":
                    fee = "0.60%"
                elif code in ["159941", "513100", "159509", "513290"]:
                    fee = "0.80%"

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
                    fee = "0.60%"
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

        if fields:
            try:
                # 价格解析：只有价格为有限正数才有效，零成交量不阻断有效价格
                if len(fields) > 3 and fields[3]:
                    p = float(fields[3])
                    if math.isfinite(p) and p > 0:
                        price = round(p, 3)

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
            premium_level = "none"
            premium_advice = "--"
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

            # 评级建议（按官方 IOPV 溢价率评定，涵盖行业标的）
            if est_premium < 3.0:
                premium_level = "safe"
                premium_advice = "极低"
            elif est_premium < 6.0:
                premium_level = "low"
                premium_advice = "温和"
            elif est_premium < 10.0:
                premium_level = "warning"
                premium_advice = "偏高"
            elif est_premium < 15.0:
                premium_level = "danger"
                premium_advice = "高危"
            else:
                premium_level = "bubble"
                premium_advice = "泡沫"
        # 3. IOPV 缺失但官方公布净值有效（非交易时段或未计算 IOPV 时的回退）
        elif nav_t1 is not None and nav_t1 > 0:
            val_status = "nav_fallback"
            status_desc = "基准公布净值估算(IOPV未发布)"
            est_realtime = nav_t1
            est_premium = round(((price - nav_t1) / nav_t1) * 100.0, 2)
            if est_premium < 3.0:
                premium_level = "safe"
                premium_advice = "极低"
            elif est_premium < 6.0:
                premium_level = "low"
                premium_advice = "温和"
            elif est_premium < 10.0:
                premium_level = "warning"
                premium_advice = "偏高"
            elif est_premium < 15.0:
                premium_level = "danger"
                premium_advice = "高危"
            else:
                premium_level = "bubble"
                premium_advice = "泡沫"
        # 4. 数据缺失
        else:
            val_status = "missing_nav"
            status_desc = "官方IOPV与净值数据缺失"
            est_realtime = None
            est_premium = None
            premium_level = "none"
            premium_advice = "--"

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
            "premium_level": premium_level,
            "premium_advice": premium_advice,
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
    return payload


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

        # 路由 3: 健康检查
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
    print("🔍 正在在线检测并同步纳指 ETF 标的池...")
    discover_etfs_online()
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadedHTTPServer(server_address, DashboardHandler)
    print("=" * 60)
    print(f"🚀 A股纳指与标普500 ETF追踪看板服务已启动！[{APP_VERSION}]")
    print(f"👉 本地访问地址: http://127.0.0.1:{PORT}")
    print(f"👉 实时行情 API: http://127.0.0.1:{PORT}/api/quotes")
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
