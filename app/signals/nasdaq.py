#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纳斯达克市场情绪、估值与每日定投建议；不依赖HTTP处理器。"""

import json
import math
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from app.core.config import APP_VERSION, DATA_DIR
from app.core.storage import read_json, write_json

# 情绪与估值常量配置
FEAR_GREED_URL = "https://feargreedflow.com/api/fear-greed"
FEAR_GREED_CACHE_TTL = 3600
FEAR_GREED_RETRY_TTL = 600
FEAR_GREED_TIMEOUT = 8
VALUATION_URL = "https://danjuanfunds.com/djapi/index_eva/dj"
VALUATION_CACHE_TTL = 6 * 3600

# 信号模块专属持久化缓存路径
FEAR_GREED_CACHE_FILE = os.path.join(DATA_DIR, "fear_greed_cache.json")
VALUATION_CACHE_FILE = os.path.join(DATA_DIR, "nasdaq_valuation_cache.json")
DCA_STATE_FILE = os.path.join(DATA_DIR, "nasdaq_dca_state.json")
DCA_TIMEZONE = timezone(timedelta(hours=8))
DCA_ACTIONS = {
    "increase": {"title": "加大投入", "amount_multiplier": 1.5},
    "normal": {"title": "正常投入", "amount_multiplier": 1.0},
    "stop": {"title": "停止投入", "amount_multiplier": 0.0},
}
DCA_CONFIRM_DAYS = 3

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

# 估值与行情、情绪独立；成功或失败均限制为每6小时最多一次上游请求。
valuation_cache = {"initialized": False, "payload": None,
                   "last_success_at": "", "last_success_epoch": 0.0, "last_attempt_epoch": 0.0}
valuation_lock = threading.Lock()

# 定投建议单独持久化，不使用ETF收盘档案或浏览器存储累计确认次数。
dca_lock = threading.Lock()
dca_state = None


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
    try:
        saved = read_json(FEAR_GREED_CACHE_FILE, default=None)
        if saved is None:
            return None
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
    saved = {
        "schema_version": 1,
        "last_success_epoch": last_success_epoch,
        "last_success_at": last_success_at,
        "payload": payload,
    }
    write_json(FEAR_GREED_CACHE_FILE, saved, fsync=True, ensure_ascii=False, indent=2)


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


def normalize_valuation_payload(raw):
    """只取NDX的PE-TTM及上游分位，不采纳上游高低估分类。"""
    if not isinstance(raw, dict) or raw.get("result_code") != 0:
        raise ValueError("invalid valuation response")
    items = (raw.get("data") or {}).get("items")
    if not isinstance(items, list):
        raise ValueError("missing valuation items")
    item = next((x for x in items if isinstance(x, dict) and x.get("index_code") == "NDX"), None)
    if item is None:
        raise ValueError("NDX valuation missing")
    values = [item.get(k) for k in ("pe", "pe_percentile", "ts")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("invalid valuation numbers")
    pe, percentile, timestamp_ms = values
    if pe <= 0 or not 0 <= percentile <= 1 or timestamp_ms <= 0:
        raise ValueError("valuation out of range")
    data_date = datetime.fromtimestamp(timestamp_ms / 1000, timezone(timedelta(hours=8))).date()
    if data_date > datetime.now(timezone(timedelta(hours=8))).date():
        raise ValueError("future valuation date")
    return {"source": "danjuan", "symbol": "NDX", "metric": "pe_ttm", "pe": pe,
            "pe_percentile": round(percentile * 100, 4), "data_date": data_date.isoformat()}


def _valid_valuation_cache(payload):
    if not isinstance(payload, dict) or (payload.get("source"), payload.get("symbol"), payload.get("metric")) != ("danjuan", "NDX", "pe_ttm"):
        return False
    pe, percentile = payload.get("pe"), payload.get("pe_percentile")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (pe, percentile)):
        return False
    try:
        data_date = datetime.strptime(payload["data_date"], "%Y-%m-%d").date()
    except (KeyError, TypeError, ValueError):
        return False
    return pe > 0 and 0 <= percentile <= 100 and data_date <= datetime.now(timezone(timedelta(hours=8))).date()


