"""冷藏包还原、去重、损坏检查和路径边界，样本只写入项目 tmp。"""
import hashlib
import io
import json
import os
import random
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import cecold

ROOT = Path(__file__).resolve().parent.parent


class ColdPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / "tmp"
        cls.created_base = not cls.base.exists()
        cls.base.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls.created_base:
            try:
                cls.base.rmdir()
            except OSError:
                pass

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cecold-tests-", dir=self.base)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(self.base.resolve()))
        self.source = self.root / "SummerMemories"
        self.source.mkdir()
        self.archive = self.root / "example.cecold"
        self.destination = self.root / "restored"
        self.root_patch = patch.object(cecold, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        cecold.Reader.block.cache_clear()
        self.temporary.cleanup()

    def make_package(self, **kwargs):
        return cecold.pack(self.source, self.archive, threads=2, chunk_size=4096,
                           block_size=32768, dictionary=1024**2, **kwargs)

    def snapshot(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def rewrite_index(self, change):
        data = self.archive.read_bytes()
        _, offset, length, _ = cecold.FOOTER.unpack(data[-cecold.FOOTER.size:])
        index = json.loads(zlib.decompress(data[offset:offset + length]))
        change(index)
        encoded = zlib.compress(json.dumps(index).encode())
        self.archive.write_bytes(data[:offset] + encoded + cecold.FOOTER.pack(
            cecold.END_MAGIC, offset, len(encoded), hashlib.sha256(encoded).digest()))

    def test_round_trip_preserves_unicode_names_empty_dirs_and_independent_duplicates(self):
        (self.source / "中文目录" / "空目录").mkdir(parents=True)
        data = random.Random(7).randbytes(100000)
        (self.source / "中文目录" / "资源.bin").write_bytes(data)
        (self.source / "duplicate.bin").write_bytes(data)
        (self.source / "empty.txt").touch()
        before = self.snapshot(self.source)
        result = self.make_package()
        self.assertEqual(result["unique_files"], 2)
        self.assertTrue(cecold.unpack(self.archive, self.destination)["verified"])
        self.assertEqual(self.snapshot(self.destination), before)
        self.assertTrue((self.destination / "中文目录/空目录").is_dir())
        (self.destination / "duplicate.bin").write_bytes(b"changed")
        self.assertEqual((self.destination / "中文目录/资源.bin").read_bytes(), data)
        self.assertEqual(self.snapshot(self.source), before)

    def test_shared_chunks_and_zero_ranges_restore_exact_bytes(self):
        chunks = [random.Random(i).randbytes(4096) for i in range(3)]
        (self.source / "first.bin").write_bytes(chunks[0] + chunks[1] + chunks[0] + b"\0" * 8192)
        (self.source / "second.bin").write_bytes(chunks[2] + chunks[1])
        result = self.make_package()
        self.assertGreater(result["zero_chunks"], 0)
        data = cecold.Reader(self.archive).manifest
        self.assertLess(len(data["chunks"]), sum(len(obj["chunks"]) for obj in data["objects"]))
        cecold.unpack(self.archive, self.destination)
        self.assertEqual(self.snapshot(self.source), self.snapshot(self.destination))

    def test_content_chunks_realign_after_inserted_bytes(self):
        data = random.Random(91).randbytes(2 * 1024**2)
        original = list(cecold.data_chunks(io.BytesIO(data), 256 * 1024))
        shifted = list(cecold.data_chunks(io.BytesIO(b"inserted" + data), 256 * 1024))
        self.assertEqual(b"".join(original), data)
        self.assertEqual(b"".join(shifted), b"inserted" + data)
        self.assertTrue(all(0 < len(chunk) <= 256 * 1024 for chunk in shifted))
        self.assertGreater(len(set(original) & set(shifted)), len(original) // 2)

    def test_temporary_windows_file_lock_is_retried(self):
        (self.source / "example.bin").write_bytes(b"example")
        self.make_package()
        rename = cecold.os.rename
        calls = []

        def briefly_locked(source, destination):
            calls.append((source, destination))
            if len(calls) < 3:
                raise PermissionError("模拟短暂文件占用")
            return rename(source, destination)

        with patch.object(cecold.os, "rename", side_effect=briefly_locked), patch.object(cecold.time, "sleep"):
            cecold.unpack(self.archive, self.destination)
        self.assertEqual(len(calls), 3)
        self.assertEqual((self.destination / "example.bin").read_bytes(), b"example")

    def test_empty_game_directory_round_trip(self):
        (self.source / "empty").mkdir()
        self.make_package()
        cecold.unpack(self.archive, self.destination)
        self.assertTrue((self.destination / "empty").is_dir())

    def test_reference_version_one_archive_remains_readable(self):
        fixture = ROOT / "tests/fixtures/cecold-v1-reference.cecold"
        cecold.unpack(fixture, self.destination)
        expected = b"legacy-game-data\n" * 1000
        self.assertEqual((self.destination / "中文目录/资源.bin").read_bytes(), expected)
        self.assertEqual((self.destination / "duplicate.bin").read_bytes(), expected)
        self.assertEqual((self.destination / "zeros.bin").read_bytes(), b"\0" * 12000)
        self.assertTrue((self.destination / "中文目录/空目录").is_dir())
        self.assertEqual((self.destination / "duplicate.bin").stat().st_mtime_ns, 1600000000000000000)

    def test_executable_filter_is_reversible(self):
        data = bytearray(random.Random(8).randbytes(120000))
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 128)
        data[128:134] = b"PE\0\0\x64\x86"
        for offset in range(256, len(data) - 5, 8):
            data[offset:offset + 5] = b"\xe8" + struct.pack("<i", 4096 - offset)
        (self.source / "example.exe").write_bytes(data)
        self.make_package()
        cecold.unpack(self.archive, self.destination)
        self.assertEqual((self.destination / "example.exe").read_bytes(), data)

    def test_corrupt_payload_never_publishes_partial_game(self):
        (self.source / "example.bin").write_bytes(random.Random(4).randbytes(100000))
        self.make_package()
        data = bytearray(self.archive.read_bytes())
        data[cecold.HEADER.size + 5] ^= 1
        self.archive.write_bytes(data)
        with self.assertRaises(cecold.FormatError):
            cecold.unpack(self.archive, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "tmp").exists())

    def test_truncated_or_corrupt_index_is_rejected(self):
        (self.source / "example.txt").write_text("example", encoding="utf-8")
        self.make_package()
        good = self.archive.read_bytes()
        for data in (good[:5], good[:-1], good[:-cecold.FOOTER.size] + b"\0" * cecold.FOOTER.size):
            with self.subTest(size=len(data)):
                self.archive.write_bytes(data)
                with self.assertRaises(cecold.FormatError):
                    cecold.Reader(self.archive)

    def test_escaping_and_windows_alias_paths_are_rejected(self):
        (self.source / "example.bin").write_bytes(b"example")
        self.make_package()
        good = self.archive.read_bytes()
        for name in ("../outside.bin", "/outside.bin", "C:/outside.bin", "file:stream", "CON.txt", "file.", "a/../file", "a\\file"):
            with self.subTest(name=name):
                self.archive.write_bytes(good)
                self.rewrite_index(lambda index: index["files"][0].update(path=name))
                with self.assertRaises(cecold.FormatError):
                    cecold.unpack(self.archive, self.destination)
                self.assertFalse(self.destination.exists())

    def test_oversized_blocks_unknown_codecs_and_wrong_references_are_rejected(self):
        (self.source / "example.bin").write_bytes(b"example" * 2000)
        self.make_package()
        good = self.archive.read_bytes()
        changes = [lambda x: x["blocks"][0].update(size=cecold.MAX_BLOCK + 1),
                   lambda x: x["blocks"][0].update(codec="unknown"),
                   lambda x: x["files"][0].update(object=10000),
                   lambda x: x["chunks"][0].update(offset=-1),
                   lambda x: x.update(dictionary=2**40),
                   lambda x: x["files"].append(dict(x["files"][0], path="EXAMPLE.BIN"))]
        for change in changes:
            self.archive.write_bytes(good)
            self.rewrite_index(change)
            with self.assertRaises(cecold.FormatError):
                cecold.Reader(self.archive)

    def test_existing_outputs_are_not_overwritten(self):
        (self.source / "example.txt").write_text("example", encoding="utf-8")
        self.make_package()
        old = self.archive.read_bytes()
        with self.assertRaises(ValueError):
            self.make_package()
        self.assertEqual(self.archive.read_bytes(), old)
        self.destination.mkdir()
        (self.destination / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            cecold.unpack(self.archive, self.destination)
        self.assertEqual((self.destination / "keep.txt").read_text(), "keep")

    def test_changed_source_aborts_before_publishing(self):
        path = self.source / "example.bin"
        path.write_bytes(b"example" * 10000)
        original = cecold.encode_block

        def changing(data, group, dictionary, **kwargs):
            path.write_bytes(b"changed")
            return original(data, group, dictionary, **kwargs)

        with patch.object(cecold, "encode_block", side_effect=changing), self.assertRaises(ValueError):
            self.make_package()
        self.assertFalse(self.archive.exists())

    def test_unreadable_subdirectory_aborts_before_publishing(self):
        folder = self.source / "assets"
        folder.mkdir()
        original = folder / "example.txt"
        original.write_text("example", encoding="utf-8")
        (self.source / "readme.txt").write_text("readme", encoding="utf-8")
        scandir = os.scandir

        def scan(path):
            if Path(path) == folder:
                raise PermissionError(13, "无法读取", path)
            return scandir(path)

        with patch.object(cecold.os, "scandir", side_effect=scan), self.assertRaisesRegex(OSError, "无法读取游戏文件夹"):
            self.make_package()
        self.assertFalse(self.archive.exists())
        self.assertEqual(original.read_text(encoding="utf-8"), "example")

    def test_failed_jpeg_round_trip_keeps_original_bytes(self):
        data = random.Random(17).randbytes(16000)
        (self.source / "example.jpg").write_bytes(data)
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("cjxl.exe", "djxl.exe"):
            (tools / name).touch()

        def wrong_conversion(command, directory, **kwargs):
            Path(command[2]).write_bytes(b"different bytes")

        with patch.object(cecold, "run_codec", side_effect=wrong_conversion):
            result = self.make_package(jxl_bin=tools)
        self.assertEqual(result["jpeg_files"], 0)
        cecold.unpack(self.archive, self.destination)
        self.assertEqual((self.destination / "example.jpg").read_bytes(), data)

    @unittest.skipUnless(os.environ.get("CECOLD_JXL_BIN"), "未指定 JPEG XL 测试工具")
    def test_real_jpeg_recompression_restores_original_file(self):
        from PIL import Image

        image = Image.frombytes("RGB", (512, 512), random.Random(23).randbytes(512 * 512 * 3))
        path = self.source / "example.jpg"
        image.save(path, quality=90)
        original = path.read_bytes()
        result = self.make_package(jxl_bin=os.environ["CECOLD_JXL_BIN"])
        self.assertEqual(result["jpeg_files"], 1)
        with self.assertRaises(ValueError):
            cecold.unpack(self.archive, self.destination)
        cecold.unpack(self.archive, self.destination, jxl_bin=os.environ["CECOLD_JXL_BIN"])
        self.assertEqual((self.destination / "example.jpg").read_bytes(), original)

    def png_fixture(self):
        from PIL import Image

        pixels = bytearray()
        for y in range(128):
            for x in range(128):
                pixels.extend((x, y, (x + y) % 256, 255 if (x + y) % 3 else 0))
        path = self.source / "中文图片.png"
        Image.frombytes("RGBA", (128, 128), bytes(pixels)).save(path, compress_level=0)
        return path

    def fake_png_codec(self, wrong=False, unsafe_name=False):
        originals = {}

        def run(tool, mode, incoming, outgoing, directory, threads, max_output):
            outgoing.mkdir()
            if mode == "a":
                for path in incoming.glob("*.png"):
                    original = path.read_bytes()
                    originals[path.name] = original
                    name = ("../outside.png" if unsafe_name else path.name).encode("ascii")
                    payload = zlib.compress(original)
                    encoded = b"TCIP\x01\0" + struct.pack("<H", len(name)) + name
                    encoded += struct.pack("<QQ", len(original), len(payload)) + payload
                    (outgoing / path.with_suffix(".ppg").name).write_bytes(encoded)
            else:
                for path in incoming.glob("*.ppg"):
                    name = path.with_suffix(".png").name
                    original = originals[name]
                    content = bytes([original[0] ^ 1]) + original[1:] if wrong else original
                    (outgoing / name).write_bytes(content)
            return True

        return run

    def png_tool(self):
        path = self.root / "packPNG.exe"
        path.touch()
        return path

    def test_png_v2_preserves_names_duplicates_and_mtime(self):
        path = self.png_fixture()
        original = path.read_bytes()
        duplicate = self.source / "another.png"
        duplicate.write_bytes(original)
        stamp = 1_600_000_000_123_456_700
        os.utime(path, ns=(stamp, stamp))
        stamp = path.stat().st_mtime_ns
        tool = self.png_tool()
        with patch.object(cecold, "run_png", side_effect=self.fake_png_codec()):
            result = self.make_package(png_tool=tool)
            self.assertEqual(result["version"], 2)
            self.assertEqual(result["png_files"], 1)
            self.assertEqual(cecold.Reader(self.archive).manifest["png_codec"], cecold.PNG_CODEC)
            with self.assertRaisesRegex(ValueError, "PNG 解码工具"):
                cecold.unpack(self.archive, self.destination)
            cecold.unpack(self.archive, self.destination, png_tool=tool)
        restored = self.destination / path.name
        self.assertEqual(restored.read_bytes(), original)
        self.assertEqual(restored.stat().st_mtime_ns, stamp)
        (self.destination / duplicate.name).write_bytes(b"changed")
        self.assertEqual(restored.read_bytes(), original)

    def test_png_failed_round_trip_falls_back_to_original(self):
        path = self.png_fixture()
        original = path.read_bytes()
        with patch.object(cecold, "run_png", side_effect=self.fake_png_codec(wrong=True)):
            result = self.make_package(png_tool=self.png_tool())
        self.assertEqual(result["png_files"], 0)
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["png"]["failed"], 1)
        cecold.unpack(self.archive, self.destination)
        self.assertEqual((self.destination / path.name).read_bytes(), original)

    def test_png_embedded_filename_is_checked_before_decode(self):
        self.png_fixture()
        fake = self.fake_png_codec(unsafe_name=True)
        with patch.object(cecold, "run_png", side_effect=fake) as run:
            result = self.make_package(png_tool=self.png_tool())
        self.assertEqual(result["png_files"], 0)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / "outside.png").exists())

    def test_png_decode_failure_never_publishes_game(self):
        self.png_fixture()
        tool = self.png_tool()
        with patch.object(cecold, "run_png", side_effect=self.fake_png_codec()):
            self.make_package(png_tool=tool)
        with patch.object(cecold, "run_png", return_value=False), self.assertRaises(cecold.FormatError):
            cecold.unpack(self.archive, self.destination, png_tool=tool)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "tmp").exists())

    def test_png_decoded_bytes_are_checked_even_when_size_matches(self):
        path = self.png_fixture()
        original = path.read_bytes()
        tool = self.png_tool()
        with patch.object(cecold, "run_png", side_effect=self.fake_png_codec()):
            self.make_package(png_tool=tool)

        def wrong_output(tool, mode, incoming, outgoing, directory, threads, max_output):
            outgoing.mkdir()
            for encoded in incoming.glob("*.ppg"):
                (outgoing / encoded.with_suffix(".png").name).write_bytes(b"0" * len(original))
            return True

        with patch.object(cecold, "run_png", side_effect=wrong_output), self.assertRaises(cecold.FormatError):
            cecold.unpack(self.archive, self.destination, png_tool=tool)
        self.assertFalse(self.destination.exists())
        self.assertEqual(path.read_bytes(), original)

    def test_png_output_monitor_tolerates_atomic_rename(self):
        directory = self.root / "codec-output"
        directory.mkdir()
        completed = directory / "image.png"
        completed.write_bytes(b"finished")
        vanished = directory / "image.png.tmp"
        with patch.object(Path, "iterdir", return_value=iter([vanished, completed])):
            self.assertEqual(cecold.image_output_size(directory), len(b"finished"))

    def test_mislabeled_and_oversized_png_are_stored_without_conversion(self):
        path = self.png_fixture()
        data = bytearray(path.read_bytes())
        struct.pack_into(">II", data, 16, 8192, 4096)
        path.write_bytes(data)
        (self.source / "mislabeled.png").write_bytes(b"RIFF" + b"x" * 8192)
        before = self.snapshot(self.source)
        with patch.object(cecold, "run_png") as run:
            result = self.make_package(png_tool=self.png_tool())
        run.assert_not_called()
        self.assertEqual(result["png_files"], 0)
        cecold.unpack(self.archive, self.destination)
        self.assertEqual(self.snapshot(self.destination), before)

    def test_png_index_and_version_must_agree(self):
        self.png_fixture()
        with patch.object(cecold, "run_png", side_effect=self.fake_png_codec()):
            self.make_package(png_tool=self.png_tool())
        good = self.archive.read_bytes()
        for change in (lambda x: x.update(version=1), lambda x: x.update(png_codec="unknown"),
                       lambda x: x["objects"][0].update(png_pixels=cecold.MAX_PNG_PIXELS)):
            self.archive.write_bytes(good)
            self.rewrite_index(change)
            with self.assertRaises(cecold.FormatError):
                cecold.Reader(self.archive)

    def test_small_blocks_decode_with_the_declared_dictionary_limit(self):
        data = random.Random(42).randbytes(2048) * 50
        for effort in ("compact", "balanced", "fast"):
            with self.subTest(effort=effort):
                (self.source / "small.bin").write_bytes(data)
                self.make_package(effort=effort)
                cecold.unpack(self.archive, self.destination)
                self.assertEqual((self.destination / "small.bin").read_bytes(), data)
                self.archive.unlink()
                (self.destination / "small.bin").unlink()
                self.destination.rmdir()

    def test_incompressible_large_block_is_stored_exactly(self):
        data = random.Random(73).randbytes(8 * 1024**2)
        codec, payload, checksum = cecold.encode_block(data, "jxl", 16 * 1024**2)
        self.assertEqual(codec, "copy")
        self.assertEqual(payload, data)
        self.assertEqual(checksum, hashlib.sha256(data).hexdigest())

    def test_low_density_png_can_use_compact_profile(self):
        path = self.png_fixture()
        data = bytearray(path.read_bytes())
        struct.pack_into(">II", data, 16, 4096, 4096)
        path.write_bytes(data)
        with patch.object(cecold, "run_png") as run:
            result = self.make_package(png_tool=self.png_tool())
        run.assert_not_called()
        self.assertEqual(result["png"]["skipped_low_density"], 1)
        self.archive.unlink()
        with patch.object(cecold, "run_png", side_effect=self.fake_png_codec()):
            result = self.make_package(png_tool=self.root / "packPNG.exe", effort="compact")
        self.assertEqual(result["png_files"], 1)

    @unittest.skipUnless(os.environ.get("CECOLD_PNG_TOOL"), "未指定 PNG 测试工具")
    def test_real_png_recompression_and_corruption_detection(self):
        path = self.png_fixture()
        original = path.read_bytes()
        tool = os.environ["CECOLD_PNG_TOOL"]
        temporary = self.root / "tmp" / "中文临时目录"
        result = self.make_package(png_tool=tool, temp_dir=temporary)
        self.assertEqual(result["png_files"], 1)
        self.assertEqual(result["version"], 2)
        self.assertFalse((self.root / "tmp").exists())
        cecold.unpack(self.archive, self.destination, png_tool=tool, temp_dir=temporary)
        self.assertEqual((self.destination / path.name).read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)
        damaged = bytearray(self.archive.read_bytes())
        damaged[cecold.HEADER.size + 5] ^= 1
        self.archive.write_bytes(damaged)
        other = self.root / "damaged-output"
        with self.assertRaises(cecold.FormatError):
            cecold.unpack(self.archive, other, png_tool=tool)
        self.assertFalse(other.exists())
