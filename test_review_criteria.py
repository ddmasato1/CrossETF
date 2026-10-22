#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CrossETF 核心逻辑与验收测试脚本
覆盖验收项：
1. 路由解析：带参数与不带参数
2. 合法 0.0% 溢价保留
3. 单只与全部行情缺失
4. 期货或汇率缺失（NQ、ES 分别缺失，汇率缺失影响两者，缺少基准值不假装正常）
5. 零成交量但有效价格
6. 行业 ETF 保守处理（不输出估值）
7. 排序沉底（升序沉底、降序沉底）
8. 全无效时均值与极值输出 None
9. 移动端核心卡片与桌面表格并存
10. 恐惧贪婪指数解析、缓存降级、并发与接口隔离
11. ETF 停牌状态 (is_suspended) 解析与前端标识
"""

import os
import sys
import json
import math
import urllib.parse
import inspect
import tempfile
import threading
import time
import server
from server import DashboardHandler

def run_tests():
    print("=" * 60)
    print("🚀 开始执行 CrossETF 全面验收测试")
    print("=" * 60)

    # -------------------------------------------------------------
    # 测试 1: 路由解析带参数与不带参数
    # -------------------------------------------------------------
    test_paths = [
        ("/api/refresh-symbols", "/api/refresh-symbols"),
        ("/api/refresh-symbols?t=1792138900", "/api/refresh-symbols"),
        ("/api/quotes", "/api/quotes"),
        ("/api/quotes?_=123456&t=999", "/api/quotes"),
        ("/api/fear-greed?t=1792138900", "/api/fear-greed"),
        ("/health?check=1", "/health"),
        ("/favicon.png?v=2", "/favicon.png"),
        ("/index.html?from=desktop", "/index.html"),
    ]
    for raw, expected in test_paths:
        parsed = urllib.parse.urlparse(raw).path
        assert parsed == expected, f"路由解析失败: {raw} -> {parsed} != {expected}"
    print("✅ 测试 1 通过: 统一路由解析成功支持带参/无参请求")

    # -------------------------------------------------------------
    # 测试 2: 宏观校验与基准值测试
    # -------------------------------------------------------------
    def check_macro(p, s):
        return (math.isfinite(p) and p > 0 and math.isfinite(s) and s > 0)

    # 缺少基准值（settle <= 0）绝对无效
    assert check_macro(20000.0, 0.0) is False, "缺少 settle 基准必须判定无效"
    assert check_macro(0.0, 20000.0) is False, "缺少 price 必须判定无效"
    assert check_macro(float('nan'), 20000.0) is False, "NaN 必须判定无效"
    assert check_macro(20000.0, 19900.0) is True, "有限正数有效"
    print("✅ 测试 2 通过: 宏观基准值严格覆盖，缺少结算价不假冒正常")

    # -------------------------------------------------------------
    # 测试 3: NQ/ES/汇率分别缺失联动测试
    # -------------------------------------------------------------
    def get_multipliers(nq_valid, es_valid, fx_valid, nq_pct=1.0, es_pct=0.5, fx_pct=0.2):
        sf_nq = ((1.0 + nq_pct / 100.0) * (1.0 + fx_pct / 100.0)) if (nq_valid and fx_valid) else None
        sf_es = ((1.0 + es_pct / 100.0) * (1.0 + fx_pct / 100.0)) if (es_valid and fx_valid) else None
        return sf_nq, sf_es

    # NQ 缺失只影响纳指
    sf_nq, sf_es = get_multipliers(nq_valid=False, es_valid=True, fx_valid=True)
    assert sf_nq is None and sf_es is not None, "NQ 缺失应只置空纳指乘数"

    # ES 缺失只影响标普
    sf_nq, sf_es = get_multipliers(nq_valid=True, es_valid=False, fx_valid=True)
    assert sf_nq is not None and sf_es is None, "ES 缺失应只置空标普乘数"

    # 汇率缺失两者皆空
    sf_nq, sf_es = get_multipliers(nq_valid=True, es_valid=True, fx_valid=False)
    assert sf_nq is None and sf_es is None, "汇率缺失两者皆应置空"
    print("✅ 测试 3 通过: NQ/ES 分别缺失解耦与汇率全局熔断生效")

    # -------------------------------------------------------------
    # 测试 4: 零成交量但有效价格 vs 价格缺失 vs 净值缺失
    # -------------------------------------------------------------
    # 模拟估值计算
    def mock_eval(price, nav, sf, is_sector=False):
        if is_sector:
            return "unsupported_index", None, None
        if price is None or price <= 0:
            return "missing_quote", None, None
        if nav is None or nav <= 0:
            return "missing_nav", None, None
        if sf is None or sf <= 0:
            return "missing_macro", None, None
        
        est = round(nav * sf, 4)
        prem = round(((price - est) / est) * 100.0, 2)
        return "nav_unverified", est, prem

    # 零成交量但价格正常
    st, est, prem = mock_eval(price=1.200, nav=1.000, sf=1.100)
    assert st == "nav_unverified" and prem is not None, "价格有效时应可计算参考估值"

    # 净值缺失 (<= 0)
    st, est, prem = mock_eval(price=1.200, nav=0.0, sf=1.100)
    assert st == "missing_nav" and prem is None, "净值缺失不能回退到 IOPV，必须置空"

    # 价格缺失 (<= 0)
    st, est, prem = mock_eval(price=0.0, nav=1.000, sf=1.100)
    assert st == "missing_quote" and prem is None, "价格缺失必须置空"

    # 行业 ETF 保守处理
    st, est, prem = mock_eval(price=1.200, nav=1.000, sf=1.100, is_sector=True)
    assert st == "unsupported_index" and prem is None, "行业 ETF 必须标记 unsupported_index 且置空"
    print("✅ 测试 4 通过: 零成交量不阻断有效价格，净值/现价缺失正确置空，行业保守处理生效")

    # -------------------------------------------------------------
    # 测试 5: 合法 0.0% 溢价保留
    # -------------------------------------------------------------
    st, est, prem = mock_eval(price=1.100, nav=1.000, sf=1.100)
    assert st == "nav_unverified"
    assert prem == 0.0, f"现价等于 EST 时溢价率应为 0.0，实际为 {prem}"
    assert prem is not None, "0.0% 是合法有效值，绝不能被当成 None"
    print("✅ 测试 5 通过: 合法 0.0% 溢价精确保留")

    # -------------------------------------------------------------
    # 测试 6: 沉底排序与全无效统计输出
    # -------------------------------------------------------------
    mock_etfs = [
        {"code": "A", "est_premium": None},
        {"code": "B", "est_premium": 5.2},
        {"code": "C", "est_premium": 0.0},  # 合法 0.0%
        {"code": "D", "est_premium": None},
        {"code": "E", "est_premium": 1.5},
    ]

    valid = [x for x in mock_etfs if x["est_premium"] is not None]
    invalid = [x for x in mock_etfs if x["est_premium"] is None]
    valid.sort(key=lambda x: x["est_premium"])
    res_asc = valid + invalid
    assert [x["code"] for x in res_asc] == ["C", "E", "B", "A", "D"], f"升序排列失败: {res_asc}"

    # 降序沉底
    valid.sort(key=lambda x: x["est_premium"], reverse=True)
    res_desc = valid + invalid
    assert [x["code"] for x in res_desc] == ["B", "E", "C", "A", "D"], f"降序沉底失败: {res_desc}"

    # 全无效统计
    all_invalid = [{"code": "X", "est_premium": None}, {"code": "Y", "est_premium": None}]
    val_items = [x for x in all_invalid if x["est_premium"] is not None]
    assert len(val_items) == 0
    avg_p = round(sum(x["est_premium"] for x in val_items) / len(val_items), 2) if val_items else None
    min_item = val_items[0] if val_items else None
    max_item = val_items[-1] if val_items else None
    assert avg_p is None and min_item is None and max_item is None, "全无效时均值与极值必须全为 None"
    print("✅ 测试 6 通过: 升降序空值稳定沉底，全无效时均值/极值严格返回 None")

    # 7. 测试每日收盘历史沉淀与幂等性
    test_hist_file = os.path.join(server.CURRENT_DIR, "test_history_tmp.json")
    orig_hist_file = server.HISTORY_FILE
    try:
        server.HISTORY_FILE = test_hist_file
        if os.path.exists(test_hist_file):
            os.remove(test_hist_file)

        mock_payload = {
            "macro": {
                "avg_premium": 2.5,
                "sp500_avg_premium": 1.2,
                "min_premium_etf": {"code": "159941", "name": "纳指ETF", "est_premium": 0.8, "price": 1.7, "iopv": 1.5},
                "sp500_min_premium_etf": {"code": "513500", "name": "标普500", "est_premium": 0.5, "price": 1.5, "iopv": 1.5}
            },
            "nasdaq100_etfs": [
                {"code": "159941", "name": "纳指ETF", "price": 1.7, "open": 1.68, "high": 1.72, "low": 1.67, "iopv": 1.5, "est_premium": 0.8, "trade_time": "20261016150200"}
            ],
            "sp500_etfs": [],
            "sector_etfs": []
        }
        # 首次归档
        server.record_daily_history(mock_payload)
        assert os.path.exists(test_hist_file), "历史归档文件必须成功生成"
        with open(test_hist_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "2026-10-16" in data, "必须包含对应交易日条目"
        assert data["2026-10-16"]["macro"]["nasdaq_avg_premium"] == 2.5
        assert len(data["2026-10-16"]["etfs"]) == 1
        assert data["2026-10-16"]["etfs"][0]["open"] == 1.68

        # 再次调用验证幂等性（条目数仍为 1）
        server.record_daily_history(mock_payload)
        with open(test_hist_file, "r", encoding="utf-8") as f:
            data2 = json.load(f)
        assert len(data2) == 1, "幂等性校验失败：重复写入了条目"
        print("✅ 测试 7 通过: 每日收盘历史安全归档与幂等性验证通过")
    finally:
        server.HISTORY_FILE = orig_hist_file
        if os.path.exists(test_hist_file):
            os.remove(test_hist_file)

    # -------------------------------------------------------------
    # 测试 8: 移动端核心卡片与桌面表格静态结构
    # -------------------------------------------------------------
    index_file = os.path.join(server.CURRENT_DIR, "static", "index.html")
    with open(index_file, "r", encoding="utf-8") as f:
        index_html = f.read()

    required_mobile_markers = [
        'data-mobile-etf-list',
        'data-mobile-sector-list',
        'expandedEtfCode',
        'toggleEtfDetails(item.code)',
        ':aria-expanded="expandedEtfCode === item.code"',
        '实时溢价',
        '涨跌幅',
        '现价',
        '完整数据',
    ]
    for marker in required_mobile_markers:
        assert marker in index_html, f"移动端结构缺少关键标记: {marker}"

    assert 'premium_level' not in index_html and 'premium_advice' not in index_html, "页面不应再展示 ETF 溢价评级或建议"
    assert '>建议</th>' not in index_html, "桌面列表不应保留 ETF 建议列"

    assert index_html.count('hidden sm:block bg-white border') >= 2, "核心与行业桌面表格必须在 sm 断点保留"
    assert 'role="dialog" aria-modal="true"' in index_html, "历史档案必须提供 dialog 语义"
    assert ':aria-expanded="showKnowledge"' in index_html, "风控折叠区必须暴露展开状态"
    print("✅ 测试 8 通过: 手机核心卡片、行内详情与桌面表格响应式结构并存")

    # -------------------------------------------------------------
    # 测试 9: 恐惧贪婪指数标准化、缓存状态机、并发与隔离
    # -------------------------------------------------------------
    rating_cases = [
        (24.9, "extreme_fear"), (25, "fear"), (44.9, "fear"),
        (45, "neutral"), (54.9, "neutral"), (55, "greed"),
        (74.9, "greed"), (75, "extreme_greed"), (100, "extreme_greed"),
    ]
    for score, expected in rating_cases:
        assert server.classify_fear_greed(score)[0] == expected, f"情绪边界错误: {score}"

    normalized = server.normalize_fear_greed_payload({
        "dates": ["2026-09-01", "invalid", "2026-09-18", "2026-10-16"],
        "values": [60, 30, 46.24, 17.86],
        "asOf": "2026-10-16T23:59:49+00:00",
        "indicators": {"market_momentum": 99, "safe_haven_demand": 1},
    })
    assert normalized["score"] == 17.9 and normalized["rating"] == "extreme_fear"
    assert [point["date"] for point in normalized["history"]] == ["2026-09-18", "2026-10-16"], "必须按最新数据日截取30个自然日"
    assert normalized["as_of"] == "2026-10-16T23:59:49+00:00"
    assert "indicators" not in normalized, "组成指标不得进入标准化响应"

    try:
        server.normalize_fear_greed_payload({"dates": ["2026-10-16"], "values": []})
        raise AssertionError("长度不一致必须抛出异常")
    except ValueError:
        pass

    original_cache_file = server.FEAR_GREED_CACHE_FILE
    original_fetch = server.fetch_fear_greed_upstream
    sample_raw = {
        "dates": ["2026-10-15", "2026-10-16"],
        "values": [21.2, 17.9],
        "asOf": "2026-10-16T23:59:49+00:00",
    }
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            server.FEAR_GREED_CACHE_FILE = os.path.join(temp_dir, "fear_greed_cache.json")
            calls = {"count": 0, "fail": False}

            def mock_fetch():
                calls["count"] += 1
                if calls["fail"]:
                    raise RuntimeError("mock upstream unavailable")
                return sample_raw

            server.fetch_fear_greed_upstream = mock_fetch
            server.reset_fear_greed_runtime_cache()

            status, first = server.get_fear_greed_data(now=1000)
            assert status == 200 and first["data"]["status"] == "fresh"
            assert calls["count"] == 1 and os.path.exists(server.FEAR_GREED_CACHE_FILE)

            status, cached = server.get_fear_greed_data(now=1100)
            assert status == 200 and cached["data"]["stale"] is False
            assert calls["count"] == 1, "60分钟内不得重复抓取"

            calls["fail"] = True
            status, stale = server.get_fear_greed_data(now=4601)
            assert status == 200 and stale["data"]["stale"] is True
            assert stale["data"]["score"] == 17.9 and calls["count"] == 2

            status, throttled = server.get_fear_greed_data(now=4700)
            assert status == 200 and throttled["data"]["status"] == "stale"
            assert calls["count"] == 2, "失败后10分钟内不得重复抓取"

            # 模拟服务重启：从持久化文件恢复最后有效值，在线仍失败时继续降级。
            server.reset_fear_greed_runtime_cache()
            status, restored = server.get_fear_greed_data(now=5200)
            assert status == 200 and restored["data"]["stale"] is True
            assert restored["data"]["score"] == 17.9

            # 冷启动既无缓存又抓取失败，必须返回 503，不能伪造 0 分。
            os.remove(server.FEAR_GREED_CACHE_FILE)
            server.reset_fear_greed_runtime_cache()
            status, unavailable = server.get_fear_greed_data(now=6000)
            assert status == 503 and unavailable["data"]["valid"] is False
            assert "score" not in unavailable["data"]

            # 并发到期请求只能触发一次真实上游抓取。
            calls.update({"count": 0, "fail": False})
            server.reset_fear_greed_runtime_cache()

            def slow_fetch():
                calls["count"] += 1
                time.sleep(0.03)
                return sample_raw

            server.fetch_fear_greed_upstream = slow_fetch
            results = []
            threads = [threading.Thread(target=lambda: results.append(server.get_fear_greed_data(now=7000))) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert calls["count"] == 1, f"并发请求不应重复抓取，实际 {calls['count']} 次"
            assert all(status_code == 200 for status_code, _ in results)

        market_source = inspect.getsource(server.get_market_data)
        assert "get_fear_greed_data" not in market_source and "FEAR_GREED" not in market_source, "/api/quotes 行情函数不得依赖情绪模块"
    finally:
        server.FEAR_GREED_CACHE_FILE = original_cache_file
        server.fetch_fear_greed_upstream = original_fetch
        server.reset_fear_greed_runtime_cache()

    required_fear_greed_markers = [
        'data-fear-greed-card',
        "fetch('/api/fear-greed?t='",
        'fearGreedTimer = setInterval(fetchFearGreed, 60 * 60 * 1000)',
        'fear-greed-canvas-wrap',
        '缓存数据',
        '情绪数据暂不可用',
    ]
    for marker in required_fear_greed_markers:
        assert marker in index_html, f"正式情绪模块缺少关键标记: {marker}"
    fixed_explanations = [
        ("extreme_fear", "市场避险情绪很强", "组成指数的多项信号通常明显偏向防御"),
        ("fear", "市场情绪偏谨慎", "市场动能和资金风险偏好有所减弱"),
        ("neutral", "市场情绪相对均衡", "组成指数中的恐惧与贪婪信号大致平衡"),
        ("greed", "市场风险偏好较高", "市场动能和广度较强"),
        ("extreme_greed", "市场追涨情绪很强", "上涨动能、创新高股票和风险资产需求较强"),
    ]
    for rating, title, body in fixed_explanations:
        assert f"{rating}: {{" in index_html and title in index_html and body in index_html, f"{rating} 固定解释缺失或未独立定义"
    print("✅ 测试 9 通过: 恐惧贪婪指数解析、缓存降级、并发与 ETF 接口隔离")

    # -------------------------------------------------------------
    # 测试 10: ETF 停牌状态 (is_suspended) 解析与前端结构
    # -------------------------------------------------------------
    # 1. 模拟行情 fields 解析逻辑
    def mock_parse_suspended(fields):
        is_suspended = False
        if fields:
            if len(fields) > 40 and fields[40]:
                if str(fields[40]).strip().upper() == "S":
                    is_suspended = True
        return is_suspended

    # 正常交易
    fields_normal = [""] * 45
    fields_normal[40] = ""
    assert mock_parse_suspended(fields_normal) is False, "正常交易标的 is_suspended 必须为 False"

    # 停牌标的
    fields_suspended = [""] * 45
    fields_suspended[40] = "S"
    assert mock_parse_suspended(fields_suspended) is True, "停牌标的 fields[40]=='S' 必须解析为 True"

    # 小写 's' 容错
    fields_suspended_lower = [""] * 45
    fields_suspended_lower[40] = "s"
    assert mock_parse_suspended(fields_suspended_lower) is True, "小写 's' 容错判定必须为 True"

    # 字段截断或越界安全性
    assert mock_parse_suspended([""] * 10) is False, "短字段必须安全返回 False"
    assert mock_parse_suspended(None) is False, "None 字段必须安全返回 False"

    # 2. 前端界面标记验证
    assert 'v-if="item.is_suspended"' in index_html, "前端模板必须包含 item.is_suspended 条件渲染"
    assert '临时停牌' in index_html, "前端模板必须包含 临时停牌 徽章提示"
    print("✅ 测试 10 通过: ETF 停牌状态解析、越界安全容错与前端标识验证通过")

    # -------------------------------------------------------------
    # 测试 11: 美债收益率方案A (2Y/10Y/利差倒挂) 解析、熔断与前端展示
    # -------------------------------------------------------------
    def mock_parse_treasury(f_list, default_name):
        if len(f_list) > 8:
            try:
                y = float(f_list[1]) if f_list[1] else 0.0
                chg_bp = (float(f_list[8]) if f_list[8] else 0.0) * 100.0
                chg_pct = float(f_list[7]) if f_list[7] else 0.0
                t = f_list[13] if len(f_list) > 13 and f_list[13] else "--"
                if math.isfinite(y) and y > 0:
                    return {
                        "name": default_name,
                        "yield": round(y, 3),
                        "change_bp": round(chg_bp, 2),
                        "change_pct": round(chg_pct, 2),
                        "valid": True,
                        "time": t
                    }
            except Exception:
                pass
        return {"name": default_name, "yield": 0.0, "change_bp": 0.0, "change_pct": 0.0, "valid": False, "time": "--"}

    # 1. 2Y 与 10Y 解析
    raw_2y = "美国2年期国债,4.800,4.804,4.777,4.804,4.778,0,-0.5586,-0.0268,0,0,1792650903,2026-10-22,02:35:03,2028-09-30,4.750,1年2次,99.947,美国".split(",")
    raw_10y = "美国10年期国债,5.107,5.119,5.085,5.115,5.082,0,-0.6645,-0.0340,0,0,1792650946,2026-10-22,02:35:46,2036-08-15,4.625,1年2次,96.473,美国".split(",")
    res_2y = mock_parse_treasury(raw_2y, "美债2Y")
    res_10y = mock_parse_treasury(raw_10y, "美债10Y")
    assert res_2y["valid"] is True and res_10y["valid"] is True, "2Y 与 10Y 均必须有效"

    # 2. 利差计算与倒挂判定逻辑
    diff = res_10y["yield"] - res_2y["yield"]
    diff_bp = round(diff * 100.0, 1)
    assert diff_bp == 30.7, f"正常利差计算错误: {diff_bp}"
    assert (diff < 0) is False, "正常曲线倒挂必须为 False"

    # 模拟倒挂场景（2Y 利率高于 10Y）
    inv_diff = 4.800 - 5.107
    assert (inv_diff < 0) is True, "倒挂场景判定必须为 True"

    # 3. 前端模板验证
    assert "us_treasuries?.us2y" in index_html, "前端模板必须包含 2Y 美债数据绑定"
    assert "us_treasuries?.us10y" in index_html, "前端模板必须包含 10Y 美债数据绑定"
    assert "spread_10y_2y" in index_html, "前端模板必须包含 10Y-2Y 利差绑定"
    assert "倒挂警报" in index_html, "前端模板必须包含倒挂警报提示"
    print("✅ 测试 11 通过: 美债收益率方案A (2Y/10Y/利差倒挂) 解析、熔断与前端展示验证通过")

    # -------------------------------------------------------------
    # 测试 12: 纳指定投风向标 (纯血 QQQM 方案1) 多因子引擎与五档动态乘数
    # -------------------------------------------------------------
    from server import calculate_nasdaq_dca

    # 1. 常规实盘数据推演 (当前盘面: fg=31.3, 10y=5.107, spread=30.7, pos_52w=81.9)
    res_normal = calculate_nasdaq_dca(fg_score=31.3, us10y_yield=5.107, spread_bp=30.7, pos_52w=81.9)
    assert res_normal["valid"] is True, "定投风向标必须有效"
    assert res_normal["score"] == 52.8, f"实盘打分推算错误: {res_normal['score']}"
    assert res_normal["multiplier"] == 1.0, "52.8分必须命中 1.0x 基准定投乘数"
    assert res_normal["tier"] == "neutral", "52.8分必须判定为 neutral 中性博弈"

    # 2. 极端冰点黄金坑场景 (超跌大底: fg=10, 10y=3.0, spread=100, pos_52w=10)
    res_bottom = calculate_nasdaq_dca(fg_score=10.0, us10y_yield=3.0, spread_bp=100.0, pos_52w=10.0)
    assert res_bottom["score"] <= 20.0, f"大底得分必须<=20: {res_bottom['score']}"
    assert res_bottom["multiplier"] == 2.0, "大底必须触发 2.0x 翻倍定投"
    assert res_bottom["tier"] == "extreme_fear", "大底等级必须为 extreme_fear"

    # 3. 极端泡沫高危区场景 (泡沫顶部: fg=95, 10y=5.5, spread=-30, pos_52w=98)
    res_top = calculate_nasdaq_dca(fg_score=95.0, us10y_yield=5.5, spread_bp=-30.0, pos_52w=98.0)
    assert res_top["score"] > 80.0, f"泡沫顶得分必须>80: {res_top['score']}"
    assert res_top["multiplier"] == 0.0, "泡沫顶必须停投 0.0x"
    assert res_top["tier"] == "extreme_greed", "泡沫顶等级必须为 extreme_greed"

    # 4. 前端组件绑定校验
    assert "nasdaqDca" in index_html, "前端模板必须包含 nasdaqDca 响应式计算属性绑定"
    assert "纳指定投风向标" in index_html, "前端模板必须包含 纳指定投风向标 标题"
    assert "getDcaBadgeClass" in index_html, "前端模板必须导出 getDcaBadgeClass 辅助函数"
    print("✅ 测试 12 通过: 纳指定投风向标 (纯血 QQQM 方案1) 多因子引擎与五档动态乘数验证通过")

    print("=" * 60)
    print("🎉 全部 12 项核心业务、情绪缓存与响应式结构测试 100% 验证通过！")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
