# CrossETF

CrossETF 是一个面向个人使用的 ETF 行情看板，用于跟踪境内跨境 ETF 折溢价、观察 A 股宽基 ETF 成交变化，并提供宏观行情和纳指定投参考。

后端使用 Python 3.11 标准库，前端使用 Vue 3 CDN，数据以 JSON 文件保存，无需安装 pip 或 npm 依赖。支持 Docker 常驻部署。

版本更新见 [CHANGELOG](CHANGELOG.md)。开发者或 agent 接手项目时，先阅读 [项目交接](docs/handover.md)，再按任务查阅 `docs/` 下的功能专题及 `app/`、`tests/` 中的对应模块；测试入口见 [测试说明](tests/README.md)。

## 功能

- **跨境 ETF 行情**：跟踪纳指100、标普500及部分行业 ETF，展示现价、折溢价、涨跌幅、年初至今涨幅等，支持排序和自动刷新。估值优先采用 IOPV，缺失时回退公布净值。
- **价差与历史**：提供同指数 ETF 价差金额示意，自动保存每日收盘档案并支持回看。
- **宏观行情**：展示纳指／标普期货、离岸人民币、美债收益率与利差、WTI／布伦特原油，以及恐惧贪婪指数。
- **纳指定投参考**：结合 PE 分位与市场情绪，提供加大、正常或停止投入的参考，包含连续确认和数据时效判断。
- **A股成交观察**：独立页面汇总10只核心宽基 ETF 的分钟成交额与四指数走势，提供单只成交额排行，以及三年起步、持续追加的日成交额与上证指数走势；支持滑轨缩放、悬停和历史回看。

首页支持桌面表格、手机卡片和明暗主题；A股成交观察页目前以桌面使用为主。价差金额仅为示意，成交额不等于净流入，定投建议仅供参考，不执行交易。

## 部署

### Docker Compose（推荐）

需要 Git、Docker 及 Docker Compose。

```bash
git clone https://github.com/ddmasato1/CrossETF.git
cd CrossETF
docker compose up -d --build
```

启动后访问：

- 首页：<http://127.0.0.1:8000>
- A股成交观察：<http://127.0.0.1:8000/volume>

查看日志或停止服务：

```bash
docker compose logs -f
docker compose down
```

更新代码后，重新执行 `docker compose up -d --build`。

运行数据保存在 `data/`，Compose 已挂载整个目录；重建容器会保留数据。迁移或备份时请保留该目录，不要改为挂载单个 JSON 文件。

默认仅在宿主机 `127.0.0.1:8000` 提供访问。远程访问可通过反向代理或 Cloudflare Tunnel 转发，公网入口应阻断管理接口 `/api/refresh-symbols`。代理若运行在其他容器中，应通过私有 Docker 网络连接应用。

### 直接运行 Python

需要 Python 3.11，在项目目录执行：

```bash
python3 server.py
```

默认地址同上，可通过环境变量 `BIND_ADDRESS`、`PORT` 和 `DATA_DIR` 配置监听地址、端口及数据目录。
