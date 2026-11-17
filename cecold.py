"""独立游戏冷藏包：分块去重、资源分组和可逆 PNG / JPEG 重压缩。

文件布局为固定头、数据块、压缩后的 JSON 索引、带索引校验值的固定尾。
压缩和解压不依赖 7-Zip；图片工具只在包使用对应转换时需要。
"""
import argparse
import hashlib
import json
import lzma
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath

import psutil

ROOT = Path(__file__).resolve().parent
HEADER = struct.Struct("<8sII")
FOOTER = struct.Struct("<8sQQ32s")
MAGIC = b"CECOLD\x00\x01"
END_MAGIC = b"CEINDEX1"
MAX_INDEX = 64 * 1024**2
MAX_BLOCK = 128 * 1024**2
MAX_FILES = 500_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_CODEC = "packpng-2-tcip-1"
MAX_PNG_PIXELS = 32 * 1024**2
PNG_BATCH_PIXELS = 256 * 1024**2
PNG_ACTIVE_PIXELS = 128 * 1024**2
PNG_BATCH_FILES = 256
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class FormatError(ValueError):
    pass


def digest_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def publish_ready(source, destination):
    deadline = time.monotonic() + 10
    while True:
        if destination.exists():
            raise FileExistsError("输出位置已经存在，原内容未覆盖")
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            # Windows 的扫描程序可能短暂占用刚写好的文件，稍等后再交付完整目录。
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def data_chunks(stream, maximum):
    """在二进制数据的内容边界处分段，使插入少量数据后仍能找到相同片段。"""
    minimum = 64 * 1024
    if maximum < minimum * 2:
        while data := stream.read(maximum):
            yield data
        return
    buffer = bytearray()
    while True:
        incoming = stream.read(4 * 1024**2)
        buffer.extend(incoming)
        start = 0
        while len(buffer) - start >= maximum or (not incoming and start < len(buffer)):
            stop = min(start + maximum, len(buffer))
            anchor = buffer.find(b"\0\0", start + minimum, stop)
            end = anchor + 2 if anchor >= 0 else stop
            yield bytes(buffer[start:end])
            start = end
        if start:
            del buffer[:start]
        if not incoming:
            break


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 4096 or "\\" in name:
        raise FormatError("包内文件名不正确")
    parts = PurePosixPath(name).parts
    if (name.startswith("/") or not parts or "/".join(parts) != name or
            any(p in (".", "..") or p.endswith((".", " ")) or
                any(c in p for c in ':<>"|?*\0') or any(ord(c) < 32 for c in p) or
                p.split(".")[0].casefold() in RESERVED for p in parts)):
        raise FormatError("包内含不安全的文件路径")
    return name


@contextmanager
def workspace(base=None):
    boundary = (ROOT / "tmp").resolve()
    base = Path(base).resolve() if base is not None else boundary
    if not base.is_relative_to(boundary):
        raise ValueError("临时目录必须放在项目 tmp 内")
    created = []
    parent = base
    while parent.is_relative_to(boundary) and not parent.exists():
        created.append(parent)
        parent = parent.parent
    base.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="cecold-", dir=base) as directory:
            resolved = Path(directory).resolve()
            if not resolved.is_relative_to(base):
                raise ValueError("临时目录超出项目范围")
            yield resolved
    finally:
        for directory in created:
            try:
                directory.rmdir()
            except OSError:
                break


def inventory(source, threads):
    def unreadable_directory(error):
        raise OSError(f"无法读取游戏文件夹：{error.filename or source}") from error

    paths, directories = [], []
    for parent, folders, names in os.walk(source, onerror=unreadable_directory, followlinks=False):
        folders.sort()
        for name in sorted([*folders, *names]):
            path = Path(parent) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("游戏目录含链接文件，请先将它复制为普通文件")
            relative = safe_name(path.relative_to(source).as_posix())
            if path.is_dir():
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode):
                paths.append((path, relative, info))
            else:
                raise ValueError("游戏目录含不支持的特殊文件")
    if len(paths) + len(directories) > MAX_FILES:
        raise ValueError("游戏文件数量超过限制")

    def read(item):
        path, relative, before = item
        checksum = digest_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("游戏文件正在变化，请关闭游戏后再冷藏")
        return {"source": path, "path": relative, "size": before.st_size,
                "mtime_ns": before.st_mtime_ns, "sha256": checksum}

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return list(pool.map(read, paths)), directories


def codec_tools(folder):
    if folder is None:
        return None
    folder = Path(folder).resolve()
    names = ("cjxl.exe", "djxl.exe") if os.name == "nt" else ("cjxl", "djxl")
    paths = tuple(folder / name for name in names)
    if not all(p.is_file() for p in paths):
        raise ValueError("没找到 JPEG XL 的编码和解码工具")
    return paths