def fetch_valuation_upstream():
    request = urllib.request.Request(VALUATION_URL, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Referer": "https://danjuanfunds.com/"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read(2_000_000).decode("utf-8"))


def _save_valuation_cache():
    """保存最后有效快照与尝试时间；失败冷却在进程重启后也有效。"""
    payload = {k: v for k, v in valuation_cache.items() if k != "initialized"}
    write_json(VALUATION_CACHE_FILE, payload, fsync=True, ensure_ascii=False, allow_nan=False)


def get_valuation_data(now=None):
    """独立只读缓存；调用方无法强制刷新或指定任意上游URL。"""
    now = time.time() if now is None else float(now)
    with valuation_lock:
        if not valuation_cache["initialized"]:
            try:
                saved = read_json(VALUATION_CACHE_FILE)
                attempt = saved["last_attempt_epoch"]
                if isinstance(attempt, bool) or not isinstance(attempt, (int, float)) or not math.isfinite(attempt) or not 0 < attempt <= now:
                    raise ValueError("invalid cached attempt time")
                payload = saved.get("payload")
                success = saved.get("last_success_epoch", 0.0)
                if isinstance(success, bool) or not isinstance(success, (int, float)) or not math.isfinite(success) or not 0 <= success <= attempt:
                    raise ValueError("invalid cached success time")
                if payload is not None and (not _valid_valuation_cache(payload) or not _parse_iso_datetime(saved.get("last_success_at"))):
                    raise ValueError("invalid cached valuation")
                if payload is not None and success <= 0:
                    raise ValueError("missing cached success time")
                valuation_cache.update({"payload": payload, "last_attempt_epoch": attempt,
                                        "last_success_at": saved.get("last_success_at", ""),
                                        "last_success_epoch": success})
            except FileNotFoundError:
                pass
            except Exception as exc:
                print(f"[Warning] 忽略估值缓存: {exc}", file=sys.stderr)
            valuation_cache["initialized"] = True

        attempt = valuation_cache["last_attempt_epoch"]
        if not attempt or now - attempt >= VALUATION_CACHE_TTL or now < attempt:
            valuation_cache["last_attempt_epoch"] = now
            try:
                payload = normalize_valuation_payload(fetch_valuation_upstream())
                if valuation_cache["payload"] and payload["data_date"] < valuation_cache["payload"]["data_date"]:
                    raise ValueError("valuation date moved backwards")
                valuation_cache.update({"payload": payload,
                    "last_success_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                    "last_success_epoch": now})
            except Exception as exc:
                print(f"[Warning] 估值拉取失败: {exc}", file=sys.stderr)
            try:
                _save_valuation_cache()
            except Exception as exc:
                print(f"[Warning] 保存估值缓存失败: {exc}", file=sys.stderr)

        payload = valuation_cache["payload"]
        if payload is None:
            return 503, {"code": 503, "message": "valuation unavailable",
                         "data": {"valid": False, "status": "unavailable"}}
        data_old = (datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date() -
                    datetime.strptime(payload["data_date"], "%Y-%m-%d").date()).days > 7
        failed = valuation_cache["last_success_epoch"] < valuation_cache["last_attempt_epoch"]
        stale = failed or data_old
        return 200, {"code": 200, "message": "success", "data": {
            **payload, "valid": True, "stale": stale, "data_old": data_old,
            "status": "stale" if stale else "fresh", "last_success_at": valuation_cache["last_success_at"]}}


def _empty_dca_state():
    return {"schema_version": 1, "action": "normal", "confirmed": False,
            "candidate_action": "", "candidate_count": 0, "last_evaluation_date": "",
            "last_valid_at": "", "snapshot": None}


def _load_dca_state(today):
    try:
        saved = read_json(DCA_STATE_FILE)
        if (saved.get("schema_version") != 1 or saved.get("action") not in DCA_ACTIONS or
                type(saved.get("confirmed")) is not bool or
                saved.get("candidate_action") not in ("", *DCA_ACTIONS) or
                type(saved.get("candidate_count")) is not int or
                not 0 <= saved["candidate_count"] < DCA_CONFIRM_DAYS):
            raise ValueError("invalid DCA state")
        if bool(saved["candidate_action"]) != bool(saved["candidate_count"]):
            raise ValueError("invalid DCA confirmation count")
        for key in ("last_evaluation_date", "last_valid_at"):
            if not isinstance(saved.get(key), str):
                raise ValueError("invalid DCA date")
            if saved[key] and datetime.fromisoformat(saved[key]).date() > today:
                raise ValueError("DCA state is ahead of current date")
        snapshot = saved.get("snapshot")
        if saved["last_valid_at"]:
            if not isinstance(snapshot, dict) or not saved["last_evaluation_date"]:
                raise ValueError("missing DCA snapshot")
            for key in ("pe_percentile", "emotion"):
                value = snapshot.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                    raise ValueError("invalid DCA snapshot")
        elif saved["confirmed"] or saved["candidate_count"] or saved["action"] != "normal":
            raise ValueError("missing DCA evaluation")
        return {key: saved[key] for key in _empty_dca_state()}
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[Warning] 忽略定投建议状态: {exc}", file=sys.stderr)
    return _empty_dca_state()


def _save_dca_state(state):
    write_json(DCA_STATE_FILE, state, fsync=True, ensure_ascii=False, allow_nan=False, indent=2)


def _dca_inputs_valid(valuation, sentiment, now):
    if not valuation.get("valid") or not sentiment.get("valid"):
        return False
    for value in (valuation.get("pe_percentile"), sentiment.get("score")):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            return False
    try:
        valuation_date = datetime.strptime(valuation["data_date"], "%Y-%m-%d").date()
        sentiment_time = datetime.fromisoformat(sentiment["as_of"].replace("Z", "+00:00"))
        if sentiment_time.tzinfo is None:
            sentiment_time = sentiment_time.replace(tzinfo=timezone.utc)
        return (0 <= (now.date() - valuation_date).days <= 7 and
                0 <= (now - sentiment_time).total_seconds() <= 72 * 3600)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _dca_target_action(action, percentile, emotion):
    if action == "increase":
        return "normal" if percentile > 40 or emotion > 45 else "increase"
    if action == "stop":
        return "normal" if percentile < 80 or emotion < 65 else "stop"
    if percentile >= 85 and emotion >= 75:
        return "stop"
    if (percentile <= 30 and emotion <= 25) or (percentile <= 15 and emotion <= 45):
        return "increase"
    return "normal"


def _dca_reason(action, snapshot):
    if not snapshot:
        return "按原定计划执行，等待每日信号确认。"
    percentile, emotion = snapshot["pe_percentile"], snapshot["emotion"]
    if action == "increase":
        return "估值与情绪支持适度加码；仅使用预先准备的备用预算，分批投入。"
    if action == "stop":
        return "过热信号已确认，暂缓本期新增资金，等待退出条件确认。"
    if percentile >= 85 and emotion <= 25:
        return "估值偏高、情绪低迷；坚持原计划，暂不加码。"
    if percentile > 30 and emotion <= 25:
        return "市场情绪恐惧，但估值尚不便宜；坚持定投，暂不加码。"
    if emotion >= 75:
        return "市场情绪偏热；按原计划投入，避免追涨加码。"
    return "尚未形成需要调整投入的确认信号，按原计划执行。"


def get_nasdaq_dca_data(now=None):
    """工作日北京时间16:00后每日一次评估，刷新与重启均不重复累计。"""
    global dca_state
    epoch = time.time() if now is None else float(now)
    local_now = datetime.fromtimestamp(epoch, DCA_TIMEZONE)
    today = local_now.date()
    _, valuation_result = get_valuation_data(epoch)
    _, sentiment_result = get_fear_greed_data(epoch)
    valuation = valuation_result.get("data") or {}
    sentiment = sentiment_result.get("data") or {}
    valid = _dca_inputs_valid(valuation, sentiment, local_now)
    storage_error = False
    with dca_lock:
        if dca_state is None:
            dca_state = _load_dca_state(today)
        previous_date = dca_state["last_evaluation_date"]
        if local_now.weekday() < 5 and local_now.hour >= 16 and previous_date < today.isoformat():
            updated = dict(dca_state)
            expected_previous = today - timedelta(days=1)
            while expected_previous.weekday() >= 5:
                expected_previous -= timedelta(days=1)
            # 离线漏评或无效数据中断连续确认，不补写历史评估。
            if previous_date != expected_previous.isoformat() or not valid:
                updated.update(candidate_action="", candidate_count=0)
            updated["last_evaluation_date"] = today.isoformat()
            if valid:
                percentile, emotion = valuation["pe_percentile"], sentiment["score"]
                target = _dca_target_action(updated["action"], percentile, emotion)
                if target == updated["action"] and updated["confirmed"]:
                    updated.update(candidate_action="", candidate_count=0)
                else:
                    count = updated["candidate_count"] + 1 if updated["candidate_action"] == target else 1
                    if count >= DCA_CONFIRM_DAYS:
                        updated.update(action=target, confirmed=True, candidate_action="", candidate_count=0)
                    else:
                        updated.update(candidate_action=target, candidate_count=count)
                updated["last_valid_at"] = local_now.isoformat(timespec="seconds")
                updated["snapshot"] = {"pe_percentile": percentile, "emotion": emotion,
                                       "valuation_date": valuation["data_date"], "sentiment_as_of": sentiment["as_of"]}
            try:
                _save_dca_state(updated)
                dca_state = updated
            except Exception as exc:
                storage_error = True
                print(f"[Warning] 保存定投建议失败，本次不推进状态: {exc}", file=sys.stderr)
        state = dict(dca_state)
    return {"code": 200, "data": {
        **state, **DCA_ACTIONS[state["action"]], "valid": valid and not storage_error,
        "reason": _dca_reason(state["action"], state["snapshot"]),
        "consider_take_profit": state["confirmed"] and state["action"] == "stop",
        "evaluation_schedule": "工作日16:00（北京时间）", "confirmation_days": DCA_CONFIRM_DAYS,
        "unavailable_reason": "建议暂存失败，本次不更新" if storage_error else "数据不足或过期，暂不更新建议",
    }}


def dca_evaluator_daemon():
    while True:
        try:
            now = datetime.now(DCA_TIMEZONE)
            if now.weekday() < 5 and now.hour >= 16:
                get_nasdaq_dca_data()
        except Exception as exc:
            print(f"[DCA Daemon Error] {exc}", file=sys.stderr)
        time.sleep(60)
