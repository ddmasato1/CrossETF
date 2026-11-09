"""共享JSON存储回归；只使用临时文件，不启动采集或访问上游。"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import storage


class JsonStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "state.json"

    def test_missing_default_and_bad_json_remain_distinct(self):
        self.assertIsNone(storage.read_json(self.path, default=None))
        with self.assertRaises(FileNotFoundError):
            storage.read_json(self.path)
        self.path.parent.mkdir()
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            storage.read_json(self.path, default=None)

    def test_failed_replace_preserves_archive_and_removes_temporary(self):
        storage.write_json(self.path, {"value": "旧档案"}, ensure_ascii=False)
        original = storage.read_bytes(self.path)
        with patch.object(storage.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                storage.write_json(self.path, {"value": "新档案"})
        self.assertEqual(storage.read_bytes(self.path), original)
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_serialization_failure_cannot_replace_valid_state(self):
        storage.write_json(self.path, {"value": 1})
        with self.assertRaises(ValueError):
            storage.write_json(self.path, {"value": float("nan")}, allow_nan=False)
        self.assertEqual(storage.read_json(self.path), {"value": 1})
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_reentrant_transactions_preserve_concurrent_updates(self):
        storage.write_json(self.path, {"count": 0})
        errors = []

        def update():
            try:
                for _ in range(10):
                    with storage.file_lock(self.path):
                        value = storage.read_json(self.path)
                        value["count"] += 1
                        storage.write_json(self.path, value)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=update, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(storage.read_json(self.path), {"count": 40})
        self.assertIs(storage.file_lock(self.path), storage.file_lock(os.path.abspath(self.path)))

    def test_seed_and_download_preserve_bytes_without_overwriting(self):
        template = self.path.parent.parent / "seed.json"
        raw = b'{\n  "day": 1\n}\n'
        template.write_bytes(raw)
        self.assertTrue(storage.initialize_json(self.path, template))
        self.assertEqual(storage.read_bytes(self.path), raw)
        storage.write_json(self.path, {"day": 2})
        self.assertFalse(storage.initialize_json(self.path, template))
        self.assertEqual(storage.read_json(self.path), {"day": 2})


if __name__ == "__main__":
    unittest.main()
