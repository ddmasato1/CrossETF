#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON文件的进程内互斥与原子替换；档案格式和降级策略由业务模块决定。"""

import json
import os
import shutil
import threading

_locks = {}
_locks_guard = threading.Lock()
_missing = object()


def file_lock(path):
    path = os.path.abspath(os.fspath(path))
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


def read_json(path, *, default=_missing):
    with file_lock(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            if default is _missing:
                raise
            return default


def read_bytes(path):
    with file_lock(path):
        with open(path, "rb") as handle:
            return handle.read()


def initialize_json(path, template):
    with file_lock(path):
        if os.path.exists(path) or not os.path.exists(template):
            return False
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copyfile(template, path)
        return True


def remove_json(path):
    with file_lock(path):
        os.remove(path)


def write_json(path, value, *, fsync=False, **options):
    path = os.path.abspath(os.fspath(path))
    with file_lock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(value, handle, **options)
                if fsync:
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
