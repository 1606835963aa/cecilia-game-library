"""设置并发保存、密码增删和写入失败检查，使用项目 tmp 内的独立设置。"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import auto_extract as extract

ROOT = Path(__file__).resolve().parent.parent


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_base = ROOT / "tmp"
        cls.created_temp_base = not cls.temp_base.exists()
        cls.temp_base.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls.created_temp_base:
            try:
                cls.temp_base.rmdir()
            except OSError:
                pass

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="config-tests-", dir=self.temp_base)
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps({"theme": "steam", "passwords": ["remove-me"], "custom": "keep"}), encoding="utf-8")
        self.patches = [patch.object(extract, "ROOT", self.root), patch.object(extract, "CONFIG_PATH", self.path),
                        patch.object(extract, "LOG_DIR", self.root / "logs")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_overlapping_threads_keep_settings_and_password_changes(self):
        operations = [lambda: extract.save_config({"theme": "paper"}),
                      lambda: extract.save_config({"auto_scrape": False}),
                      lambda: extract.remove_password("remove-me")]
        operations += [lambda index=index: extract.add_password(f"example-{index}") for index in range(4)]
        barrier = threading.Barrier(len(operations))
        read = Path.read_text

        def slow_read(path, *args, **kwargs):
            content = read(path, *args, **kwargs)
            if path == self.path:
                time.sleep(0.03)
            return content

        def run(operation):
            barrier.wait(timeout=10)
            operation()

        with patch.object(Path, "read_text", slow_read), ThreadPoolExecutor(max_workers=len(operations)) as pool:
            list(pool.map(run, operations))
        saved = extract.load_config()
        self.assertEqual(saved["theme"], "paper")
        self.assertFalse(saved["auto_scrape"])
        self.assertEqual(set(saved["passwords"]), {f"example-{index}" for index in range(4)})
        self.assertEqual(saved["custom"], "keep")

    def test_independent_processes_merge_settings_and_passwords(self):
        code = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import auto_extract as extract
root, index = Path(sys.argv[2]), sys.argv[3]
extract.ROOT = root
extract.CONFIG_PATH = root / 'config.json'
(root / ('ready-' + index)).touch()
deadline = time.monotonic() + 15
while not (root / 'start').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('测试启动超时')
    time.sleep(0.01)
def change(user):
    time.sleep(0.05)
    return {'writer-' + index: True}
extract.save_config(change)
extract.add_password('process-' + index)
"""
        processes = []
        try:
            for index in range(4):
                processes.append(subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT), str(self.root), str(index)],
                                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   creationflags=subprocess.CREATE_NO_WINDOW))
            deadline = time.monotonic() + 15
            while len(list(self.root.glob("ready-*"))) != len(processes):
                self.assertLess(time.monotonic(), deadline, "测试子进程未准备好")
                time.sleep(0.02)
            (self.root / "start").touch()
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, (stdout + stderr).decode(errors="replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        saved = extract.load_config()
        self.assertTrue(all(saved[f"writer-{index}"] for index in range(4)))
        self.assertEqual(set(saved["passwords"]), {"remove-me", *(f"process-{index}" for index in range(4))})
        self.assertEqual(saved["custom"], "keep")

    def test_partial_write_keeps_previous_file_readable(self):
        before = self.path.read_bytes()
        observed = []

        def failed_dump(value, output, **kwargs):
            output.write('{"theme":')
            output.flush()
            observed.append(extract.load_config()["theme"])
            raise OSError("模拟写入中断")

        with patch.object(extract.json, "dump", failed_dump), self.assertRaises(OSError):
            extract.save_config({"theme": "paper"})
        self.assertEqual(observed, ["steam"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "tmp").exists())
        extract.save_config({"theme": "paper"})
        self.assertEqual(extract.load_config()["theme"], "paper")

    def test_failed_replace_keeps_previous_file_and_cleans_temporary(self):
        before = self.path.read_bytes()
        with patch.object(extract.os, "replace", side_effect=OSError("模拟磁盘写入失败")), self.assertRaises(OSError):
            extract.save_config({"theme": "paper"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "tmp").exists())

    def test_invalid_existing_settings_are_not_overwritten(self):
        for content in ('{"passwords":', '[]'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    extract.save_config({"theme": "paper"})
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_password_routes_use_latest_list_and_reject_duplicates(self):
        import game_manager as manager
        from fastapi.testclient import TestClient

        with TestClient(manager.app, base_url="http://127.0.0.1", headers={"X-Token": manager.TOKEN}) as client:
            extract.add_password("imported-password")
            self.assertEqual(client.post("/api/passwords", json={"password": "screen-password"}).status_code, 200)
            self.assertEqual(client.post("/api/passwords", json={"password": "screen-password"}).status_code, 400)
            self.assertEqual(client.post("/api/passwords/remove", json={"password": "remove-me"}).status_code, 200)
        self.assertEqual(extract.load_config()["passwords"], ["imported-password", "screen-password"])

    def test_other_requests_respond_while_settings_writes_wait(self):
        import game_manager as manager
        from fastapi.testclient import TestClient

        locked = threading.Event()
        release = threading.Event()
        saving_password = threading.Event()
        saving_settings = threading.Event()
        add_password = manager.add_password
        save_config = manager.save_config

        def hold_lock():
            with extract.config_lock():
                locked.set()
                self.assertTrue(release.wait(10))

        def observe_password(*args, **kwargs):
            saving_password.set()
            return add_password(*args, **kwargs)

        def observe_settings(*args, **kwargs):
            saving_settings.set()
            return save_config(*args, **kwargs)

        with TestClient(manager.app, base_url="http://127.0.0.1", headers={"X-Token": manager.TOKEN}) as client:
            with patch.object(manager, "add_password", side_effect=observe_password), \
                 patch.object(manager, "save_config", side_effect=observe_settings), \
                 ThreadPoolExecutor(max_workers=4) as pool:
                lock = pool.submit(hold_lock)
                try:
                    self.assertTrue(locked.wait(3))
                    password = pool.submit(client.post, "/api/passwords", json={"password": "screen-password"})
                    settings = pool.submit(client.patch, "/api/settings", json={"auto_scrape": False})
                    self.assertTrue(saving_password.wait(3))
                    self.assertTrue(saving_settings.wait(3))
                    self.assertEqual(pool.submit(client.get, "/healthz").result(timeout=2).status_code, 200)
                    self.assertFalse(password.done())
                    self.assertFalse(settings.done())
                finally:
                    release.set()
                lock.result(timeout=3)
                self.assertEqual(password.result(timeout=3).status_code, 200)
                self.assertEqual(settings.result(timeout=3).status_code, 200)
        saved = extract.load_config()
        self.assertIn("screen-password", saved["passwords"])
        self.assertFalse(saved["auto_scrape"])

    def test_import_does_not_restore_removed_passwords_or_lose_new_ones(self):
        import importer

        archive = self.root / "Example.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("readme.txt", "example")
        job = {"id": 1, "path": str(archive), "name": "SummerMemories", "password": "import-password", "delete_archive": False}

        def finish_extract(seven, group, config, passwords, roots, into):
            extract.remove_password("remove-me")
            extract.add_password("added-during-import")
            extract.save_config({"theme": "paper"})
            payload = into / "Game_000001"
            payload.mkdir()
            (payload / "readme.txt").write_text("example", encoding="utf-8")
            return True, "密码正确"

        worker = importer.Importer(Mock(), Mock())
        with patch.object(importer, "find_seven_zip", return_value="7z"), \
             patch.object(importer, "process_group", side_effect=finish_extract), \
             patch.object(importer.library, "scan", return_value={"added_ids": []}), \
             patch.object(importer.library, "games_under", return_value=[]):
            worker.import_archive(job)
        saved = extract.load_config()
        self.assertEqual(saved["passwords"], ["added-during-import", "import-password"])
        self.assertEqual(saved["theme"], "paper")
        self.assertEqual(saved["custom"], "keep")
        self.assertEqual(job["state"], "done")
