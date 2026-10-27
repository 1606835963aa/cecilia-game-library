"""更新包边界、失败还原和更新调度的隔离检查。"""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import update_helper as helper
import updates
from app_version import VERSION

ROOT = Path(__file__).resolve().parent.parent


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_root = ROOT / "tmp"
        cls.created_tmp = not cls.temporary_root.exists()
        cls.temporary_root.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls.created_tmp:
            try:
                cls.temporary_root.rmdir()
            except OSError:
                pass

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="update-tests-", dir=self.temporary_root)
        self.root = Path(self.temporary.name).resolve()
        self.job = self.root / "tmp/updates" / uuid.uuid4().hex
        self.job.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def package(self, extras=None, transform=None):
        contents = {name: ("新版 " + name).encode() for name in helper.REQUIRED}
        contents["_internal/web/style.css"] = b"body {}"
        manifest = {"schema": 1, "version": VERSION, "files": {
            name: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in contents.items()}}
        if transform:
            transform(contents, manifest)
        contents[helper.MANIFEST] = json.dumps(manifest).encode()
        archive = self.job / helper.PACKAGE
        with zipfile.ZipFile(archive, "w") as zf:
            for name, content in {**contents, **(extras or {})}.items():
                zf.writestr(helper.NAME + "/" + name, content)
            zf.writestr(helper.NAME + "/games/", b"")
            zf.writestr(helper.NAME + "/downloads/", b"")
        return archive, helper.digest(archive)

    def stage(self, **options):
        archive, sha256 = self.package(**options)
        return helper.stage_package(archive, self.job / "staged", VERSION, sha256)

    def old_program(self):
        for name in helper.OWNED:
            path = self.root / name
            if name in ("_internal", "7-Zip"):
                path.mkdir()
                path = path / "old.txt"
            path.write_bytes(b"old-program")
        for name in ("config.json", "games/SummerMemories/save.dat", "downloads/unfinished.zip.part",
                     "android/example.apk", "data/covers/custom.png", "logs/extract.log"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep-user-data")
        with closing(sqlite3.connect(self.root / "data/library.db")) as conn, conn:
            conn.execute("CREATE TABLE sessions (seconds INTEGER)")
            conn.execute("INSERT INTO sessions VALUES (12345)")

    def user_snapshot(self):
        snapshot = {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and
                path.name != "library.db" and
                path.relative_to(self.root).parts[0] in ("config.json", "games", "downloads", "android", "data", "logs")}
        with closing(sqlite3.connect(self.root / "data/library.db")) as conn:
            snapshot["database"] = tuple(conn.iterdump())
        return snapshot

    def manager(self, enabled=True):
        (self.root / "更新助手.exe").write_bytes(b"helper")
        config = {"auto_update": enabled}
        manager = updates.UpdateManager(self.root, True, lambda: config, Mock(return_value=""), Mock(), Mock(), Mock())
        manager.supported = True
        return manager, config

    def release(self):
        return {"tag_name": "v99.0.0", "draft": False, "prerelease": False, "assets": [{
            "name": helper.PACKAGE, "size": 123, "digest": "sha256:" + "a" * 64,
            "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/v99.0.0/{helper.PACKAGE}"}]}

    def test_version_comparison_and_no_downgrade(self):
        self.assertGreater(helper.version_tuple("1.10.0"), helper.version_tuple("v1.9"))
        for invalid in ("1.1-beta", "../2.0", "latest", "1", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                helper.version_tuple(invalid)
        release = self.release()
        release["tag_name"] = "v" + VERSION
        self.assertIsNone(updates.release_info(release))

    def test_only_complete_official_stable_release(self):
        self.assertEqual(updates.release_info(self.release())["version"], "99.0.0")
        for change in (lambda r: r.update(prerelease=True), lambda r: r.update(draft=True),
                       lambda r: r.update(assets=[]), lambda r: r["assets"][0].update(digest=None),
                       lambda r: r["assets"][0].update(size=helper.MAX_PACKAGE + 1),
                       lambda r: r["assets"][0].update(browser_download_url="https://example.org/other.zip")):
            release = self.release()
            change(release)
            with self.assertRaises(ValueError):
                updates.release_info(release)

    def test_complete_package_extracts_only_program_files(self):
        self.stage()
        self.assertTrue((self.job / "staged/_internal/web/index.html").is_file())
        self.assertFalse((self.job / "staged/games").exists())

    def test_reject_package_with_user_data_or_escaping_paths(self):
        for path in ("config.json", "data/library.db", "games/save.dat", "_internal/../../config.json",
                     "_internal/x:secret", "_internal/CON", "_internal/file. "):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.stage(extras={path: b"bad"})
        self.assertFalse((self.root / "config.json").exists())

    def test_reject_modified_or_incomplete_package(self):
        archive, _ = self.package()
        with self.assertRaises(ValueError):
            helper.stage_package(archive, self.job / "staged", VERSION, "0" * 64)
        for transform in (lambda c, m: c.update({"LICENSE": b"changed"}),
                          lambda c, m: m.update(version="99.0.0"),
                          lambda c, m: c.pop("自动解压.exe")):
            with self.assertRaises(ValueError):
                self.stage(transform=transform)
        self.assertFalse((self.job / "staged").exists())

    def test_replace_and_restore_preserve_all_user_data(self):
        self.old_program()
        before = self.user_snapshot()
        self.stage()
        journal = helper.replace_program(self.root, self.job)
        self.assertNotEqual((self.root / "LICENSE").read_bytes(), b"old-program")
        self.assertFalse((self.root / "_internal/old.txt").exists())
        self.assertEqual(before, self.user_snapshot())
        with closing(sqlite3.connect(self.root / "data/library.db")) as conn, conn:
            conn.execute("UPDATE sessions SET seconds=0")
        helper.restore(self.root, self.job, journal)
        self.assertEqual((self.root / "LICENSE").read_bytes(), b"old-program")
        self.assertTrue((self.root / "_internal/old.txt").is_file())
        self.assertEqual(before, self.user_snapshot())

    def test_partial_replace_failure_restores_previous_program(self):
        self.old_program()
        before = self.user_snapshot()
        self.stage()
        original = os.replace

        def fail_internal(source, target):
            if Path(source) == self.job / "staged/_internal":
                raise PermissionError("模拟文件占用")
            return original(source, target)

        with patch.object(helper.os, "replace", side_effect=fail_internal), self.assertRaises(PermissionError):
            helper.replace_program(self.root, self.job)
        helper.restore(self.root, self.job, helper.read_json(self.job / "journal.json"))
        for name in helper.OWNED:
            target = self.root / name
            self.assertEqual((target / "old.txt" if target.is_dir() else target).read_bytes(), b"old-program")
        self.assertEqual(before, self.user_snapshot())

    def test_new_program_boot_failure_rolls_back(self):
        self.old_program()
        before = self.user_snapshot()
        archive, sha256 = self.package()
        helper.write_json(self.root / "tmp/updates/active.json", {"job": self.job.name, "expires": time.time() + 300})
        helper.write_json(self.job / "parent-exiting.json", {})
        plan = {"root": str(self.root), "version": VERSION, "sha256": sha256, "wait_pids": [], "extractors": [], "token": "test"}
        helper.write_json(self.job / "plan.json", plan)
        candidate = Mock()
        candidate.poll.return_value = 1
        with patch.object(helper, "restart", return_value=candidate) as restart, self.assertRaises(RuntimeError):
            helper.apply_update(self.job / "plan.json")
        self.assertEqual(restart.call_count, 2)
        self.assertEqual((self.root / "LICENSE").read_bytes(), b"old-program")
        self.assertEqual(before["config.json"], (self.root / "config.json").read_bytes())
        self.assertFalse(helper.active_request(self.root))
        self.assertEqual(helper.read_json(self.root / "data/software-update.json")["phase"], "error")

    def test_success_requires_new_version_boot_confirmation(self):
        self.old_program()
        archive, sha256 = self.package()
        helper.write_json(self.root / "tmp/updates/active.json", {"job": self.job.name, "expires": time.time() + 300})
        helper.write_json(self.job / "parent-exiting.json", {})
        plan = {"root": str(self.root), "version": VERSION, "sha256": sha256, "wait_pids": [], "extractors": [], "token": "test"}
        helper.write_json(self.job / "plan.json", plan)

        def started(root, saved_plan, updated=False):
            self.assertTrue(updated)
            helper.write_json(self.job / "booted.json", {"token": "test", "version": VERSION})
            process = Mock()
            process.poll.return_value = None
            return process

        with patch.object(helper, "restart", side_effect=started):
            helper.apply_update(self.job / "plan.json")
        self.assertTrue(helper.read_json(self.job / "finished.json")["ok"])
        self.assertFalse(helper.active_request(self.root))

    def test_download_verifies_size_hash_and_keeps_current_program(self):
        manager, config = self.manager()
        (self.root / "LICENSE").write_bytes(b"old")
        data = b"a downloaded package"
        manager.info = {"url": "https://github.com/test", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "version": "99.0"}
        with patch.object(updates, "open_url", return_value=io.BytesIO(data)):
            manager._download()
        self.assertEqual(manager.state["phase"], "ready")
        self.assertEqual((self.root / "LICENSE").read_bytes(), b"old")
        for damaged in (b"short", b"A" * len(data), data + b"extra"):
            with patch.object(updates, "open_url", return_value=io.BytesIO(damaged)), self.assertRaises(ValueError):
                manager._download()
        config["auto_update"] = False
        with patch.object(updates, "open_url", return_value=io.BytesIO(data)), self.assertRaises(InterruptedError):
            manager._download()

    def test_busy_library_defers_handoff_without_pausing_other_work(self):
        manager, _ = self.manager()
        manager.prepare.return_value = "等游戏结束后更新"
        manager.handoff()
        self.assertIn("等游戏结束", manager.state["message"])
        self.assertFalse((self.root / "tmp/updates/active.json").exists())
        manager.close.assert_not_called()

    def test_source_run_cannot_replace_program(self):
        manager, _ = self.manager()
        manager.supported = False
        manager.info = {"version": "99.0"}
        manager.update(phase="available")
        with self.assertRaises(ValueError):
            manager.install()

    def test_auto_update_waits_for_idle_and_honors_switch(self):
        for enabled, active, editing, expected in ((True, True, False, False), (True, False, True, False),
                                                   (False, False, False, False), (True, False, False, True)):
            with self.subTest(enabled=enabled, active=active, editing=editing):
                manager, _ = self.manager(enabled)
                manager.state["phase"] = "ready"
                manager.last_activity = time.monotonic() if active else time.monotonic() - 120
                manager.editing_until = time.monotonic() + 10 if editing else 0
                manager.stop = Mock()
                manager.stop.wait.side_effect = [False, True]
                with patch.object(manager, "handoff") as handoff:
                    manager.loop()
                    self.assertEqual(handoff.called, expected)

    def test_manual_update_can_start_without_waiting_for_idle(self):
        manager, _ = self.manager(False)
        manager.state["phase"] = "ready"
        manager.install()
        manager.stop = Mock()
        manager.stop.wait.side_effect = [False, True]
        with patch.object(manager, "handoff") as handoff:
            manager.loop()
            handoff.assert_called_once()

    def test_auto_update_off_checks_without_downloading(self):
        manager, _ = self.manager(False)
        manager.operation.acquire()
        with patch.object(updates, "open_url", return_value=io.BytesIO(json.dumps(self.release()).encode())), patch.object(manager, "_download") as download:
            manager._check()
        self.assertEqual(manager.state["phase"], "available")
        download.assert_not_called()

    def test_auto_update_on_downloads_new_release(self):
        manager, _ = self.manager(True)
        manager.operation.acquire()
        with patch.object(updates, "open_url", return_value=io.BytesIO(json.dumps(self.release()).encode())), patch.object(manager, "_download") as download:
            manager._check()
        download.assert_called_once()

    def test_update_marker_expires_and_extractor_does_not_start(self):
        import auto_extract
        marker = self.root / "tmp/updates/active.json"
        helper.write_json(marker, {"job": self.job.name, "expires": time.time() + 300})
        with patch.object(auto_extract, "ROOT", self.root), patch.object(auto_extract, "LOG_DIR", self.root / "logs"):
            self.assertTrue(auto_extract.software_update_pending())
            with patch.object(auto_extract.sys, "argv", ["auto_extract.py", "--once"]), patch.object(auto_extract, "scan_once") as scan:
                auto_extract.main()
                scan.assert_not_called()
            helper.write_json(marker, {"job": self.job.name, "expires": time.time() - 1})
            self.assertFalse(auto_extract.software_update_pending())

    def test_api_update_guards_and_settings(self):
        import game_manager as manager_module
        from fastapi.testclient import TestClient
        manager, _ = self.manager()
        with patch.object(manager_module, "software_updates", manager), patch.object(manager_module, "load_config", return_value={}), patch.object(manager, "check", return_value={"phase": "checking"}) as check:
            with TestClient(manager_module.app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get("/api/updates").status_code, 403)
                self.assertEqual(client.get("/api/updates", headers={"Host": "example.org", "X-Token": manager_module.TOKEN}).status_code, 403)
                headers = {"X-Token": manager_module.TOKEN}
                self.assertEqual(client.get("/api/updates", headers=headers).json()["current_version"], VERSION)
                self.assertTrue(client.get("/api/settings", headers=headers).json()["auto_update"])
                self.assertEqual(client.post("/api/updates/check", headers=headers).status_code, 200)
                check.assert_called_once()
                try:
                    manager_module.maintenance["updating"] = True
                    self.assertEqual(client.post("/api/updates/install", headers=headers).status_code, 503)
                finally:
                    manager_module.maintenance["updating"] = False

    def test_game_and_background_tasks_prevent_update(self):
        import game_manager as module
        with patch.object(module.tracker, "active", return_value=True):
            self.assertIn("游戏结束", module.prepare_software_update())
            self.assertFalse(module.maintenance["updating"])
        with patch.object(module.tracker, "active", return_value=False), patch.object(module.imports, "busy", return_value=True):
            self.assertIn("导入和解压", module.prepare_software_update())
            self.assertFalse(module.maintenance["updating"])
        with patch.object(module.tracker, "active", return_value=False), patch.object(module.imports, "busy", return_value=False), patch.object(module.refresh, "busy", return_value=False), patch.object(module.worker, "busy", return_value={1}):
            self.assertIn("资料获取", module.prepare_software_update())
            self.assertFalse(module.maintenance["updating"])

    def test_login_callback_cannot_queue_work_during_update(self):
        import game_manager as module
        try:
            module.maintenance["updating"] = True
            with patch.object(module.worker, "enqueue") as enqueue:
                module.shell.on_itch_ready()
                enqueue.assert_not_called()
        finally:
            module.maintenance["updating"] = False


if __name__ == "__main__":
    unittest.main()
