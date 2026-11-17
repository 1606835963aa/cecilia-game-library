"""分卷识别、临时名称和原包保留检查，所有样例都在项目 tmp 内生成。"""
import hashlib
import hmac
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


def make_aes_zip(path, content, password=b"example-password", method=0, version=2, packed=None):
    """生成可由 7-Zip 校验的 AES ZIP，便于单独破坏密文、校验码或压缩流。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if packed is None:
        compressor = zlib.compressobj(wbits=-15)
        packed = compressor.compress(content) + compressor.flush() if method == 8 else content
    salt = b"0123456789abcdef"
    keys = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 66)
    cipher = Cipher(algorithms.AES(keys[:32]), modes.ECB()).encryptor()
    counters = b"".join(i.to_bytes(16, "little") for i in range(1, (len(packed) + 15) // 16 + 1))
    stream = cipher.update(counters) + cipher.finalize()
    encrypted = bytes(a ^ b for a, b in zip(packed, stream))
    payload = salt + keys[64:] + encrypted + hmac.digest(keys[32:64], encrypted, "sha1")[:10]
    info = zipfile.ZipInfo("SummerMemories/readme.txt")
    info.extra = struct.pack("<HHH2sBH", 0x9901, 7, version, b"AE", 3, method)
    with zipfile.ZipFile(path, "w") as output:
        output.writestr(info, payload)
        central = output.start_dir
    data = bytearray(path.read_bytes())
    crc = zlib.crc32(content) if version == 1 else 0
    for offset, value in ((6, 1), (8, 99), (central + 8, 1), (central + 10, 99)):
        struct.pack_into("<H", data, offset, value)
    for offset, value in ((14, crc), (22, len(content)), (central + 16, crc), (central + 24, len(content))):
        struct.pack_into("<I", data, offset, value)
    path.write_bytes(data)
    return 30 + len(info.filename.encode()) + len(info.extra)


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

    def config(self, delete=False):
        return {"games_dir": str(self.root / "games"), "download_dirs": [str(self.downloads)],
                "android_dir": str(self.root / "android"), "delete_archive": delete, "nested_depth": 3}

    def require_seven(self):
        if not self.seven:
            self.skipTest("本机没有 7-Zip")

    def write_zip(self, path, content):
        modified = path.stat().st_mtime_ns if path.exists() else 0
        with zipfile.ZipFile(path, "w") as output:
            output.writestr("SummerMemories/readme.txt", content)
        if modified:
            os.utime(path, ns=(modified + 2_000_000_000, modified + 2_000_000_000))

    def test_corrupt_aes_archive_is_not_published_or_deleted(self):
        self.require_seven()
        path = self.downloads / "Example.zip"
        for damaged_part in ("data", "mac"):
            with self.subTest(damaged_part=damaged_part):
                offset = make_aes_zip(path, b"Example content" * 64)
                data = bytearray(path.read_bytes())
                damaged_offset = offset + 18 if damaged_part == "data" else offset + 18 + 15 * 64
                data[damaged_offset] ^= 1
                path.write_bytes(data)
                state = {}
                config = dict(self.config(delete=True), passwords=["example-password"])
                report = extract._scan_once(config, self.seven, state, {}, {}, force=True)
                self.assertEqual(report["failed"], 1)
                self.assertEqual(report["succeeded"], 0)
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(state, {})
                self.assertFalse(list((self.root / "games").glob("Game_*")))

    def test_chinese_password_aes_zip_is_deleted_only_after_success(self):
        self.require_seven()
        content = b"Example content" * 64
        password = "示例密码"
        for method in (0, 8):
            with self.subTest(method=method):
                path = self.downloads / f"Example{method}.zip"
                make_aes_zip(path, content, password.encode("gbk"), method=method)
                config = dict(self.config(delete=True), passwords=[password])
                report = extract._scan_once(config, self.seven, {}, {}, {}, force=True)
                self.assertEqual(report["succeeded"], 1)
                self.assertFalse(path.exists())
        outputs = list((self.root / "games").glob("Game_*/readme.txt"))
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all(output.read_bytes() == content for output in outputs))

    def test_aes_formats_match_real_7zip(self):
        self.require_seven()
        content = b"Example content" * 64
        path = self.downloads / "Example.zip"
        for version in (1, 2):
            for method in (0, 8):
                with self.subTest(version=version, method=method):
                    make_aes_zip(path, content, method=method, version=version)
                    code, report = extract.run_7z(self.seven, ["t", "-pexample-password", str(path)])
                    self.assertEqual(code, 0, report)
                    target = self.root / f"decoded-{version}-{method}"
                    extract.extract_zip_with_python(path, b"example-password", target)
                    self.assertEqual((target / "SummerMemories/readme.txt").read_bytes(), content)

    def test_python_fallback_reads_real_7zip_aes_output(self):
        self.require_seven()
        content = b"Example content" * 1024
        source = self.root / "readme.txt"
        source.write_bytes(content)
        path = self.downloads / "Example.zip"
        code, report = extract.run_7z(self.seven, ["a", "-tzip", "-mem=AES256", "-pexample-password", str(path), str(source)])
        self.assertEqual(code, 0, report)
        target = self.root / "decoded"
        extract.extract_zip_with_python(path, b"example-password", target)
        self.assertEqual((target / "readme.txt").read_bytes(), content)

    def test_aes_missing_mac_and_bad_metadata_are_rejected(self):
        path = self.downloads / "Example.zip"
        content = b"Example content" * 64
        for damage in ("missing_mac", "size", "crc", "compression", "unfinished_deflate"):
            with self.subTest(damage=damage):
                compressor = zlib.compressobj(wbits=-15)
                packed = compressor.compress(content) + compressor.flush()
                method = 12 if damage == "compression" else 8
                offset = make_aes_zip(path, content, method=method, version=1,
                                      packed=packed[:-1] if damage == "unfinished_deflate" else None)
                with zipfile.ZipFile(path) as archive:
                    info = archive.infolist()[0]
                if damage == "missing_mac":
                    with path.open("r+b") as output:
                        output.truncate(offset + info.compress_size - 10)
                elif damage == "size":
                    info.file_size += 1
                elif damage == "crc":
                    info.CRC ^= 1
                with self.assertRaises(ValueError):
                    extract._decrypt_aes_entry(path, info, b"example-password", self.root / "decoded.txt")

    def test_7zip_warning_keeps_original_archive(self):
        path = self.downloads / "Example.zip"
        self.write_zip(path, "example")
        with patch.object(extract, "archive_is_android", return_value=False), \
             patch.object(extract, "choose_password", return_value=(None, "", "无密码")), \
             patch.object(extract, "extract_archive", return_value=(1, "部分文件未能解压")):
            ok, message = extract.process_group(self.seven, extract.archive_group(path), self.config(delete=True), [], [self.downloads])
        self.assertFalse(ok)
        self.assertIn("解压失败", message)
        self.assertTrue(path.exists())
        self.assertFalse(list((self.root / "games").glob("Game_*")))

    def test_corrupt_nested_aes_zip_keeps_outer_archive(self):
        self.require_seven()
        inner = self.root / "inner.zip"
        offset = make_aes_zip(inner, b"Example content", "示例密码".encode("gbk"))
        data = bytearray(inner.read_bytes())
        data[offset + 18] ^= 1
        outer = self.downloads / "Example.zip"
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("inner.zip", data)
        before = outer.read_bytes()
        ok, message = extract.process_group(self.seven, extract.archive_group(outer), self.config(delete=True), ["示例密码"], [self.downloads])
        self.assertFalse(ok)
        self.assertIn("原包已保留", message)
        self.assertEqual(outer.read_bytes(), before)
        self.assertFalse(list((self.root / "games").glob("Game_*")))

    def test_unchanged_archive_is_skipped_but_same_size_replacement_is_processed(self):
        self.require_seven()
        path = self.downloads / "Example.zip"
        self.write_zip(path, "version one")
        first = extract._scan_once(self.config(), self.seven, {}, {}, {}, force=True)
        self.assertEqual(first["succeeded"], 1)
        state = extract.load_state()
        repeated = extract._scan_once(self.config(), self.seven, state, {}, {}, force=True)
        self.assertEqual(repeated["processed"], 0)
        size = path.stat().st_size
        self.write_zip(path, "version two")
        self.assertEqual(path.stat().st_size, size)
        changed = extract._scan_once(self.config(), self.seven, state, {}, {}, force=True)
        self.assertEqual(changed["succeeded"], 1)
        outputs = sorted(p.read_text() for p in (self.root / "games").glob("Game_*/readme.txt"))
        self.assertEqual(outputs, ["version one", "version two"])

    def test_same_size_and_timestamp_updates_are_processed(self):
        self.require_seven()
        path = self.downloads / "Example.zip"
        self.write_zip(path, "version one")
        before = path.stat()
        self.assertEqual(extract._scan_once(self.config(), self.seven, {}, {}, {}, force=True)["succeeded"], 1)

        replacement = self.root / "replacement.zip"
        self.write_zip(replacement, "version two")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        current = path.stat()
        self.assertEqual((current.st_size, current.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertEqual(extract._scan_once(self.config(), self.seven, extract.load_state(), {}, {}, force=True)["succeeded"], 1)

        self.write_zip(path, "version six")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        rewritten = path.stat()
        self.assertEqual((rewritten.st_size, rewritten.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertEqual(extract._scan_once(self.config(), self.seven, extract.load_state(), {}, {}, force=True)["succeeded"], 1)
        outputs = sorted(p.read_text() for p in (self.root / "games").glob("Game_*/readme.txt"))
        self.assertEqual(outputs, ["version one", "version six", "version two"])

    def test_new_archive_replacing_old_one_before_cleanup_is_preserved(self):
        self.require_seven()
        path = self.downloads / "Example.zip"
        replacement = self.root / "replacement.zip"
        self.write_zip(path, "version one")
        self.write_zip(replacement, "version two")
        new_bytes = replacement.read_bytes()
        before = path.stat()
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        publish = extract.publish_game

        def replace_after_publish(*args, **kwargs):
            published = publish(*args, **kwargs)
            os.replace(replacement, path)
            return published

        with patch.object(extract, "publish_game", side_effect=replace_after_publish):
            report = extract._scan_once(self.config(delete=True), self.seven, {}, {}, {}, force=True)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(path.read_bytes(), new_bytes)
        outputs = [p.read_text() for p in (self.root / "games").glob("Game_*/readme.txt")]
        self.assertEqual(outputs, ["version one"])

        report = extract._scan_once(self.config(delete=True), self.seven, extract.load_state(), {}, {}, force=True)
        self.assertEqual(report["succeeded"], 1)
        self.assertFalse(path.exists())
        outputs = sorted(p.read_text() for p in (self.root / "games").glob("Game_*/readme.txt"))
        self.assertEqual(outputs, ["version one", "version two"])

    def test_change_to_later_volume_is_not_skipped(self):
        self.require_seven()
        paths = self.parts()
        state = {}
        first = extract._scan_once(self.config(), self.seven, state, {}, {}, force=True)
        self.assertEqual(first["succeeded"], 1)
        old_time = paths[1].stat().st_mtime_ns
        os.utime(paths[1], ns=(old_time + 2_000_000_000, old_time + 2_000_000_000))
        changed = extract._scan_once(self.config(), self.seven, state, {}, {}, force=True)
        self.assertEqual(changed["succeeded"], 1)
        self.assertEqual(len(list((self.root / "games").glob("Game_*"))), 2)

    def test_legacy_state_is_preserved_then_tracks_changes(self):
        self.require_seven()
        path = self.downloads / "Example.zip"
        self.write_zip(path, "version one")
        state = {str(path): True}
        report = extract._scan_once(self.config(), self.seven, state, {}, {}, force=True)
        self.assertEqual(report["processed"], 0)
        self.assertIsInstance(extract.load_state()[str(path)], list)
        self.write_zip(path, "version two")
        changed = extract._scan_once(self.config(), self.seven, extract.load_state(), {}, {}, force=True)
        self.assertEqual(changed["succeeded"], 1)

    def test_legacy_size_and_timestamp_record_is_upgraded(self):
        path = self.downloads / "Example.zip"
        self.write_zip(path, "example")
        before = path.stat()
        state = {str(path): [[path.as_posix(), before.st_size, before.st_mtime_ns]]}
        report = extract._scan_once(self.config(), self.seven, state, {}, {}, force=True)
        self.assertEqual(report["processed"], 0)
        self.assertEqual(extract.load_state()[str(path)], extract.group_signature(extract.archive_group(path)))

    def test_deleted_archive_does_not_leave_stale_in_memory_state(self):
        self.require_seven()
        path = self.downloads / "Example.zip"
        state = {}
        for _ in range(2):
            self.write_zip(path, "example")
            report = extract._scan_once(self.config(delete=True), self.seven, state, {}, {}, force=True)
            self.assertEqual(report["succeeded"], 1)
            self.assertEqual(state, {})
            self.assertEqual(extract.load_state(), {})

    def test_download_stability_includes_modification_time(self):
        path = self.downloads / "Example.zip"
        self.write_zip(path, "version one")
        group = extract.archive_group(path)
        seen = {}
        with patch.object(extract.time, "time", return_value=100):
            self.assertFalse(extract.group_ready(group, 8, seen))
        with patch.object(extract.time, "time", return_value=110):
            self.assertTrue(extract.group_ready(group, 8, seen))
        self.write_zip(path, "version two")
        with patch.object(extract.time, "time", return_value=110):
            self.assertFalse(extract.group_ready(group, 8, seen))
        path.unlink()
        self.assertFalse(extract.group_ready(group, 8, seen))

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

    def test_cleanup_of_mixed_rar_volumes_checks_all_parts_before_deleting(self):
        self.require_seven()
        paths = self.parts()
        replacement = self.root / "replacement.rar"
        replacement.write_bytes(paths[1].read_bytes())
        publish = extract.publish_game

        def replace_after_publish(*args, **kwargs):
            published = publish(*args, **kwargs)
            os.replace(replacement, paths[1])
            return published

        with patch.object(extract, "publish_game", side_effect=replace_after_publish):
            report = extract._scan_once(self.config(delete=True), self.seven, {}, {}, {}, force=True)
        self.assertEqual(report["succeeded"], 1)
        self.assertTrue(all(path.is_file() for path in paths))

        report = extract._scan_once(self.config(delete=True), self.seven, extract.load_state(), {}, {}, force=True)
        self.assertEqual(report["succeeded"], 1)
        self.assertFalse(any(path.exists() for path in paths))

    def test_cleanup_cancels_deletion_if_a_later_volume_cannot_be_marked(self):
        self.require_seven()
        paths = self.parts()
        mark = extract.mark_archive_for_deletion
        marked = []

        def fail_second_mark(fd, delete):
            if delete and marked:
                raise PermissionError("分卷暂时被占用")
            mark(fd, delete)
            if delete:
                marked.append(fd)

        with patch.object(extract, "mark_archive_for_deletion", side_effect=fail_second_mark):
            report = extract._scan_once(self.config(delete=True), self.seven, {}, {}, {}, force=True)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(len(marked), 1)
        self.assertTrue(all(path.is_file() for path in paths))

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