def run_codec(command, directory, max_output=MAX_BLOCK):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    output = Path(command[2])
    with subprocess.Popen([str(part) for part in command], cwd=directory,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=flags, env=dict(os.environ, TEMP=str(directory), TMP=str(directory))) as child:
        try:
            process = psutil.Process(child.pid)
        except psutil.NoSuchProcess:
            process = None
        try:
            while child.poll() is None:
                try:
                    memory = process.memory_info().rss if process is not None else 0
                except psutil.Error:
                    memory = 0
                if (time.monotonic() - started > 120 or memory > 2 * 1024**3 or
                        (output.exists() and output.stat().st_size > max_output)):
                    raise ValueError("图片转换超出时间、内存或文件大小限制")
                time.sleep(0.02)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    if child.returncode or not output.is_file() or output.stat().st_size > max_output:
        raise ValueError("JPEG XL 转换失败")


def transform_jpeg(item, number, tools, work):
    if tools is None or item["source"].suffix.lower() not in (".jpg", ".jpeg") or not 4096 <= item["size"] <= MAX_BLOCK:
        return None
    directory = work / f"jpeg-{number}"
    directory.mkdir()
    encoded, restored = directory / "image.jxl", directory / "restored.jpg"
    selected = False
    try:
        run_codec([tools[0], item["source"], encoded, "--lossless_jpeg=1", "--allow_jpeg_reconstruction=1",
                   "--effort=7", "--num_threads=1"], directory, max_output=item["size"])
        if encoded.stat().st_size >= item["size"]:
            return None
        run_codec([tools[1], encoded, restored, "--reconstruct_jpeg", "--num_threads=1"], directory, max_output=item["size"])
        # 图片像素相同还不够，游戏文件和原有校验工具需要原文件的字节完全一致。
        if restored.stat().st_size != item["size"] or digest_file(restored) != item["sha256"]:
            return None
        selected = True
        return encoded
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        restored.unlink(missing_ok=True)
        if not selected:
            encoded.unlink(missing_ok=True)


def png_tool_path(tool):
    if tool is None:
        return None
    path = Path(tool).resolve()
    if not path.is_file():
        raise ValueError("没找到 PNG 编码和解码工具")
    return path


def png_name(index):
    return f"image_{index:08}.png"


def png_pixels(item):
    if item["source"].suffix.lower() != ".png" or not 4096 <= item["size"] <= MAX_BLOCK:
        return None
    with item["source"].open("rb") as source:
        head = source.read(33)
    if len(head) != 33 or head[:8] != PNG_MAGIC or head[8:16] != b"\0\0\0\rIHDR":
        return None
    width, height = struct.unpack_from(">II", head, 16)
    pixels = width * height
    # 限制展开后的图片大小，避免小 PNG 占满内存，也跳过已实测无法还原的大图。
    if not width or not height or pixels >= MAX_PNG_PIXELS:
        return None
    return pixels


def png_batches(records):
    batch, pixels, size = [], 0, 0
    for record in records:
        if batch and (len(batch) >= PNG_BATCH_FILES or pixels + record[2] > PNG_BATCH_PIXELS or
                      size + record[1]["size"] > 2 * MAX_BLOCK):
            yield batch
            batch, pixels, size = [], 0, 0
        batch.append(record)
        pixels += record[2]
        size += record[1]["size"]
    if batch:
        yield batch


def validate_png_payload(path, name, original_size):
    size = path.stat().st_size
    if not 25 <= size <= MAX_BLOCK:
        raise FormatError("PNG 压缩数据大小不正确")
    with path.open("rb") as source:
        header = source.read(8)
        if len(header) != 8 or header[:5] != b"TCIP\x01" or header[5] not in (0, 1):
            raise FormatError("不支持这个 PNG 压缩数据版本")
        length = struct.unpack_from("<H", header, 6)[0]
        if length != len(name.encode("ascii")) or source.read(length) != name.encode("ascii"):
            raise FormatError("PNG 压缩数据的文件名不正确")
        sizes = source.read(16)
        if len(sizes) != 16:
            raise FormatError("PNG 压缩数据不完整")
        restored_size, stored_size = struct.unpack("<QQ", sizes)
        if restored_size != original_size or stored_size != size - 24 - length or stored_size == 0:
            raise FormatError("PNG 压缩数据的长度不正确")


def png_workers(pixels, maximum):
    total = count = 0
    for size in sorted(pixels, reverse=True)[:maximum]:
        if count and total + size > PNG_ACTIVE_PIXELS:
            break
        total += size
        count += 1
    return max(1, count)


