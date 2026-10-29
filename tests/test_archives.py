"""分卷识别、临时名称和原包保留检查，所有样例都在项目 tmp 内生成。"""
import os
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

import auto_extract as extract

_work_dir = extract.WORK_DIR
import importer
extract.WORK_DIR = _work_dir

ROOT = Path(__file__).resolve().parent.parent


def vint(number):
    data = bytearray()
    while number >= 128:
        data.append((number & 127) | 128)
        number >>= 7
    data.append(number)
    return bytes(data)


def rar_block(kind, flags, body):
    data = vint(kind) + vint(flags) + body
    checked = vint(len(data)) + data
    return struct.pack("<I", zlib.crc32(checked)) + checked


def make_rar_volumes(folder):
    """生成两个不压缩的 RAR5 分卷，按官方格式填写分卷标记和数据校验值。"""
    content = b"Example game data\n" * 256
    filename = b"SummerMemories/readme.txt"
    chunks = (content[:2048], content[2048:])
    paths = []
    for index, chunk in enumerate(chunks):
        more = index == 0
        main = rar_block(1, 0, vint(1 if more else 3) + (b"" if more else vint(index)))
        crc = zlib.crc32(chunk if more else content)
        file = rar_block(2, 2 | (16 if more else 8), vint(len(chunk)) + vint(4) + vint(len(content))
                         + vint(0) + struct.pack("<I", crc) + vint(0) + vint(0) + vint(len(filename)) + filename)
        ending = rar_block(5, 0, vint(1 if more else 0))
        path = folder / ("SummerMemories.part1.gif" if more else "SummerMemories.part2.rar.gif")
        path.write_bytes(b"Rar!\x1a\x07\x01\x00" + main + file + chunk + ending)
        paths.append(path)
    return paths, content


