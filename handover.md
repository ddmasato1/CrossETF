# CrossETF 项目交接与上下文备忘录 (handover.md)

> 💡 **致接手的 AI 助手 (Agent Protocol)**：
> 本项目存储于 iCloud 中，代码跨设备无缝同步，但会话上下文不互通。**本文件是你在新设备/新会话接手本项目时的唯一记忆中枢**。
> **维护协议**：若在本次会话中产生了**重大架构调整、核心业务决策、关键避坑结论或新版本发布**，请在提请用户提交代码前，**主动同步精简更新本文件**，确保下一台电脑上的 Agent 能够无缝继承记忆。

---

## 1. 项目快速认知 (Quick Context)

- **项目名称**：`CrossETF`（曾用名 NasDashboard，已正式更名）。
  - *命名内涵*：**Cross** 代表“立足境内 A 股、追踪跨境（Cross-Border）美股”，**绝非 NAS 网络存储主机**！
- **当前最新版本**：`v1.3.0`（Git 分支 `main`）。
- **远程仓库**：[https://github.com/ddmasato1/CrossETF](https://github.com/ddmasato1/CrossETF)
- **核心定位**：极简、高信噪比的 A 股跨境 ETF 实时折溢价量化监控与收盘档案沉淀服务。专为云主机与本地 7×24 小时常驻运行打造。

---

## 2. 核心业务口径与指标模型 (Business Domain)

1. **估值与溢价计算口径（100% 对齐券商交易端）**：
   - **指标源**：采用中证指数/交易所盘中每 15 秒广播的**官方 IOPV**（基金份额参考净值）及**官方 IOPV 溢价率**（腾讯行情接口 `fields[78]` 与 `fields[77]`）。
   - **公式**：$\text{实时溢价率} = \frac{\text{场内现价} - \text{IOPV}}{\text{IOPV}} \times 100\%$。
   - **优势**：与同花顺、各大券商 App 交易终端盘中数值完全一致，消除分歧。
2. **标的池覆盖范围**：
   - **纳指 100 核心宽基**（12只）：`513390`、`513110`、`159513`、`159632`、`159659`、`159660`、`513870`、`513300`、`159696`、`159941`、`159501`、`513100`。
   - **标普 500 核心宽基**（4只）：`513500`、`159612`、`159655`、`513650`。
   - **细分行业/主题标的**：`159509`（景顺纳指科技，跟踪 NDXT）、`513290`（汇添富纳指生物科技，跟踪 NBI）。已放开估值计算与评级，独立专区展示。
3. **空值与异常沉底机制**：
   - 无论按溢价率升序还是降序排序，无现价或数据缺失的标的**必须稳定沉底**在最后，杜绝将故障标的误判为“最低溢价最优标的”。合法 `0.0%` 溢价正常保留。
4. **价差测算器原则**：
   - 仅限同指数标的比较（如纳指宽基内部）；测算金额仅为理论价差示意，不代表实际套利利润。
5. **综合运作费率口径 (Expense Ratio)**：
   - 看板展示的“费率”为基金官方**综合年化运作费率**（管理费 + 托管费，从资产每日计提）。其中 `159941`、`513110`、`159513`、`159509` 为 1.00%；标普 500 为 0.75%~0.80%；低费率宽基为 0.60%。由 `ETF_TOTAL_FEES` 权威字典统一维护。

---

## 3. 技术架构与关键文件地图 (Architecture & File Map)

- **极简设计原则（KISS）**：
  - **后端**：零第三方依赖，纯 Python 3 标准库（`http.server.ThreadingHTTPServer` + `urllib`）。
  - **前端**：单页面响应式（Vue 3 CDN + Tailwind CSS CDN），无打包编译环节，开箱即用。
  - **数据存储**：全部持久化文件统一收敛于 **`data/`** 目录。
- **关键文件清单**：
  - [`server.py`](file:///Users/aiden/Documents/CrossETF/server.py)：服务端主程序。内置标的动态发现、行情聚合、`/api/history` 查询与后台自主采集守护线程 `daily_collector_daemon`。
  - [`static/index.html`](file:///Users/aiden/Documents/CrossETF/static/index.html)：单页面前端。集成实时表格、换仓测算器、版本徽章及每日收盘历史感知抽屉。
  - [`data/`](file:///Users/aiden/Documents/CrossETF/data)：**核心持久化数据目录**。
    - `data/nasdaq_symbols_cache.json`：天天基金动态探测出的标的池本地缓存。
    - `data/history_daily.json`：每日 15:00 收盘后沉淀的全市场宏观中枢与 18 只 ETF 量价档案。
  - [`docker-compose.yml`](file:///Users/aiden/Documents/CrossETF/docker-compose.yml)：容器部署编排，挂载 `./data:/app/data`，端口 `8000:8000`。
  - [`test_review_criteria.py`](file:///Users/aiden/Documents/CrossETF/test_review_criteria.py)：7 项核心验收与幂等性回归单元测试套件。

---

## 4. 关键设计决策与避坑指南 (Crucial Decisions & Gotchas)

> ⚠️ **以下踩坑教训与决策结论至关重要，接手时切勿重蹈覆辙**：

1. **Docker 卷挂载必须挂载目录 `./data:/app/data`，切勿挂单文件**：
   - *踩坑背景*：为防止断电损坏 JSON，后端落盘采用了“写临时文件 + `os.replace` 原子替换”机制。在 Linux/Docker 环境下挂载单文件会导致其 inode 被锁定，`os.replace` 会抛出 `OSError: [Errno 16] Device or resource busy` 报错。挂载整个目录彻底解决了该隐患。
2. **后台收盘采集守护线程 (Active Daemon) 的工作机制**：
   - 服务启动时自动启动 `start_daily_collector_daemon()`。
   - 每个工作日（周一至周五）**15:02 ~ 15:30** 期间每分钟巡检一次。
   - 即使数周无人访问网页，只要后台程序在跑，系统也会自主抓取收盘行情并落盘，**绝不漏采**。单日严格只写一次（幂等）。
3. **单一数据源原则 (Single Source of Truth)**：
   - 所有持久化数据统一收敛至 `data/` 目录并直接纳入 Git 跟踪。根目录下不再保留重复的 JSON 文件，杜绝双份维护隐患。Docker 镜像构建与本地运行一律以 `data/` 为唯一源头。
4. **沙箱与外网访问**：
   - 在本助手的终端沙箱中，直接网络调用（拉取行情、`git push`）需按规则使用 `BypassSandbox: true`。

---

## 5. 用户协作铁律与行为准则 (User Rules & Constraints)

接手本项目的任何 Agent 必须时刻严格遵循用户的全局准则：

1. 🔴 **Git 推送绝对控制线**：**严禁任何形式的静默自动 `git push`**！只有在用户明确给出“提交并推送”等指示后方可执行。
2. 🟢 **KISS 原则（Keep It Simple, Stupid）**：崇尚简洁与可维护性，避免过度工程化与冗余抽象（如：不需要额外引入数据库、不无端上大型打包器）。
3. 🟢 **事实为本，拒绝臆测**：以事实为最高准则；有疑问直接坦率提出或回答不知道，绝不奉承或瞎编。
4. 🟢 **结构化工作流程**：严格遵循 **“构思方案 → 提请审核 → 分解为具体任务”** 的开发流程，未经用户审核不擅自大幅改动代码。
5. 🟢 **语言与输出规范**：所有思考过程、规划方案与正式回复一律使用**中文**。

---

## 6. 常用运维与验证指令 (Cheat Sheet)

```bash
# 本地测试启动
python3 server.py

# 运行全面回归单元测试套件（7项全通）
python3 test_review_criteria.py

# Docker 生产部署
docker compose up -d --build

# 查看运行日志
docker compose logs -f

# 查询历史归档 API
curl -s http://127.0.0.1:8000/api/history | jq .

# GitHub CLI (系统已支持 gh 命令行工具)
gh repo view
```

---

## 7. 当前状态与演进记录 (Status & Changelog)

- **当前版本**：`v1.3.0`
- **最新 Commit**：[`28dbb30`](https://github.com/ddmasato1/CrossETF/commit/28dbb30) (`fix: calibrate expense ratios and enforce single source of truth in data directory`)
- **近期规划事项**：
  - [x] 券商官方 IOPV 口径对齐与全标的覆盖
  - [x] 每日收盘宏观与个券数据自动静默沉淀
  - [x] 后台自主采集守护线程与前端历史抽屉
  - [x] 独立 `data/` 目录隔离与 Docker 原子挂载改造
  - [x] 综合运营费率校准（对齐 1.00% 梯队）与单一数据源彻底收敛
  - [ ] 后续：云主机生产部署上线，以及基于 `history_daily.json` 的极简历史均溢趋势线探索