def image_output_size(directory):
    total = 0
    for path in directory.iterdir():
        try:
            info = path.stat()
        except FileNotFoundError:
            # 编解码器先写 .tmp 再重命名，采样时消失的临时文件不代表转换失败。
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def run_png(tool, mode, incoming, outgoing, directory, threads, max_output):
    outgoing.mkdir()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [str(tool), mode, "-r", "-np", "--no-color", f"-th{threads}",
               "-od" + str(outgoing), str(incoming)]
    started = time.monotonic()
    with subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                          env=dict(os.environ, TEMP=str(directory), TMP=str(directory))) as child:
        try:
            process = psutil.Process(child.pid)
        except psutil.NoSuchProcess:
            process = None
        limit = False
        next_disk_sample = 0
        total = 0
        try:
            while child.poll() is None:
                try:
                    memory = process.memory_info().rss if process else 0
                except psutil.Error:
                    memory = 0
                elapsed = time.monotonic() - started
                if elapsed >= next_disk_sample:
                    total = image_output_size(outgoing)
                    next_disk_sample = elapsed + 0.25
                if elapsed > 120 or memory > 8 * 1024**3 or total > max_output:
                    limit = True
                    break
                time.sleep(0.05)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
    total = image_output_size(outgoing)
    return not limit and child.returncode == 0 and total <= max_output


def transform_pngs(items, tool, work, threads, notify, effort="balanced"):
    result = {}
    stats = {"candidates": 0, "converted": 0, "not_smaller": 0, "failed": 0,
             "skipped": 0, "skipped_low_density": 0, "source_bytes": 0, "encoded_bytes": 0}
    if tool is None:
        return result, stats
    started = time.perf_counter()
    records = []
    for index, item in enumerate(items):
        pixels = png_pixels(item)
        if pixels is not None:
            divisor = {"balanced": 100, "fast": 50}.get(effort)
            # 大画布但已很小的 PNG 收益有限，展开全部像素会占掉冷藏时间。
            if divisor is not None and item["size"] * divisor < pixels:
                stats["skipped"] += 1
                stats["skipped_low_density"] += 1
                continue
            item["png_pixels"] = pixels
            records.append((index, item, pixels))
        elif item["source"].suffix.lower() == ".png":
            stats["skipped"] += 1
    stats["candidates"] = len(records)
    processed = 0
    selected = work / "png-selected"
    selected.mkdir()
    for batch in png_batches(records):
        with tempfile.TemporaryDirectory(prefix="png-batch-", dir=work) as temporary:
            directory = Path(temporary)
            incoming, encoded, decoded = directory / "input", directory / "encoded", directory / "decoded"
            incoming.mkdir()
            original_bytes = sum(item["size"] for _, item, _ in batch)
            workers = png_workers((pixels for _, _, pixels in batch), threads)
            for index, item, _ in batch:
                path = incoming / png_name(index)
                shutil.copyfile(item["source"], path)
                if path.stat().st_size != item["size"] or digest_file(path) != item["sha256"]:
                    raise ValueError("游戏图片正在变化，请关闭游戏后再冷藏")
            try:
                run_png(tool, "a", incoming, encoded, directory, workers, original_bytes * 2 + 1024**2)
            except OSError:
                stats["failed"] += len(batch)
                continue
            valid = []
            decode_input = directory / "decode-input"
            decode_input.mkdir()
            for index, item, pixels in batch:
                path = encoded / Path(png_name(index)).with_suffix(".ppg")
                try:
                    validate_png_payload(path, png_name(index), item["size"])
                    if path.stat().st_size >= item["size"]:
                        stats["not_smaller"] += 1
                        path.unlink()
                    else:
                        os.rename(path, decode_input / path.name)
                        valid.append((index, item, pixels))
                except (OSError, FormatError):
                    stats["failed"] += 1
                    path.unlink(missing_ok=True)
            if valid:
                try:
                    run_png(tool, "x", decode_input, decoded, directory, workers,
                            sum(item["size"] for _, item, _ in valid))
                except OSError:
                    pass
            for index, item, _ in valid:
                restored = decoded / png_name(index)
                if (not restored.is_file() or restored.stat().st_size != item["size"] or
                        digest_file(restored) != item["sha256"]):
                    stats["failed"] += 1
                    continue
                path = decode_input / Path(png_name(index)).with_suffix(".ppg")
                destination = selected / path.name
                os.rename(path, destination)
                result[index] = destination
                stats["converted"] += 1
                stats["source_bytes"] += item["size"]
                stats["encoded_bytes"] += destination.stat().st_size
        processed += len(batch)
        notify(f"处理 PNG：{processed}/{len(records)}")
    stats["seconds"] = round(time.perf_counter() - started, 3)
    return result, stats


def compression_group(item, transformed):
    if transformed != "none":
        return "jxl" if transformed == "jpeg-xl" else "png"
    with item["source"].open("rb") as source:
        head = source.read(64)
        if head.startswith(b"MZ") and len(head) >= 64:
            offset = struct.unpack_from("<I", head, 60)[0]
            if offset < item["size"] - 6:
                source.seek(offset)
                pe = source.read(6)
                if pe[:4] == b"PE\0\0" and int.from_bytes(pe[4:], "little") in (0x14c, 0x8664):
                    return "x86"
        if head.startswith(b"\x7fELF") and len(head) >= 20 and head[5] == 1 and int.from_bytes(head[18:20], "little") in (3, 62):
            return "x86"
    return "raw:" + item["source"].suffix.lower()


