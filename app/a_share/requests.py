#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股观察采集共用的请求预算；不影响首页现有行情轮询。"""

import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

REQUESTS_PER_MINUTE = 5
MIN_REQUEST_INTERVAL = 12.5


class SourceCoolingDown(Exception):
    pass


class RequestBudget:
    def __init__(self, clock=None, sleep=None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.starts = deque()
        self.cooldowns = {}
        self.failures = {}
        self.total = 0

    def acquire(self, host):
        while True:
            with self.lock:
                now = self.clock()
                if self.cooldowns.get(host, 0) > now:
                    raise SourceCoolingDown(f"{host}处于请求冷却期")
                while self.starts and self.starts[0] <= now - 60:
                    self.starts.popleft()
                wait = max(0, self.starts[-1] + MIN_REQUEST_INTERVAL - now) if self.starts else 0
                if len(self.starts) >= REQUESTS_PER_MINUTE:
                    wait = max(wait, self.starts[0] + 60 - now)
                if wait <= 0:
                    self.starts.append(now)
                    self.total += 1
                    return
            self.sleep(wait)

    def success(self, host):
        with self.lock:
            self.failures[host] = 0

    def failure(self, host, error):
        with self.lock:
            failures = self.failures.get(host, 0) + 1
            self.failures[host] = failures
            delay = min(300, 60 * 2 ** min(failures - 3, 3)) if failures >= 3 else 0
            if isinstance(error, urllib.error.HTTPError) and error.code in (403, 429):
                delay = max(delay, 300)
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                try:
                    seconds = float(retry_after)
                    if math.isfinite(seconds):
                        delay = max(delay, seconds)
                except (TypeError, ValueError, OverflowError):
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        delay = max(delay, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        pass
            self.cooldowns[host] = self.clock() + delay

    def info(self):
        with self.lock:
            now = self.clock()
            return {
                "limit_per_minute": REQUESTS_PER_MINUTE,
                "minimum_interval_seconds": MIN_REQUEST_INTERVAL,
                "requests_last_minute": sum(stamp > now - 60 for stamp in self.starts),
                "requests_since_start": self.total,
                "cooldowns": {host: round(until - now) for host, until in self.cooldowns.items() if until > now},
            }


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不允许一次逻辑请求自动产生不计入预算的额外HTTP请求。
        return None


request_budget = RequestBudget()


def read_upstream(request, timeout=8):
    host = urllib.parse.urlparse(request.full_url).hostname
    opener = urllib.request.build_opener(NoRedirects)
    request_budget.acquire(host)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except Exception as exc:
        request_budget.failure(host, exc)
        raise
    request_budget.success(host)
    return body
