# -*- coding: utf-8 -*-
"""按小块 HTTP Range 读取 Sentinel-2 COG 的原生分辨率窗口。

本机代理对一次约 1.5 MB 的 TIFF 瓦片会提前断开（GDAL 报 TIFFFillTile:
got N bytes, expected M）。64 KB 到 1 MB 的 Range 可以完整返回。这里自己
拼瓦片，再用 zlib 解开 Deflate，并还原 TIFF 水平差分。
"""

from __future__ import annotations

import struct
import threading
import time
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np

PROXY = "http://127.0.0.1:7897"
CHUNK = 1024 * 1024
_LOCAL = threading.local()


def _opener():
    opener = getattr(_LOCAL, "opener", None)
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
        )
        _LOCAL.opener = opener
    return opener


def read_range(url: str, start: int, length: int, tries: int = 4) -> bytes:
    """读取 [start, start+length)。失败时把这一段拆成更小的 Range 再试。"""
    if length <= 0:
        return b""
    buf = bytearray()
    pos = start
    end = start + length
    piece = min(CHUNK, length)
    while pos < end:
        ask = min(piece, end - pos)
        last = None
        for attempt in range(tries):
            try:
                req = urllib.request.Request(
                    url, headers={"Range": f"bytes={pos}-{pos + ask - 1}"}
                )
                with _opener().open(req, timeout=60) as resp:
                    part = resp.read()
                if len(part) != ask:
                    raise OSError(f"Range 不完整：要 {ask} 字节，得到 {len(part)}")
                buf.extend(part)
                pos += ask
                last = None
                break
            except Exception as exc:
                last = exc
                time.sleep(0.6 + 0.8 * attempt)
        if last is not None:
            if piece > 65536:
                piece = 65536
                continue
            raise RuntimeError(f"读取 {url} [{pos}, {pos + ask}) 失败") from last
    return bytes(buf)


def _parse_ifd(blob: bytes):
    endian = "<" if blob[:2] == b"II" else ">"
    magic = struct.unpack_from(endian + "H", blob, 2)[0]
    if magic == 42:
        ifd_off = struct.unpack_from(endian + "I", blob, 4)[0]
        count = struct.unpack_from(endian + "H", blob, ifd_off)[0]
        entry_size, count_size, val_size = 12, 2, 4
        pack = "HHII"
    elif magic == 43:
        ifd_off = struct.unpack_from(endian + "Q", blob, 8)[0]
        count = struct.unpack_from(endian + "Q", blob, ifd_off)[0]
        entry_size, count_size, val_size = 20, 8, 8
        pack = "HHQQ"
    else:
        raise RuntimeError(f"不是可识别的 TIFF，magic={magic}")

    tags = {}
    pos = ifd_off + count_size
    for _ in range(count):
        tag, typ, n, packed = struct.unpack_from(endian + pack, blob, pos)
        tags[tag] = (typ, n, packed, val_size)
        pos += entry_size
    return endian, tags


def _tag_array(url: str, blob: bytes, endian: str, tags: dict, tag: int, dtype: str) -> np.ndarray:
    typ, n, packed, val_size = tags[tag]
    item = np.dtype(dtype).itemsize
    if n * item <= val_size:
        raw = struct.pack(endian + ("I" if val_size == 4 else "Q"), packed)[: n * item]
    else:
        raw = read_range(url, int(packed), int(n * item))
    return np.frombuffer(raw, dtype=np.dtype(dtype).newbyteorder("<" if endian == "<" else ">")).copy()