def filters(dictionary, x86=False, effort=None):
    options = {"id": lzma.FILTER_LZMA2, "dict_size": dictionary}
    if effort is not None:
        options.update(preset=6, nice_len=273 if effort == "compact" else 64)
        if effort == "balanced":
            options.update(mode=lzma.MODE_FAST, mf=lzma.MF_HC4, depth=32)
        elif effort == "fast":
            options.update(mode=lzma.MODE_FAST, mf=lzma.MF_HC4, nice_len=32, depth=16)
    return ([{"id": lzma.FILTER_X86}] if x86 else []) + [options]


def encode_block(data, group, dictionary, *, effort="balanced"):
    checksum = hashlib.sha256(data).hexdigest()
    if effort != "compact" and group != "x86" and len(data) >= 8 * 1024**2:
        width = 512 * 1024
        middle = (len(data) - width) // 2
        sample = data[:width] + data[middle:middle + width] + data[-width:]
        probe = lzma.compress(sample, format=lzma.FORMAT_RAW,
                              filters=filters(2 * 1024**2, effort="fast"))
        minimum_gain = 0.005 if effort == "balanced" else (0.05 if group in ("png", "jxl") else 0.01)
        # 重复内容已先去重；对近乎不可压的数据块继续搜索只会耗时。
        if len(probe) >= len(sample) * (1 - minimum_gain):
            return "copy", data, checksum
    # 一个独立块不可能引用块外数据，小块不必分配整份大字典。
    effective_dictionary = min(dictionary, max(4096, 1 << (len(data) - 1).bit_length()))
    encoded = lzma.compress(data, format=lzma.FORMAT_RAW,
                            filters=filters(effective_dictionary, group == "x86", effort))
    if len(encoded) >= len(data):
        return "copy", data, checksum
    return "lzma2-x86" if group == "x86" else "lzma2", encoded, checksum


