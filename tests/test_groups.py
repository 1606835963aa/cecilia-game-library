"""验证分组保存、旧库迁移和隐藏游戏的访问范围。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, call, patch

import library


class GroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(__file__).resolve().parent.parent / "tmp"
        cls.created_tmp = not cls.tmp.exists()
        cls.tmp.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls.created_tmp:
            cls.tmp.rmdir()

    def setUp(self):
        import game_manager as manager

        self.manager = manager
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="group-tests-", dir=self.tmp))
        self.root = Path(root)
        self.stack.enter_context(patch.multiple(library, ROOT=self.root, DB_PATH=self.root / "library.db",
                                               COVER_DIR=self.root / "covers"))
        library.init_db()
        self.client = self.stack.enter_context(self.new_client())
        manager.lock_all_group_sessions()
        self.addCleanup(manager.lock_all_group_sessions)

    def new_client(self):
        from fastapi.testclient import TestClient

        return TestClient(self.manager.app, base_url="http://127.0.0.1",
                          headers={"X-Token": self.manager.TOKEN})

    def add_game(self, name, status="通关", wishlist=False):
        path = self.root / "games" / name
        path.mkdir(parents=True)
        with library.connect() as conn:
            return conn.execute("INSERT INTO games (name,path,status,is_wishlist,added_at) VALUES (?,?,?,?,?)",
                                (name, library.to_rel(path), status, int(wishlist), library.now_str())).lastrowid

    def create_group(self, name, *, hidden=False, password=None):
        result = self.client.post("/api/groups", json={"name": name, "hidden": hidden, "password": password})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["id"]

    def assign(self, game_id, group_id):
        result = self.client.put(f"/api/games/{game_id}/group", json={"group_id": group_id})
        self.assertEqual(result.status_code, 200, result.text)

    def test_public_group_rename_move_and_delete_keep_game_and_sessions(self):
        game_id = self.add_game("SummerMemories")
        session = library.start_session(game_id)
        library.update_session(session, 62, finished=True)
        a = self.create_group("常玩")
        b = self.create_group("稍后再玩")
        self.assign(game_id, a)
        self.assign(game_id, b)
        self.assertEqual(self.client.patch(f"/api/groups/{b}", json={"name": "收藏"}).status_code, 200)
        self.assertEqual(self.client.post("/api/groups", json={"name": " 收藏 "}).status_code, 400)
        self.assertEqual(self.client.post("/api/groups", json={"name": "通关"}).status_code, 400)

        data = self.client.get("/api/games").json()
        self.assertEqual([g["name"] for g in data["groups"]], ["常玩", "收藏"])
        self.assertEqual(data["games"][0]["group_id"], b)
        self.assertEqual(data["games"][0]["status"], "通关")
        self.assertEqual(self.client.delete(f"/api/groups/{b}").status_code, 200)
        data = self.client.get("/api/games").json()
        self.assertIsNone(data["games"][0]["group_id"])
        self.assertEqual(data["games"][0]["play_seconds"], 62)
        self.assertEqual(data["games"][0]["status"], "通关")
        self.assertTrue((self.root / "games" / "SummerMemories").is_dir())

    def test_locked_games_media_and_history_require_the_correct_password(self):
        private_id = self.add_game("SummerMemories")
        public_id = self.add_game("WinterMemories")
        group_id = self.create_group("我的收藏", hidden=True, password="example-password")
        self.assign(private_id, group_id)
        cover = f"{private_id}-cover.jpg"
        library.COVER_DIR.mkdir()
        (library.COVER_DIR / cover).write_bytes(b"example cover")
        private_path = library.get_game(private_id)["path"]

        refresh = self.manager.RefreshRunner()
        refresh.update(phase="done", succeeded=0, failed=0, waiting=0,
                       games=[(private_id, private_path, "SummerMemories", True),
                              (public_id, "games/WinterMemories", "WinterMemories", False)])
        job = {"id": 1, "name": "SummerMemories", "state": "done", "added_games": [(private_id, private_path)]}
        with self.new_client() as locked, patch.object(self.manager, "refresh", refresh), \
             patch.object(self.manager.imports, "records", [job]), \
             patch.object(self.manager.tracker, "seconds", return_value={private_id: 20}):
            data = locked.get("/api/games").json()
            self.assertEqual(data["groups"], [])
            self.assertEqual([g["id"] for g in data["games"]], [public_id])
            self.assertEqual(locked.get(f"/covers/{cover}").status_code, 404)
            self.assertEqual(locked.get(f"/api/games/{private_id}/sessions").status_code, 404)
            self.assertEqual(locked.get(f"/api/search?q=Example&game_id={private_id}").status_code, 404)
            self.assertEqual(locked.post(f"/api/games/{private_id}/launch").status_code, 404)
            self.assertEqual(locked.put(f"/api/games/{private_id}/group", json={"group_id": None}).status_code, 404)
            self.assertEqual(locked.put(f"/api/games/{public_id}/group", json={"group_id": group_id}).status_code, 404)
            self.assertEqual(locked.delete(f"/api/groups/{group_id}").status_code, 404)
            self.assertEqual(locked.get("/api/import/jobs").json()["jobs"], [])
            self.assertEqual(locked.get("/api/refresh").json()["added"], [])
            self.assertEqual(locked.get("/api/refresh").json()["total"], 1)
            self.assertEqual(locked.post("/api/groups/unlock", json={"password": "wrong-password"}).status_code, 403)
            self.assertEqual(locked.get("/api/games").json()["groups"], [])

            self.assertEqual(locked.post("/api/groups/unlock", json={"password": "example-password"}).status_code, 200)
            data = locked.get("/api/games")
            self.assertEqual(data.headers["Cache-Control"], "no-store")
            self.assertEqual(data.json()["groups"], [{"id": group_id, "name": "我的收藏", "hidden": True}])
            self.assertEqual(next(g for g in data.json()["games"] if g["id"] == private_id)["running_seconds"], 20)
            self.assertEqual(locked.get(f"/covers/{cover}").content, b"example cover")
            self.assertEqual(locked.get(f"/api/games/{private_id}/sessions").status_code, 200)
            self.assertEqual(locked.get("/api/import/jobs").json()["jobs"][0]["name"], "SummerMemories")
            self.assertEqual(locked.get("/api/refresh").json()["added"], ["SummerMemories"])

            self.assertEqual(locked.post("/api/groups/lock").status_code, 200)
            self.assertEqual(locked.get(f"/covers/{cover}").status_code, 404)
            self.assertEqual(locked.get("/api/games").json()["groups"], [])
            self.assertEqual(self.client.get("/api/games").json()["groups"][0]["id"], group_id)

        with library.connect() as conn:
            row = conn.execute("SELECT password_salt,password_hash FROM game_groups WHERE id=?", (group_id,)).fetchone()
        self.assertEqual(len(row["password_salt"]), 16)
        self.assertNotEqual(row["password_hash"], b"example-password")

    def test_unlock_is_per_password_and_expired_on_restart(self):
        first = self.create_group("私密一", hidden=True, password="example-one")
        second = self.create_group("私密二", hidden=True, password="example-two")
        self.manager.lock_all_group_sessions()
        self.assertEqual(self.client.get("/api/games").json()["groups"], [])
        self.assertEqual(self.client.post("/api/groups/unlock", json={"password": "example-two"}).status_code, 200)
        self.assertEqual([g["id"] for g in self.client.get("/api/games").json()["groups"]], [second])
        self.assertEqual(self.client.post("/api/groups/unlock", json={"password": "example-one"}).status_code, 200)
        self.assertEqual([g["id"] for g in self.client.get("/api/games").json()["groups"]], [first, second])

    def test_closing_busy_window_locks_groups_before_it_is_shown_again(self):
        self.create_group("我的收藏", hidden=True, password="example-password")
        shell = self.manager.Shell()
        shell.window = Mock()
        with patch.object(self.manager.tracker, "active", return_value=True):
            self.assertFalse(shell.on_closing())
        self.assertTrue(shell.hidden)
        self.assertEqual(self.client.get("/api/games").json()["groups"], [])
        shell.window.evaluate_js.assert_not_called()
        shell.bring_to_front()
        self.assertEqual(shell.window.method_calls, [
            call.hide(), call.evaluate_js("window.hideLockedGroups?.()"), call.show(),
        ])

    def test_wishlist_attachment_keeps_the_hidden_membership(self):
        game_id = self.add_game("SummerMemories")
        wish_id = self.add_game("WinterMemories", wishlist=True)
        group_id = self.create_group("我的收藏", hidden=True, password="example-password")
        self.assign(game_id, group_id)
        path = f"/api/wishlist/{wish_id}/attach"
        with self.new_client() as locked:
            self.assertEqual(locked.post(path, json={"local_id": game_id}).status_code, 404)
            self.assertEqual(self.client.post(path, json={"local_id": game_id}).status_code, 200)
            self.assertEqual(locked.get("/api/games").json()["games"], [])
        game = self.client.get("/api/games").json()["games"][0]
        self.assertEqual((game["id"], game["group_id"], game["is_wishlist"]), (wish_id, group_id, 0))

    def test_legacy_database_adds_group_column(self):
        with patch.object(library, "DB_PATH", self.root / "legacy.db"):
            with library.connect() as conn:
                conn.execute("CREATE TABLE games (id INTEGER PRIMARY KEY, path TEXT, name TEXT, cover TEXT)")
                conn.execute("INSERT INTO games VALUES (1, 'games/SummerMemories', 'SummerMemories', '')")
            library.init_db()
            self.assertIsNone(library.get_game(1, unlocked_groups=())["group_id"])
            self.assertEqual(library.get_game(1)["name"], "SummerMemories")
            self.assertEqual(library.list_groups(()), [])


if __name__ == "__main__":
    unittest.main()