def open_cog(url: str) -> dict:
    blob = read_range(url, 0, 65536)
    endian, tags = _parse_ifd(blob)

    def scalar(tag):
        typ, n, packed, _ = tags[tag]
        if n != 1:
            raise RuntimeError(f"标签 {tag} 不是标量")
        if typ == 3:
            return int(packed & 0xFFFF)
        if typ == 4:
            return int(packed & 0xFFFFFFFF)
        raise RuntimeError(f"标签 {tag} 类型 {typ} 未处理")

    bits = scalar(258) if tags[258][1] == 1 else int(_tag_array(url, blob, endian, tags, 258, "<u2")[0])
    compression = scalar(259)
    predictor = scalar(317) if 317 in tags else 1
    tile_w = scalar(322)
    tile_h = scalar(323)
    if compression != 8:
        raise RuntimeError(f"只处理 Deflate COG，当前压缩码 {compression}")
    if predictor not in (1, 2):
        raise RuntimeError(f"未处理的 TIFF predictor {predictor}")
    if bits == 16:
        dtype = np.dtype("<u2")
    elif bits == 8:
        dtype = np.dtype("u1")
    else:
        raise RuntimeError(f"未处理的位深 {bits}")
    count_dtype = "<u4" if tags[324][0] == 4 else "<u8"
    offsets = _tag_array(url, blob, endian, tags, 324, count_dtype).astype(np.int64)
    counts = _tag_array(url, blob, endian, tags, 325, count_dtype).astype(np.int64)
    return {
        "url": url,
        "width": scalar(256),
        "height": scalar(257),
        "tile_w": tile_w,
        "tile_h": tile_h,
        "predictor": predictor,
        "dtype": dtype,
        "offsets": offsets,
        "counts": counts,
    }


def _decode_tile(meta: dict, row: int, col: int) -> np.ndarray:
    across = (meta["width"] + meta["tile_w"] - 1) // meta["tile_w"]
    index = row * across + col
    raw = read_range(meta["url"], int(meta["offsets"][index]), int(meta["counts"][index]))
    decoded = zlib.decompress(raw)
    item = meta["dtype"].itemsize
    valid_w = min(meta["tile_w"], meta["width"] - col * meta["tile_w"])
    valid_h = min(meta["tile_h"], meta["height"] - row * meta["tile_h"])
    n = len(decoded) // item
    arr = np.frombuffer(decoded, dtype=meta["dtype"]).copy()
    if n == meta["tile_w"] * meta["tile_h"]:
        img = arr.reshape(meta["tile_h"], meta["tile_w"])
    elif n == valid_w * valid_h:
        img = arr.reshape(valid_h, valid_w)
    else:
        raise RuntimeError(f"瓦片 {row},{col} 解开后有 {n} 个像元，对不上 {valid_h}×{valid_w}")
    if meta["predictor"] == 2:
        acc = np.cumsum(img.astype(np.uint32), axis=1)
        mask = np.iinfo(meta["dtype"]).max
        img = (acc & mask).astype(meta["dtype"])
    return img[:valid_h, :valid_w]


def read_window(url: str, col_off: int, row_off: int, width: int, height: int, workers: int = 4) -> np.ndarray:
    meta = open_cog(url)
    col_off = max(0, col_off)
    row_off = max(0, row_off)
    width = min(width, meta["width"] - col_off)
    height = min(height, meta["height"] - row_off)
    out = np.zeros((height, width), dtype=meta["dtype"])
    tw, th = meta["tile_w"], meta["tile_h"]
    c0, c1 = col_off // tw, (col_off + width - 1) // tw
    r0, r1 = row_off // th, (row_off + height - 1) // th
    jobs = [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

    def fetch(job):
        r, c = job
        return job, _decode_tile(meta, r, c)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (r, c), tile in pool.map(fetch, jobs):
            src_r = r * th
            src_c = c * tw
            r_start = max(src_r, row_off)
            c_start = max(src_c, col_off)
            r_stop = min(src_r + tile.shape[0], row_off + height)
            c_stop = min(src_c + tile.shape[1], col_off + width)
            out[r_start - row_off:r_stop - row_off, c_start - col_off:c_stop - col_off] = tile[
                r_start - src_r:r_stop - src_r,
                c_start - src_c:c_stop - src_c,
            ]
    return out
