# 测试目录

测试按功能分组，使用 Python 3.11 标准库；上游由 mock 模拟，档案使用临时目录。

| 目录 | 覆盖内容 |
| --- | --- |
| `acceptance/` | 核心业务与页面结构综合验收 |
| `market/` | 行情并发、原油、ETF年初至今涨幅 |
| `signals/` | 每日定投状态机、确认次数与持久化恢复 |
| `a_share/` | 分钟成交额、日线、腾讯解析、限频与备用源 |
| `http/` | 路由、查询参数、状态码、历史下载及接口隔离 |
| `core/` | JSON原子写入、失败恢复与并发文件事务 |

在最终提交前，从项目根目录集中执行：

```bash
./scripts/run_tests.sh
```

该入口先运行原有的综合验收与行情并发脚本，再通过 unittest 发现其他用例；任何一步失败都会停止。两个原有脚本使用函数和断言组织，单独执行可避免被 unittest 发现机制漏掉。

单独执行某类或某个测试模块时，同样从项目根目录使用包路径：

```bash
python3 -m unittest discover -s tests/a_share -t . -v
python3 -m unittest -v tests.http.test_routes
python3 -m tests.acceptance.test_review_criteria
python3 -m tests.market.test_market_concurrency
```

测试文件中的模块导入和 mock 目标必须使用 `app.*` 的实际业务模块路径。不要直接运行嵌套目录中的 `.py` 文件，也不要为测试修改 `sys.path`。开发期间按约定不逐次运行测试。