def pack(source, destination, *, jxl_bin=None, png_tool=None, threads=4, image_threads=16,
         block_size=64 * 1024**2, chunk_size=1024**2, dictionary=64 * 1024**2,
         effort="balanced", progress=None, temp_dir=None):
    started = time.perf_counter()
    phase_times = {}
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir() or destination.exists() or destination.is_relative_to(source):
        raise ValueError("请选择游戏目录，并在目录外指定一个尚不存在的冷藏包")
    if not 1 <= threads <= 8 or not 4096 <= chunk_size <= block_size <= MAX_BLOCK or not 1024**2 <= dictionary <= MAX_BLOCK:
        raise ValueError("压缩参数超出范围")
    if not 1 <= image_threads <= 16 or effort not in ("compact", "balanced", "fast"):
        raise ValueError("图片并行数或压缩档位不正确")
    tools = codec_tools(jxl_bin)
    png = png_tool_path(png_tool)
    notify = progress or (lambda message: None)
    with workspace(temp_dir) as work:
        if work.resolve().is_relative_to(source):
            raise ValueError("不能把包含临时目录的整个项目作为游戏冷藏")
        notify("检查文件")
        files, directories = inventory(source, threads)
        phase_times["inventory"] = round(time.perf_counter() - started, 3)
        unique, by_hash = [], {}
        for item in files:
            key = (item["size"], item["sha256"])
            if key not in by_hash:
                by_hash[key] = len(unique)
                unique.append(item)
            item["object"] = by_hash[key]
        notify("处理图片")
        image_started = time.perf_counter()
        transformed = [None] * len(unique)
        jpeg_jobs = [(i, item) for i, item in enumerate(unique)
                     if tools and item["source"].suffix.lower() in (".jpg", ".jpeg")]
        last_notice = time.monotonic()
        with ThreadPoolExecutor(max_workers=threads) as pool:
            jobs = pool.map(lambda pair: (pair[0], transform_jpeg(pair[1], pair[0], tools, work)), jpeg_jobs)
            for count, (index, path) in enumerate(jobs, 1):
                transformed[index] = path
                if time.monotonic() - last_notice >= 2:
                    notify(f"处理 JPEG：{count}/{len(jpeg_jobs)}")
                    last_notice = time.monotonic()
        phase_times["jpeg"] = round(time.perf_counter() - image_started, 3)
        transforms = ["jpeg-xl" if path is not None else "none" for path in transformed]
        pngs, png_stats = transform_pngs(unique, png, work, image_threads, notify, effort)
        phase_times["png"] = png_stats.get("seconds", 0)
        data_started = time.perf_counter()
        for index, path in pngs.items():
            transformed[index], transforms[index] = path, "png-pack"
        version = 2 if pngs else 1
        groups = [compression_group(item, transforms[i]) for i, item in enumerate(unique)]
        order = sorted(range(len(unique)), key=lambda i: (groups[i], unique[i]["path"].casefold()))
        blocks, chunks, chunk_ids, objects = [], [], {}, [None] * len(unique)
        pending, buffer = deque(), bytearray()
        buffer_group = None
        package = work / "package.cecold"
        total_work = sum(path.stat().st_size if path else item["size"] for path, item in zip(transformed, unique))
        completed_work = 0
        last_notice = time.monotonic()
        notify("压缩数据")
        with package.open("wb") as output, ThreadPoolExecutor(max_workers=threads) as pool:
            output.write(HEADER.pack(MAGIC, version, 0))

            def write_pending():
                index, raw_size, future = pending.popleft()
                codec, payload, checksum = future.result()
                blocks[index] = {"offset": output.tell(), "stored": len(payload), "size": raw_size,
                                 "codec": codec, "sha256": checksum}
                output.write(payload)

            def flush():
                if not buffer:
                    return
                index = len(blocks)
                blocks.append(None)
                data = bytes(buffer)
                buffer.clear()
                pending.append((index, len(data), pool.submit(encode_block, data, buffer_group, dictionary, effort=effort)))
                if len(pending) >= threads:
                    write_pending()

            for index in order:
                item, converted, group = unique[index], transformed[index], groups[index]
                if group != buffer_group:
                    flush()
                    buffer_group = group
                stream_path = converted or item["source"]
                checked, size, references = hashlib.sha256(), 0, []
                with stream_path.open("rb") as stream:
                    for data in data_chunks(stream, chunk_size):
                        checked.update(data)
                        size += len(data)
                        completed_work += len(data)
                        checksum = hashlib.sha256(data).hexdigest()
                        key = (len(data), checksum)
                        if key not in chunk_ids:
                            if data.count(0) == len(data):
                                chunk = {"size": len(data), "sha256": checksum, "block": None}
                            else:
                                if len(buffer) + len(data) > block_size:
                                    flush()
                                chunk = {"size": len(data), "sha256": checksum, "block": len(blocks), "offset": len(buffer)}
                                buffer.extend(data)
                            chunk_ids[key] = len(chunks)
                            chunks.append(chunk)
                        references.append(chunk_ids[key])
                        if time.monotonic() - last_notice >= 2:
                            notify(f"压缩数据：{100 * completed_work / max(1, total_work):.1f}%")
                            last_notice = time.monotonic()
                if not converted and (size != item["size"] or checked.hexdigest() != item["sha256"]):
                    raise ValueError("游戏文件正在变化，请关闭游戏后再冷藏")
                objects[index] = {"size": item["size"], "sha256": item["sha256"], "chunks": references,
                                  "transform": transforms[index], "encoded_size": size,
                                  "encoded_sha256": checked.hexdigest()}
                if transforms[index] == "png-pack":
                    objects[index]["png_pixels"] = item["png_pixels"]
                if converted:
                    converted.unlink()
            flush()
            while pending:
                write_pending()
            for item in files:
                current = item["source"].stat()
                if (current.st_size, current.st_mtime_ns) != (item["size"], item["mtime_ns"]):
                    raise ValueError("游戏文件正在变化，请关闭游戏后再冷藏")
            manifest = {"version": version, "dictionary": dictionary, "chunk_size": chunk_size,
                        "chunker": "zero-anchor-v1",
                        "files": [{k: item[k] for k in ("path", "object", "mtime_ns")} for item in files],
                        "directories": directories, "objects": objects, "chunks": chunks, "blocks": blocks,
                        "object_order": order}
            if pngs:
                manifest["png_codec"] = PNG_CODEC
            raw_index = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw_index) > MAX_INDEX:
                raise ValueError("游戏文件清单过大")
            index = zlib.compress(raw_index, 9)
            offset = output.tell()
            output.write(index)
            output.write(FOOTER.pack(END_MAGIC, offset, len(index), hashlib.sha256(index).digest()))
            output.flush()
            os.fsync(output.fileno())
        # 完整包才出现在目标位置，失败时来源文件保持原样。
        Reader(package)
        destination.parent.mkdir(parents=True, exist_ok=True)
        publish_ready(package, destination)
        phase_times["data_and_index"] = round(time.perf_counter() - data_started, 3)
        return {"files": len(files), "source_bytes": sum(item["size"] for item in files),
                "archive_bytes": destination.stat().st_size, "unique_files": len(unique),
                "jpeg_files": transforms.count("jpeg-xl"), "png_files": len(pngs), "png": png_stats,
                "version": version, "effort": effort, "image_threads": image_threads, "chunks": len(chunks),
                "phase_seconds": phase_times, "copy_blocks": sum(b["codec"] == "copy" for b in blocks),
                "zero_chunks": sum(c["block"] is None for c in chunks),
                "duplicate_file_bytes": sum(item["size"] for item in files) - sum(item["size"] for item in unique),
                "duplicate_chunk_bytes": sum(obj["encoded_size"] for obj in objects) - sum(c["size"] for c in chunks),
                "seconds": round(time.perf_counter() - started, 3)}


