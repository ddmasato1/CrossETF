#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务与业务模块共用的版本、目录和监听配置。"""

import os
from pathlib import Path

APP_VERSION = "v1.9.1"
CURRENT_DIR = str(Path(__file__).resolve().parents[2])
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(CURRENT_DIR, "data")))
BIND_ADDRESS = os.environ.get("BIND_ADDRESS", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
