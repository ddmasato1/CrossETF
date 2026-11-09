#!/bin/bash
# 提交前集中执行验收、并发检查及全部unittest回归。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

python3 -m tests.acceptance.test_review_criteria
python3 -m tests.market.test_market_concurrency
python3 -m unittest discover -s tests -t . -v
