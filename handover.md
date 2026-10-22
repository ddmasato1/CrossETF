# CrossETF 项目交接与上下文备忘录 (handover.md)

> 💡 **致接手的 AI 助手 (Agent Protocol)**：
> 本项目存储于 iCloud 中，代码跨设备无缝同步，但会话上下文不互通。**本文件是你在新设备/新会话接手本项目时的唯一记忆中枢**。
> **维护协议**：若在本次会话中产生了**重大架构调整、核心业务决策、关键避坑结论或新版本发布**，请在提请用户提交代码前，**主动同步精简更新本文件**，确保下一台电脑上的 Agent 能够无缝继承记忆。

---

## 1. 项目快速认知 (Quick Context)

- **项目名称**：`CrossETF`（曾用名 NasDashboard，已正式更名）。
  - *命名内涵*：**Cross** 代表“立足境内 A 股、追踪跨境（Cross-Border）美股”，**绝非 NAS 网络存储主机**！
- **当前代码版本**：`v1.5.5`（分支 `main`）；最新 Git 标签仍为 `v1.4.0`，本次发布不创建标签。
- **远程仓库**：[https://github.com/ddmasato1/CrossETF](https://github.com/ddmasato1/CrossETF)
- **核心定位**：极简、高信噪比的 A 股跨境 ETF 实时折溢价量化监控与收盘档案沉淀服务。专为云主机与本地 7×24 小时常驻运行打造。

---

## 2. 核心业务口径与指标模型 (Business Domain)

1. **估值与溢价计算口径（官方 IOPV 优先）**：
   - **指标源**：腾讯行情 `fields[78]` 为 IOPV，`fields[77]` 为 IOPV 溢价率，`fields[81]` 为公布净值。
   - **优先级**：现价与 IOPV 有效时，优先使用接口溢价率；缺失时按 `(现价 - IOPV) / IOPV × 100%` 计算。IOPV 缺失而公布净值有效时，以公布净值计算并标记 `nav_fallback`；无有效现价或无可用估值基准时溢价为空。
   - **宏观边界**：期货与汇率联动乘数仍由后端计算，但不参与 ETF 估值与溢价。不得将当前实现描述为动态净值模型。
   - **局限**：公布净值日期及行情时刻未严格核验，不能承诺与券商终端逐时刻完全一致；净值回退结果也参与排序与价差测算。
2. **标的池覆盖范围**：
   - **纳指 100 核心宽基**（12只）：`513390`、`513110`、`159513`、`159632`、`159659`、`159660`、`513870`、`513300`、`159696`、`159941`、`159501`、`513100`。
   - **标普 500 核心宽基**（4只）：`513500`、`159612`、`159655`、`513650`。
   - **细分行业/主题标的**：`159509`（景顺纳指科技，跟踪 NDXT）、`513290`（汇添富纳指生物科技，跟踪 NBI）。已放开估值计算，独立专区展示。
3. **空值与异常沉底机制**：
   - 无论按溢价率升序还是降序排序，无现价或数据缺失的标的**必须稳定沉底**在最后，杜绝将故障标的误判为“最低溢价最优标的”。合法 `0.0%` 溢价正常保留。
4. **价差测算器原则**：
   - 仅限同指数标的比较（如纳指宽基内部）；测算金额仅为理论价差示意，不代表实际套利利润。
5. **综合运作费率口径 (Expense Ratio)**：
   - 看板展示的“费率”为基金官方**综合年化运作费率**（管理费 + 托管费，从资产每日计提）。其中 `159941`、`513110`、`159513`、`159509` 为 1.00%；标普 500 为 0.75%~0.80%；低费率宽基为 0.60%。由 `ETF_TOTAL_FEES` 权威字典统一维护。
6. **停复牌状态感知口径 (Suspension Detection)**：
   - 提取腾讯行情 `fields[40]`，值为 `"S"` 时标明 `is_suspended: true`，正常交易时为 `false`；
   - 标的停牌不影响其基于官方 IOPV 的溢价测算与原有排序，前端以琥珀色微型徽章 `临时停牌` 提示；10:30 盘中复牌后自动解除；收盘归档不写入该字段。

---

## 3. 技术架构与关键文件地图 (Architecture & File Map)

- **极简设计原则（KISS）**：
  - **后端**：零第三方依赖，纯 Python 3 标准库（`http.server.ThreadingHTTPServer` + `urllib`）。
  - **前端**：单页面响应式（Vue 3 CDN + Tailwind CSS CDN），无打包编译环节，开箱即用。
  - **数据存储**：业务收盘档案与可再生成运行缓存统一收敛于 **`data/`** 目录，按用途分别维护。
- **关键文件清单**：
  - [`server.py`](server.py)：服务端主程序。内置标的动态发现、行情聚合、`/api/history` 查询与后台自主采集守护线程 `daily_collector_daemon`。
  - [`static/index.html`](static/index.html)：单页面前端。集成响应式 ETF 卡片/桌面表格、换仓测算器、版本徽章及每日收盘历史感知弹窗。
  - [`data/`](data/)：**核心持久化数据目录**。
    - `data/history_daily.json`：生产收盘档案，记录每日全市场宏观中枢与 ETF 量价数据；已被 `.gitignore` 排除，不入库。
    - `data/history_daily.json.example`：冷启动初始种子模板，入库跟踪；首次无数据时自动初始化。
    - `data/nasdaq_symbols_cache.json`：天天基金在线扫描生成的可再生成运行缓存；被 `.gitignore` 忽略，不提交。
    - `data/fear_greed_cache.json`：恐惧贪婪指数最后有效数据的可再生成运行缓存；被 `.gitignore` 忽略，不提交。
  - [`docker-compose.yml`](docker-compose.yml)：容器部署编排，挂载 `./data:/app/data`，端口 `8000:8000`。
  - [`test_review_criteria.py`](test_review_criteria.py)：10 项核心验收、幂等性、移动端结构、停牌状态及情绪缓存回归测试套件。
  - [`test_market_concurrency.py`](test_market_concurrency.py)：5 项行情并发检查，使用模拟上游，不请求真实行情源。
  - [`docs/mobile-adaptation/`](docs/mobile-adaptation/)：本轮移动端需求分析、讨论记录、最终方案、Demo 和视觉证据。
  - [`docs/design-qa.md`](docs/design-qa.md)：正式实现的 Design QA 总览；交接前必须保持 `final result: passed`。
  - [`docs/cnn-fear-greed/`](docs/cnn-fear-greed/)：恐惧贪婪指数的需求、讨论、最终方案、Demo、正式实现证据与专项 QA。

---

## 4. 关键设计决策与避坑指南 (Crucial Decisions & Gotchas)

> ⚠️ **以下踩坑教训与决策结论至关重要，接手时切勿重蹈覆辙**：

1. **Docker 卷挂载必须挂载目录 `./data:/app/data`，切勿挂单文件**：
   - *踩坑背景*：为防止断电损坏 JSON，后端落盘采用了“写临时文件 + `os.replace` 原子替换”机制。在 Linux/Docker 环境下挂载单文件会导致其 inode 被锁定，`os.replace` 会抛出 `OSError: [Errno 16] Device or resource busy` 报错。挂载整个目录彻底解决了该隐患。
2. **后台收盘采集守护线程 (Active Daemon) 的工作机制**：
   - 服务启动时自动启动 `start_daily_collector_daemon()`。
   - 每个工作日（周一至周五）**15:02 ~ 15:30** 期间每分钟巡检一次。
   - 即使数周无人访问网页，只要后台程序在跑，系统也会自主抓取收盘行情并落盘，**绝不漏采**。单日严格只写一次（幂等）。
3. **数据与缓存边界 (Business Data and Runtime Caches)**：
   - `history_daily.json` 是生产收盘档案，已从 Git 中彻底解耦并由 `.gitignore` 排除，杜绝常驻服务器更新拉取冲突；
   - 仓库提供 `history_daily.json.example` 作为种子模板，`ensure_data_storage()` 在首次部署时若文件不存在会自动从模板复制；
   - `nasdaq_symbols_cache.json` 与 `fear_greed_cache.json` 是可再生成的运行缓存，均由 `.gitignore` 排除，不得提交、暂存或回退。
   - Docker 镜像构建与本地运行均使用挂载的 `data/` 目录保存业务档案和本地运行缓存。
4. **沙箱与外网访问**：
   - 在本助手的终端沙箱中，直接网络调用（拉取行情、`git push`）需按规则使用 `BypassSandbox: true`。
5. **移动端响应式展示原则**：
   - `<640px` 使用手机卡片，默认只展示代码/简称、实时溢价率、涨跌幅和现价；点击单条后行内展开完整数据，同时最多展开一条。
   - `>=640px` 保留完整桌面表格。两种视图共享同一份 Vue 数据与计算逻辑，不复制业务口径；切换指数 Tab 时清空展开状态。
   - 顶部操作区使用独立的 `768px` 分界：窄屏为两行（品牌/图标 + 状态/刷新），宽屏为单行。版本、归档、行情时效与刷新信息必须保持紧凑，不得恢复为占满整行的独立卡片。
   - 手机端主要交互控件必须保持至少 44px 触控高度；长简称与跟踪指数允许自然换行，禁止用截断隐藏关键信息。
   - 价差测算结果在手机端使用两列指标块，标签与数值分层，金额值使用不换行约束；两个标的选择框使用系统无衬线字体、14px、500 字重，不再使用 `font-mono`。
6. **移动端交互状态与无障碍约束**：
   - ETF 卡片通过 `expandedEtfCode` 保存唯一展开项；切换 Tab 或刷新后标的消失时必须清空。
   - 卡片使用原生 `button` 与 `aria-expanded`/`aria-controls`；分类使用 `tablist`/`tab`/`aria-selected`。
   - 历史弹窗必须保留 dialog 语义、背景滚动锁定、点击遮罩和 `Esc` 关闭、关闭后焦点恢复。
   - 自动刷新使用单一组合控件：关闭操作在左，频率菜单在右。菜单保留 `listbox`/`option` 语义、`Esc` 返回触发点以及点击外部关闭。
7. **移动端文档协作流程**：
   - 需求与方案统一放在 `docs/mobile-adaptation/`，顺序为“需求分析 → 讨论记录 → 最终方案 → 实施 → Design QA”。不要把中间讨论过程堆入 README。
   - README 只保留稳定的用户能力和使用说明；CHANGELOG 记录版本变化；handover 记录跨 Agent 必须继承的架构约束、当前工作区状态和验证结果。
8. **恐惧贪婪指数必须与 ETF 完全隔离**：
   - 独立路由为 `/api/fear-greed`，主源是 `https://feargreedflow.com/api/fear-greed`；不得从 `/api/quotes` 调用，也不得写入 `history_daily.json`。
   - 成功缓存 60 分钟；到期抓取失败时继续返回 `data/fear_greed_cache.json` 的最后有效数据并标记 `stale: true`，失败后 10 分钟内不重复请求上游。
   - 持久化缓存是可再生成的运行数据，已加入 `.gitignore`；首次无缓存且上游失败时返回 HTTP 503，前端不得把缺失值显示成 0 分。
   - 情绪模块只用于观察市场温度，不参与 ETF 估值、溢价、排序、换仓测算或历史归档。
   - 情绪解释按五个等级分别使用固定通用文案；不读取或返回组成指标，也不做当前主要驱动因素分析。
   - 等级解释提示必须相对评分行定位并限制在视口内，不得因悬浮或聚焦导致页面横向滚动。

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

# 运行核心验收（10项）与行情并发检查（5项）
python3 test_review_criteria.py
python3 test_market_concurrency.py

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

## 7. 当前交接状态 (Current Handoff State)

- **代码基线**：`v1.5.5`，分支 `main`。接手时以 `git log -1 --oneline` 与 `git status --short` 为准，不在文档中固化当前提交哈希。
- **本轮已完成**：行情刷新增加进程内互斥锁与双重缓存检查；缓存 TTL 从刷新完成后起算；后台采集通过 `get_market_data(force=True)` 强刷，管理标的重扫共用同一把锁；前后端版本同步为 `v1.5.5`。
- **隔离边界**：仅合并单个 Python 进程内的普通并发行情刷新，不跨进程合并；不改变 ETF 估值、排序及收盘归档字段。持有普通行情锁时不得再次调用 `get_market_data()`，以免递归加锁死锁。
- **验证基线**：`python3 test_review_criteria.py` 10/10 通过；`python3 test_market_concurrency.py` 5/5 通过；本次提交文件的 `git diff --check` 通过。
- **运行数据**：`data/fear_greed_cache.json` 与 `data/nasdaq_symbols_cache.json` 均为可再生成且被忽略的运行缓存；在线扫描会生成或刷新标的池缓存。接手 Agent 不得提交、暂存或回退这些文件；首次启动无网且无标的池缓存时，服务会使用代码内置的 `DEFAULT_*` 标的池。
- **已知非阻塞项**：浏览器控制台仍有项目原有的 Tailwind CDN 生产提示；尚未进行实体 iOS/Android 字体缩放验收。
- **后续候选方向**：云主机生产部署；基于 `history_daily.json` 的极简历史均溢趋势线。两项均未获本轮实施授权。

下一位 Agent 开始工作前应先检查工作区状态、阅读 `docs/design-qa.md` 与相关专项文档；除非用户明确要求，不得提交、推送、打标签或回退运行数据。