def integer(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise FormatError("冷藏包中的长度或编号不正确")
    return value


def checksum(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise FormatError("冷藏包中的校验值不正确")


class Reader:
    def __init__(self, path):
        self.path = Path(path)
        size = self.path.stat().st_size
        if size < HEADER.size + FOOTER.size:
            raise FormatError("冷藏包不完整")
        with self.path.open("rb") as source:
            magic, self.version, flags = HEADER.unpack(source.read(HEADER.size))
            if magic != MAGIC or self.version not in (1, 2) or flags != 0:
                raise FormatError("不支持这种冷藏包")
            source.seek(-FOOTER.size, os.SEEK_END)
            magic, offset, length, expected = FOOTER.unpack(source.read(FOOTER.size))
            if magic != END_MAGIC or not HEADER.size <= offset < size - FOOTER.size or not 0 < length <= MAX_INDEX or offset + length + FOOTER.size != size:
                raise FormatError("冷藏包索引不完整")
            source.seek(offset)
            encoded = source.read(length)
            if hashlib.sha256(encoded).digest() != expected:
                raise FormatError("冷藏包索引校验失败")
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(encoded, MAX_INDEX + 1)
            if len(raw) > MAX_INDEX or not decoder.eof or decoder.unused_data:
                raise FormatError("冷藏包索引过大或不完整")
            self.manifest = json.loads(raw)
            self.validate(offset)
        except (KeyError, TypeError, ValueError, zlib.error) as error:
            raise FormatError("冷藏包索引格式不正确") from error

    def validate(self, index_offset):
        data = self.manifest
        if type(data["version"]) is not int or data["version"] != self.version:
            raise FormatError("不支持这种冷藏包版本")
        if self.version == 2 and data.get("png_codec") != PNG_CODEC:
            raise FormatError("不支持这个 PNG 压缩方法")
        integer(data["dictionary"], MAX_BLOCK, 1024**2)
        integer(data["chunk_size"], MAX_BLOCK, 4096)
        for key in ("files", "directories", "objects", "chunks", "blocks", "object_order"):
            if not isinstance(data[key], list) or len(data[key]) > MAX_FILES:
                raise FormatError("冷藏包记录过多")
        if len(data["files"]) + len(data["directories"]) > MAX_FILES:
            raise FormatError("冷藏包文件过多")
        names, file_names = set(), set()
        for name in data["directories"]:
            canonical = safe_name(name).casefold()
            if canonical in names:
                raise FormatError("冷藏包路径重复")
            names.add(canonical)
        for file in data["files"]:
            canonical = safe_name(file["path"]).casefold()
            if canonical in names:
                raise FormatError("冷藏包路径重复")
            names.add(canonical)
            file_names.add(canonical)
            integer(file["object"], len(data["objects"]) - 1)
            integer(file["mtime_ns"], 2**63 - 1, -(2**63))
        for name in names:
            if any(parent.as_posix() in file_names for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
                raise FormatError("冷藏包路径相互冲突")
        cursor = HEADER.size
        for block in data["blocks"]:
            integer(block["offset"], index_offset)
            if block["offset"] != cursor or block["codec"] not in ("copy", "lzma2", "lzma2-x86"):
                raise FormatError("冷藏包数据块不正确")
            integer(block["size"], MAX_BLOCK, 1)
            integer(block["stored"], MAX_BLOCK, 1)
            checksum(block["sha256"])
            cursor += block["stored"]
        if cursor != index_offset:
            raise FormatError("冷藏包数据块范围不正确")
        for chunk in data["chunks"]:
            integer(chunk["size"], data["chunk_size"], 1)
            checksum(chunk["sha256"])
            if chunk["block"] is not None:
                block = data["blocks"][integer(chunk["block"], len(data["blocks"]) - 1)]
                integer(chunk["offset"], block["size"] - chunk["size"])
        for obj in data["objects"]:
            integer(obj["size"], 2**63 - 1)
            integer(obj["encoded_size"], 2**63 - 1)
            checksum(obj["sha256"])
            checksum(obj["encoded_sha256"])
            allowed = ("none", "jpeg-xl", "png-pack") if self.version == 2 else ("none", "jpeg-xl")
            if obj["transform"] not in allowed or not isinstance(obj["chunks"], list) or len(obj["chunks"]) > MAX_FILES:
                raise FormatError("冷藏包文件数据不正确")
            total = sum(data["chunks"][integer(i, len(data["chunks"]) - 1)]["size"] for i in obj["chunks"])
            if total != obj["encoded_size"] or (obj["transform"] == "none" and obj["size"] != total):
                raise FormatError("冷藏包文件长度不一致")
            if obj["transform"] == "none" and obj["sha256"] != obj["encoded_sha256"]:
                raise FormatError("冷藏包文件校验值不一致")
            if obj["transform"] in ("jpeg-xl", "png-pack") and max(total, obj["size"]) > MAX_BLOCK:
                raise FormatError("冷藏包中的图片过大")
            if obj["transform"] == "png-pack":
                integer(obj["png_pixels"], MAX_PNG_PIXELS - 1, 1)
                if total >= obj["size"] or total < 25:
                    raise FormatError("冷藏包中的 PNG 长度不正确")
        if any(type(i) is not int for i in data["object_order"]) or sorted(data["object_order"]) != list(range(len(data["objects"]))):
            raise FormatError("冷藏包文件顺序不正确")
        if {f["object"] for f in data["files"]} != set(range(len(data["objects"]))):
            raise FormatError("冷藏包文件引用不完整")

    @lru_cache(maxsize=2)
    def block(self, index):
        block = self.manifest["blocks"][index]
        with self.path.open("rb") as source:
            source.seek(block["offset"])
            encoded = source.read(block["stored"])
        try:
            if block["codec"] == "copy":
                raw = encoded
            else:
                decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
                                              filters=filters(self.manifest["dictionary"], block["codec"] == "lzma2-x86"))
                raw = decoder.decompress(encoded, max_length=block["size"] + 1)
                if not decoder.eof or decoder.unused_data:
                    raise FormatError("冷藏包数据块不完整")
        except lzma.LZMAError as error:
            raise FormatError("冷藏包数据损坏") from error
        if len(raw) != block["size"] or hashlib.sha256(raw).hexdigest() != block["sha256"]:
            raise FormatError("冷藏包数据块校验失败")
        return raw

    def write_object(self, obj, destination):
        checked = hashlib.sha256()
        with destination.open("xb") as output:
            for index in obj["chunks"]:
                chunk = self.manifest["chunks"][index]
                raw = (b"\0" * chunk["size"] if chunk["block"] is None else
                       self.block(chunk["block"])[chunk["offset"]:chunk["offset"] + chunk["size"]])
                if hashlib.sha256(raw).hexdigest() != chunk["sha256"]:
                    raise FormatError("冷藏包数据片段校验失败")
                output.write(raw)
                checked.update(raw)
        if destination.stat().st_size != obj["encoded_size"] or checked.hexdigest() != obj["encoded_sha256"]:
            raise FormatError("冷藏包文件校验失败")


def finish_object(staging, obj, entries):
    first = staging / entries[0]["path"]
    for file in entries[1:]:
        target = staging / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        # 重复文件恢复成独立副本，避免游戏改一个文件时连另一个也被改动。
        shutil.copyfile(first, target)
        if digest_file(target) != obj["sha256"]:
            raise FormatError("重复文件没有完整还原")
    for file in entries:
        os.utime(staging / file["path"], ns=(file["mtime_ns"], file["mtime_ns"]))


def restore_jpeg(obj, entries, tools, directory, staging):
    encoded, decoded = directory / "image.jxl", directory / "image.jpg"
    run_codec([tools[1], encoded, decoded, "--reconstruct_jpeg", "--num_threads=1"], directory, max_output=obj["size"])
    if decoded.stat().st_size != obj["size"] or digest_file(decoded) != obj["sha256"]:
        raise FormatError("图片没有还原为原始文件")
    os.rename(decoded, staging / entries[0]["path"])
    finish_object(staging, obj, entries)
    encoded.unlink()
    directory.rmdir()


def unpack(archive, destination, *, jxl_bin=None, png_tool=None, image_threads=16, temp_dir=None):
    started = time.perf_counter()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("解冻目录已经存在，请选择新目录")
    reader = Reader(archive)
    tools = codec_tools(jxl_bin)
    png = png_tool_path(png_tool)
    if not 1 <= image_threads <= 16:
        raise ValueError("图片并行数超出范围")
    if any(obj["transform"] == "jpeg-xl" for obj in reader.manifest["objects"]) and tools is None:
        raise ValueError("这个冷藏包需要 JPEG XL 解码工具")
    if any(obj["transform"] == "png-pack" for obj in reader.manifest["objects"]) and png is None:
        raise ValueError("这个冷藏包需要 PNG 解码工具，请用 --png-tool 指定")
    required = sum(reader.manifest["objects"][file["object"]]["size"] for file in reader.manifest["files"])
    if required + MAX_BLOCK > shutil.disk_usage(ROOT).free:
        raise ValueError("项目所在磁盘空间不够解冻这个游戏")
    try:
        with workspace(temp_dir) as work, ThreadPoolExecutor(max_workers=min(4, image_threads)) as pool:
            staging = work / "game"
            staging.mkdir()
            for directory in reader.manifest["directories"]:
                (staging / directory).mkdir(parents=True, exist_ok=True)
            destinations = defaultdict(list)
            for file in reader.manifest["files"]:
                destinations[file["object"]].append(file)
            jpeg_pending = deque()
            png_pending = []
            png_pixels_total = png_bytes_total = 0
            png_directory = work / "png-decode"

            def flush_jpeg():
                while jpeg_pending:
                    jpeg_pending.popleft().result()

            def flush_png():
                nonlocal png_pixels_total, png_bytes_total
                if not png_pending:
                    return
                incoming, decoded = png_directory / "input", png_directory / "decoded"
                workers = png_workers((obj["png_pixels"] for _, obj, _ in png_pending), image_threads)
                if not run_png(png, "x", incoming, decoded, png_directory, workers, png_bytes_total):
                    raise FormatError("PNG 解码失败，未交付不完整的游戏")
                expected = {png_name(index) for index, _, _ in png_pending}
                if {p.name for p in decoded.iterdir()} != expected:
                    raise FormatError("PNG 还原后的文件清单不一致")
                for index, obj, entries in png_pending:
                    path = decoded / png_name(index)
                    if path.stat().st_size != obj["size"] or digest_file(path) != obj["sha256"]:
                        raise FormatError("PNG 没有还原为原始文件")
                    os.rename(path, staging / entries[0]["path"])
                    finish_object(staging, obj, entries)
                    (incoming / Path(png_name(index)).with_suffix(".ppg")).unlink()
                decoded.rmdir()
                incoming.rmdir()
                png_directory.rmdir()
                png_pending.clear()
                png_pixels_total = png_bytes_total = 0

            for index in reader.manifest["object_order"]:
                obj = reader.manifest["objects"][index]
                entries = destinations[index]
                first = staging / entries[0]["path"]
                first.parent.mkdir(parents=True, exist_ok=True)
                if obj["transform"] == "jpeg-xl":
                    flush_png()
                    directory = work / f"jpeg-{index}"
                    directory.mkdir()
                    reader.write_object(obj, directory / "image.jxl")
                    jpeg_pending.append(pool.submit(restore_jpeg, obj, entries, tools, directory, staging))
                    if len(jpeg_pending) >= min(4, image_threads):
                        jpeg_pending.popleft().result()
                elif obj["transform"] == "png-pack":
                    flush_jpeg()
                    if png_pending and (len(png_pending) >= PNG_BATCH_FILES or
                                        png_pixels_total + obj["png_pixels"] > PNG_BATCH_PIXELS or
                                        png_bytes_total + obj["size"] > 2 * MAX_BLOCK):
                        flush_png()
                    incoming = png_directory / "input"
                    incoming.mkdir(parents=True, exist_ok=True)
                    encoded = incoming / Path(png_name(index)).with_suffix(".ppg")
                    reader.write_object(obj, encoded)
                    validate_png_payload(encoded, png_name(index), obj["size"])
                    png_pending.append((index, obj, entries))
                    png_pixels_total += obj["png_pixels"]
                    png_bytes_total += obj["size"]
                else:
                    flush_png()
                    flush_jpeg()
                    reader.write_object(obj, first)
                    finish_object(staging, obj, entries)
            flush_png()
            flush_jpeg()
            destination.parent.mkdir(parents=True, exist_ok=True)
            publish_ready(staging, destination)
    finally:
        reader.block.cache_clear()
    return {"files": len(reader.manifest["files"]), "verified": True,
            "seconds": round(time.perf_counter() - started, 3)}


def main():
    parser = argparse.ArgumentParser(description="游戏冷藏包：独立打包、解冻和校验")
    parser.add_argument("command", choices=("pack", "unpack", "verify", "info"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--jxl-bin", type=Path)
    parser.add_argument("--png-tool", type=Path, help="PNG 编解码工具的路径")
    parser.add_argument("--no-png", action="store_true", help="打包时不使用 PNG 专用处理")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--image-threads", type=int, default=min(16, os.cpu_count() or 4))
    parser.add_argument("--effort", choices=("compact", "balanced", "fast"), default="balanced")
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--block-mib", type=int, default=64)
    parser.add_argument("--dictionary-mib", type=int, default=64)
    args = parser.parse_args()
    png = args.png_tool
    if png is None:
        bundled = ROOT / "codecs" / ("packPNG.exe" if os.name == "nt" else "packPNG")
        png = bundled if bundled.is_file() else None
    if args.command in ("pack", "unpack") and args.destination is None:
        parser.error("请指定输出路径")
    if args.command == "pack":
        result = pack(args.source, args.destination, jxl_bin=args.jxl_bin, png_tool=None if args.no_png else png,
                      threads=args.threads, image_threads=args.image_threads, effort=args.effort, temp_dir=args.temp_dir,
                      block_size=args.block_mib * 1024**2, dictionary=args.dictionary_mib * 1024**2,
                      progress=lambda stage: print(json.dumps({"stage": stage}, ensure_ascii=False), flush=True))
    elif args.command == "unpack":
        result = unpack(args.source, args.destination, jxl_bin=args.jxl_bin, png_tool=png,
                        image_threads=args.image_threads, temp_dir=args.temp_dir)
    elif args.command == "verify":
        with workspace(args.temp_dir) as work:
            result = unpack(args.source, work / "verified", jxl_bin=args.jxl_bin, png_tool=png,
                            image_threads=args.image_threads, temp_dir=args.temp_dir)
    else:
        data = Reader(args.source).manifest
        result = {"version": data["version"], "files": len(data["files"]), "blocks": len(data["blocks"]),
                  "png_files": sum(obj["transform"] == "png-pack" for obj in data["objects"]),
                  "source_bytes": sum(data["objects"][file["object"]]["size"] for file in data["files"]),
                  "archive_bytes": args.source.stat().st_size}
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, lzma.LZMAError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