class ArchiveTests(unittest.TestCase):
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
        self.temporary = tempfile.TemporaryDirectory(prefix="archive-tests-", dir=self.temp_base)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.seven = extract.find_seven_zip({})
        self.patches = [patch.object(extract, "ROOT", self.root),
                        patch.object(extract, "WORK_DIR", self.root / "data"),
                        patch.object(extract, "STATE_PATH", self.root / "data/extracted.json"),
                        patch.object(extract, "LOG_DIR", self.root / "logs"),
                        patch.object(extract, "load_config", return_value={})]
        for item in self.patches:
            item.start()
        extract._rar_multivolume.cache_clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        extract._rar_multivolume.cache_clear()
        self.temporary.cleanup()

    def parts(self):
        return make_rar_volumes(self.downloads)[0]

    def test_encrypted_header_uses_known_password_and_cache_tracks_passwords(self):
        outputs = [(2, "Cannot open encrypted archive. Wrong password?"), (0, "Multivolume = +\nVolume Index = 0")]
        with patch.object(extract, "run_7z", side_effect=outputs) as run:
            self.assertTrue(extract._rar_multivolume("example", 100, 1, "7z", ("example-password",)))
            self.assertIn("-pexample-password", run.call_args.args[1])
        extract._rar_multivolume.cache_clear()
        with patch.object(extract, "run_7z", return_value=outputs[0]) as run:
            self.assertFalse(extract._rar_multivolume("example", 100, 1, "7z", ()))
            self.assertFalse(extract._rar_multivolume("example", 100, 1, "7z", ()))
            self.assertEqual(run.call_count, 1)
        with patch.object(extract, "run_7z", side_effect=outputs):
            self.assertTrue(extract._rar_multivolume("example", 100, 1, "7z", ("example-password",)))

    def test_mixed_suffixes_group_once_and_import_from_second_part(self):
        paths = self.parts()
        with patch.object(extract, "rar_multivolume", return_value=True):
            group = extract.archive_group(paths[0])
            self.assertEqual(group["parts"], paths)
            self.assertIsNone(extract.archive_group(paths[1]))
            self.assertEqual(importer.first_volume(paths[1])["parts"], paths)

    def test_regular_gif_is_not_an_archive(self):
        path = self.downloads / "SummerMemories.part1.gif"
        path.write_bytes(b"GIF89a" + b"\0" * 50)
        self.assertIsNone(extract.archive_group(path))

    def test_independent_zip_parts_are_not_combined_or_skipped(self):
        for index in (1, 2):
            path = self.downloads / f"Example.part{index}.gif"
            with zipfile.ZipFile(path, "w") as output:
                output.writestr("example.txt", str(index))
            self.assertEqual(extract.archive_group(path)["parts"], [path])

    def test_alias_cleanup_keeps_original_names_and_bytes(self):
        paths = self.parts()
        with patch.object(extract, "rar_multivolume", return_value=True):
            group = extract.archive_group(paths[0])
        before = [p.read_bytes() for p in paths]
        with patch.object(extract.os, "link", wraps=os.link) as link, extract.readable_group(group) as readable:
            self.assertEqual([p.name for p in readable["parts"]], ["archive.part1.rar", "archive.part2.rar"])
            temporary = readable["trigger"].parent
            self.assertEqual([p.read_bytes() for p in readable["parts"]], before)
            self.assertEqual(link.call_count, 2)
        self.assertFalse(temporary.exists())
        self.assertEqual([p.read_bytes() for p in paths], before)

    def test_alias_copy_fallback_and_failure_cleanup(self):
        paths = self.parts()
        with patch.object(extract, "rar_multivolume", return_value=True):
            group = extract.archive_group(paths[0])
        with patch.object(extract.os, "link", side_effect=OSError("no hard links")):
            with extract.readable_group(group) as readable:
                temporary = readable["trigger"].parent
                self.assertEqual(readable["trigger"].read_bytes(), paths[0].read_bytes())
                self.assertFalse(os.path.samefile(paths[0], readable["trigger"]))
        self.assertFalse(temporary.exists())
        with self.assertRaises(RuntimeError):
            with extract.readable_group(group) as readable:
                temporary = readable["trigger"].parent
                raise RuntimeError("模拟解压失败")
        self.assertFalse(temporary.exists())
        self.assertTrue(all(p.exists() for p in paths))

    def test_missing_and_duplicate_part_numbers_are_not_complete(self):
        paths = self.parts()
        for names in ((paths[1],), (paths[0], paths[0])):
            self.assertFalse(extract.archive_complete({"trigger": names[0], "parts": names, "rar_parts": True}))
        missing = self.downloads / "SummerMemories.part3.rar"
        missing.write_bytes(paths[1].read_bytes())
        self.assertFalse(extract.archive_complete({"trigger": paths[0], "parts": [paths[0], missing], "rar_parts": True}))

    def test_disguised_single_archive_is_recognized(self):
        path = self.downloads / "Example.rar.gif"
        with zipfile.ZipFile(path, "w") as output:
            output.writestr("example.txt", "example")
        self.assertEqual(extract.archive_group(path)["parts"], [path])

    def test_independent_rar_with_part_names_is_not_skipped(self):
        for name in ("Example.part1.rar", "Example.part2.rar", "Example.part2.rar.gif"):
            path = self.downloads / name
            path.write_bytes(b"Rar!\x1a\x07\x01\x00")
            with patch.object(extract, "rar_multivolume", return_value=False):
                self.assertEqual(extract.archive_group(path)["parts"], [path])

    def test_disappeared_archive_is_not_scheduled_from_old_directory_listing(self):
        self.assertIsNone(extract.archive_group(self.downloads / "Example.part2.rar"))

    def test_scan_deletes_completed_standard_volumes_without_retrying_part_two(self):
        if not self.seven:
            self.skipTest("本机没有 7-Zip")
        paths = self.parts()
        for index, path in enumerate(paths, 1):
            path.rename(self.downloads / f"SummerMemories.part{index}.rar")
        config = {"games_dir": str(self.root / "games"), "download_dirs": [str(self.downloads)],
                  "android_dir": str(self.root / "android"), "delete_archive": True, "nested_depth": 3}
        report = extract._scan_once(config, self.seven, {}, {}, {}, force=True)
        self.assertEqual(report, {"processed": 1, "succeeded": 1, "failed": 0, "waiting": 0})
        self.assertFalse(any(self.downloads.iterdir()))
        self.assertEqual(len(list((self.root / "games").glob("Game_*"))), 1)

    def test_real_7zip_reads_and_extracts_mixed_rar_volumes_once(self):
        if not self.seven:
            self.skipTest("本机没有 7-Zip")
        paths, content = make_rar_volumes(self.downloads)
        group = extract.archive_group(paths[0])
        self.assertEqual(group["parts"], paths)
        self.assertIsNone(extract.archive_group(paths[1]))
        with extract.readable_group(group) as readable:
            code, report = extract.run_7z(self.seven, ["t", "-p-", str(readable["trigger"])])
            self.assertEqual(code, 0, report)
        config = {"games_dir": str(self.root / "games"), "download_dirs": [str(self.downloads)],
                  "android_dir": str(self.root / "android"), "delete_archive": False, "nested_depth": 3}
        ok, message = extract.process_group(self.seven, group, config, [], [self.downloads])
        self.assertTrue(ok, message)
        outputs = list((self.root / "games").glob("Game_*/readme.txt"))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].read_bytes(), content)
        self.assertTrue(all(p.exists() for p in paths))
