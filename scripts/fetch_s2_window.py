# -*- coding: utf-8 -*-
"""
从 Element84 Earth Search 的公开 Sentinel-2 L2A COG 按研究区窗口下载波段。

不下载整景（单景通常数百 MB 到数 GB）。只读取澳门近岸矩形外加 800 m 缓冲
的 B02/B03/B04/B08 与 SCL。反射率走 COG 的 2 倍概视图，落到 20 m 格网，
再把 DN 换成地表反射率。10 m 全分辨率瓦片在本机网络上会被截断。

两期是同一颗 Sentinel-2C、同一 MGRS 分幅 49QGE：
    冬季旱季  2026-01-08  S2C_T49QGE_20260108T030118_L2A
    夏季湿季  2026-08-06  S2C_T49QGE_20260806T030726_L2A

用法（在项目根目录）：
    python scripts/fetch_s2_window.py
    python scripts/fetch_s2_window.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

# 合并区间请求时，这个 S3 COG 会返回不完整的 206，10 m 瓦片经常被截断。
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
os.environ["GDAL_HTTP_MERGE_CONSECUTIVE_RANGES"] = "NO"
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "NO")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_INGESTED_BYTES_AT_OPEN", "32768")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("PROJ_NETWORK", "OFF")

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine, from_origin
from rasterio.warp import reproject
from rasterio.windows import Window, from_bounds

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
STAC_ITEM = (
    "https://earth-search.aws.element84.com/v1/collections/"
    "sentinel-2-c1-l2a/items/{item_id}"
)
STAC_SEARCH = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-c1-l2a"

# 云、云影、卷云、饱和、无数据。保留水体、植被、非植被、暗像元和未分类。
SCL_CLEAR = {2, 4, 5, 6, 7}
BANDS = (
    ("blue", "B02_blue"),
    ("green", "B03_green"),
    ("red", "B04_red"),
    ("nir", "B08_nir"),
)
SCENES = (
    ("winter", "S2C_T49QGE_20260108T030118_L2A", "冬季旱季"),
    ("summer", "S2C_T49QGE_20260806T030726_L2A", "夏季湿季"),
)
PROP_KEYS = (
    "datetime",
    "platform",
    "constellation",
    "eo:cloud_cover",
    "grid:code",
    "s2:processing_baseline",
    "s2:product_uri",
    "sat:orbit_state",
    "view:sun_elevation",
    "s2:vegetation_percentage",
    "s2:not_vegetated_percentage",
    "s2:water_percentage",
    "s2:cloud_shadow_percentage",
    "s2:nodata_pixel_percentage",
    "s2:thin_cirrus_percentage",
    "s2:medium_proba_clouds_percentage",
    "s2:high_proba_clouds_percentage",
)
NODATA = -9999.0
PAD_M = 800.0
# 原生 B04/B08 是 10 m。本机访问 S3 时，约 1.3 MB 的 10 m 压缩瓦片会被截断
# （TIFFFillTile: got N bytes, expected M）。COG 的 2 倍概视图约 20 m，可以完整读出。
RES_M = 20.0
OVERVIEW_FACTOR = 2


def retry(func, what: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return func()
        except Exception as exc:  # 网络读取 COG 偶发中断，重试即可
            last = exc
            print(f"  {what} 失败（{i + 1}/{tries}）：{exc}")
            time.sleep(2 + 2 * i)
    raise RuntimeError(f"{what} 在 {tries} 次尝试后仍失败") from last


def http_json(url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"

    def _get():
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)

    return retry(_get, url)


def load_aoi_bounds():
    gdf = gpd.read_file(RAW / "aoi.geojson")
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    utm = gdf.to_crs(32649)
    minx, miny, maxx, maxy = utm.total_bounds
    return (minx, miny, maxx, maxy), gdf


def target_grid(bounds):
    minx, miny, maxx, maxy = bounds
    minx = np.floor((minx - PAD_M) / RES_M) * RES_M
    miny = np.floor((miny - PAD_M) / RES_M) * RES_M
    maxx = np.ceil((maxx + PAD_M) / RES_M) * RES_M
    maxy = np.ceil((maxy + PAD_M) / RES_M) * RES_M
    width = int(round((maxx - minx) / RES_M))
    height = int(round((maxy - miny) / RES_M))
    transform = from_origin(minx, maxy, RES_M, RES_M)
    return transform, height, width


def request_window(src, bounds_xy):
    minx, miny, maxx, maxy = bounds_xy
    window = from_bounds(
        minx - PAD_M, miny - PAD_M, maxx + PAD_M, maxy + PAD_M, src.transform
    )
    window = window.round_offsets().round_lengths()
    full = Window(0, 0, src.width, src.height)
    return window.intersection(full)


def read_window(src, bounds_xy, overview_factor: int = 1):
    """读取研究区窗口。overview_factor>1 时走 COG 概视图，避开读不完整的 10 m 瓦片。"""
    window = request_window(src, bounds_xy)
    if overview_factor <= 1:
        arr = src.read(1, window=window)
        return arr, src.window_transform(window)
    out_h = max(1, int(window.height) // overview_factor)
    out_w = max(1, int(window.width) // overview_factor)
    arr = src.read(
        1,
        window=window,
        out_shape=(out_h, out_w),
        resampling=Resampling.nearest,
    )
    native = src.window_transform(window)
    scaled = native * Affine.scale(window.width / out_w, window.height / out_h)
    return arr, scaled


def warp_to_grid(arr, src_transform, src_crs, dst_transform, dst_shape, resampling, src_nodata, dst_nodata, dtype):
    dest = np.full(dst_shape, dst_nodata, dtype=dtype)
    reproject(
        source=arr,
        destination=dest,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=src_nodata,
        dst_transform=dst_transform,
        dst_crs="EPSG:32649",
        dst_nodata=dst_nodata,
        resampling=resampling,
    )
    return dest


def dn_to_reflectance(dn: np.ndarray, scale: float, offset: float):
    """L2A COG 的 scale/offset 已含 Collection-1 的 BOA 偏移：DN*0.0001-0.1。"""
    refl = dn.astype(np.float32) * np.float32(scale) + np.float32(offset)
    invalid = dn == 0
    valid = ~invalid
    median = float(np.median(refl[valid])) if valid.any() else 999.0
    method = "geotiff_scale_offset"
    if median > 3:
        # 文件没写 scale 时，按官方量化值与 BOA 偏移回算
        refl = (dn.astype(np.float32) - 1000.0) / 10000.0
        method = "fallback_boa_offset_-1000_div_10000"
        median = float(np.median(refl[valid])) if valid.any() else 999.0
    refl[invalid] = np.float32(NODATA)
    extreme = valid & ((refl < -0.1) | (refl > 1.2))
    refl[extreme] = np.float32(NODATA)
    return refl, method, median


def write_multiband(path: Path, bands: np.ndarray, transform, descriptions, tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": bands.shape[1],
        "width": bands.shape[2],
        "count": bands.shape[0],
        "dtype": "float32",
        "crs": "EPSG:32649",
        "transform": transform,
        "nodata": NODATA,
        "compress": "lzw",
        "predictor": 3,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(bands)
        for i, name in enumerate(descriptions, start=1):
            dst.set_band_description(i, name)
        dst.update_tags(**tags)


def write_scl(path: Path, scl: np.ndarray, transform, tags):
    profile = {
        "driver": "GTiff",
        "height": scl.shape[0],
        "width": scl.shape[1],
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:32649",
        "transform": transform,
        "nodata": 0,
        "compress": "lzw",
        "predictor": 2,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(scl, 1)
        dst.set_band_description(1, "SCL")
        dst.update_tags(**tags)


def aoi_clear_percent(scl: np.ndarray, transform, aoi_gdf) -> float:
    geoms = [g.__geo_interface__ for g in aoi_gdf.to_crs(32649).geometry]
    inside = ~geometry_mask(geoms, out_shape=scl.shape, transform=transform, invert=False)
    if not inside.any():
        return 0.0
    clear = inside & np.isin(scl, list(SCL_CLEAR))
    return float(clear.sum() / inside.sum() * 100.0)


def fetch_scene(item_id: str, bounds, transform, shape, aoi_gdf):
    item = http_json(STAC_ITEM.format(item_id=item_id))
    assets = item["assets"]
    props = item.get("properties", {})
    height, width = shape

    def grab(asset_key, resampling, dtype, src_nodata, dst_nodata, overview_factor=1):
        href = assets[asset_key]["href"]
        print(f"  读取 {asset_key}（概视图倍数 {overview_factor}）", flush=True)

        def _read():
            with rasterio.open(href) as src:
                arr, win_transform = read_window(src, bounds, overview_factor)
                scale = float(src.scales[0]) if src.scales else 1.0
                offset = float(src.offsets[0]) if src.offsets else 0.0
                warped = warp_to_grid(
                    arr, win_transform, src.crs, transform, (height, width),
                    resampling, src_nodata, dst_nodata, dtype,
                )
            return warped, scale, offset

        return retry(_read, asset_key)

    scl, _, _ = grab("scl", Resampling.nearest, "uint8", 0, 0, 1)
    reflectance = []
    descriptions = []
    scale_method = None
    scale_used = None
    offset_used = None
    red_median = None
    for key, desc in BANDS:
        dn, scale, offset = grab(key, Resampling.bilinear, "uint16", 0, 0, OVERVIEW_FACTOR)
        refl, method, median = dn_to_reflectance(dn, scale, offset)
        bad = ~np.isin(scl, list(SCL_CLEAR))
        refl[bad] = np.float32(NODATA)
        reflectance.append(refl)
        descriptions.append(desc)
        scale_method = method
        scale_used = scale
        offset_used = offset
        if key == "red":
            red_median = median
            print(f"  红波段反射率中位数 {median:.4f}（换算方式 {method}）")

    stack = np.stack(reflectance, axis=0)
    clear_pct = aoi_clear_percent(scl, transform, aoi_gdf)
    print(f"  研究区 SCL 晴空像元 {clear_pct:.2f}%")
    kept = {k: props.get(k) for k in PROP_KEYS}
    return {
        "stack": stack,
        "scl": scl.astype(np.uint8),
        "descriptions": descriptions,
        "meta": {
            "item_id": item_id,
            "datetime": props.get("datetime"),
            "date": (props.get("datetime") or "")[:10],
            "platform": props.get("platform"),
            "mgrs": props.get("grid:code"),
            "eo_cloud_cover": props.get("eo:cloud_cover"),
            "processing_baseline": props.get("s2:processing_baseline"),
            "stac_item": STAC_ITEM.format(item_id=item_id),
            "reflectance_scale": scale_used,
            "reflectance_offset": offset_used,
            "reflectance_method": scale_method,
            "red_reflectance_median_before_scl_mask": red_median,
            "aoi_scl_clear_percent": round(clear_pct, 2),
            "properties": kept,
        },
    }


def screen_summer_cloud():
    """记录 2026 年 6–8 月整景云量最低的几景，说明为何没按整景 <10% 筛选。"""
    body = {
        "collections": [COLLECTION],
        "bbox": [113.528, 22.106, 113.598, 22.217],
        "datetime": "2026-06-01T00:00:00Z/2026-08-31T23:59:59Z",
        "limit": 15,
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    try:
        data = http_json(STAC_SEARCH, body)
    except Exception as exc:
        print(f"整景云量检索失败，目录里不写筛选表：{exc}")
        return []
    rows = []
    for feat in data.get("features", [])[:8]:
        props = feat.get("properties", {})
        rows.append({
            "item_id": feat.get("id"),
            "date": (props.get("datetime") or "")[:10],
            "eo_cloud_cover": props.get("eo:cloud_cover"),
            "mgrs": props.get("grid:code"),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description="下载两期 Sentinel-2 L2A 研究区窗口")
    parser.add_argument("--force", action="store_true", help="已有 GeoTIFF 时也重新下载")
    args = parser.parse_args()

    needed = [RAW / f"s2_{name}_boa.tif" for name, _, _ in SCENES]
    needed += [RAW / f"s2_{name}_scl.tif" for name, _, _ in SCENES]
    if all(p.exists() for p in needed) and not args.force and (RAW / "scene_catalog.json").exists():
        print("本地窗口已存在。若要重下，加 --force。")
        return

    bounds, aoi_gdf = load_aoi_bounds()
    transform, height, width = target_grid(bounds)
    print(f"目标格网 {width} × {height}，{RES_M:.0f} m，EPSG:32649，缓冲 {PAD_M:.0f} m")

    screening = screen_summer_cloud()
    catalog_scenes = {}
    for season, item_id, role in SCENES:
        print(f"\n==== {role} {item_id} ====")
        got = fetch_scene(item_id, bounds, transform, (height, width), aoi_gdf)
        tags = {
            "SCENE_ID": item_id,
            "ACQUIRED": got["meta"]["datetime"] or "",
            "PRODUCT": "Sentinel-2 L2A Collection-1 surface reflectance",
            "SCALE_OFFSET_APPLIED": "yes",
            "SCL_CLEAR_CLASSES": "2,4,5,6,7",
        }
        boa_path = RAW / f"s2_{season}_boa.tif"
        scl_path = RAW / f"s2_{season}_scl.tif"
        write_multiband(boa_path, got["stack"], transform, got["descriptions"], tags)
        write_scl(scl_path, got["scl"], transform, tags)
        meta = got["meta"]
        meta["role"] = role
        meta["boa_file"] = str(boa_path.relative_to(ROOT)).replace("\\", "/")
        meta["scl_file"] = str(scl_path.relative_to(ROOT)).replace("\\", "/")
        catalog_scenes[season] = meta
        print(f"  已写 {boa_path.name}（{boa_path.stat().st_size / 1e6:.1f} MB）")

    catalog = {
        "source": "Element84 Earth Search 公开 COG，集合 sentinel-2-c1-l2a（Copernicus Sentinel-2 L2A Collection-1）",
        "credit": "Contains modified Copernicus Sentinel data 2026",
        "stac_endpoint": "https://earth-search.aws.element84.com/v1",
        "why_these_dates": (
            "2026-01-08 与 2026-08-06 同为 Sentinel-2C、分幅 49QGE。"
            "一月代表冬季旱季，八月代表夏季湿季。"
            "珠江口夏季整景云量很少低于 10%，因此先保留研究区内 SCL 晴空比例高的八月景，"
            "而不是只看 110 km 分幅的整景云量。"
        ),
        "resolution_note": (
            f"输出格网 {RES_M:.0f} m。B04/B08 的原生分辨率是 10 m；"
            f"读取时使用 COG 的 {OVERVIEW_FACTOR} 倍概视图，因为本机对约 1.3 MB 的 10 m 压缩瓦片会收到不完整响应。"
            "SCL 本身是 20 m，与这个格网一致。NDVI 用的仍是 B04 与 B08 的地表反射率。"
        ),
        "aoi_file": "data/raw/aoi.geojson",
        "target_grid": {
            "crs": "EPSG:32649",
            "resolution_m": RES_M,
            "width": width,
            "height": height,
            "pad_m": PAD_M,
        },
        "reflectance_note": "地表反射率 = DN * scale + offset。本批 COG 的 scale=0.0001、offset=-0.1，等价于 (DN-1000)/10000。NDVI 对共同乘数不敏感，但对这个加性偏移敏感，所以必须换算后再做比值。",
        "scl_clear_classes": sorted(SCL_CLEAR),
        "scl_class_meaning": {
            "0": "无数据",
            "1": "饱和或缺陷",
            "2": "暗像元（保留）",
            "3": "云影",
            "4": "植被",
            "5": "非植被",
            "6": "水体",
            "7": "未分类（保留）",
            "8": "中概率云",
            "9": "高概率云",
            "10": "薄卷云",
            "11": "雪或冰",
        },
        "summer_2026_lowest_tile_cloud": screening,
        "scenes": catalog_scenes,
    }
    out = RAW / "scene_catalog.json"
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写 {out}")


if __name__ == "__main__":
    main()
