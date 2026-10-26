# -*- coding: utf-8 -*-
"""
计算两期 NDVI、季节差值，并写出统计与快速图。

NDVI = (B08 - B04) / (B08 + B04)
输入应已是地表反射率。云和云影在下载阶段用 SCL 去掉。

用法（项目根目录）：
    python compute_ndvi.py
    python compute_ndvi.py --demo     # 只用合成栅格自测公式，结果进 outputs/demo
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap
from rasterio.features import geometry_mask, rasterize
from rasterio.mask import mask as rio_mask

ROOT = Path(__file__).resolve().parent
NODATA = -9999.0
CHANGE_THRESHOLD = 0.10
NDVI_STOPS = [
    (-0.2, "#2c7bb6"),
    (0.0, "#ffffbf"),
    (0.25, "#a6d96a"),
    (0.5, "#1a9641"),
    (0.8, "#004529"),
]
DIFF_STOPS = [(-0.3, "#8c510a"), (0.0, "#f7f7f7"), (0.3, "#01665e")]
CLASS_BINS = (
    ("水体或强吸收（<0）", -1.05, 0.0),
    ("建成区/裸地/稀疏（0–0.2）", 0.0, 0.2),
    ("中等植被（0.2–0.5）", 0.2, 0.5),
    ("较密植被（≥0.5）", 0.5, 1.05),
)


def setup_font():
    for name in ("Microsoft YaHei", "SimHei", "Source Han Sans SC"):
        if any(name == font.name for font in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


def cmap_from_stops(stops, name):
    vmin, vmax = stops[0][0], stops[-1][0]
    anchors = [(v - vmin) / (vmax - vmin) for v, _ in stops]
    cmap = LinearSegmentedColormap.from_list(name, list(zip(anchors, [c for _, c in stops])))
    cmap.set_bad("#d0d0d0")
    return cmap


def load_catalog():
    path = ROOT / "data" / "raw" / "scene_catalog.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_aoi(path: Path, crs):
    import geopandas as gpd

    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf.to_crs(crs)


def clip_raster(path: Path, aoi_path: Path, indexes):
    """按研究区裁剪。等价于 QGIS『按掩膜图层裁剪栅格』。"""
    with rasterio.open(path) as src:
        gdf = read_aoi(aoi_path, src.crs)
        geoms = [geom.__geo_interface__ for geom in gdf.geometry]
        nodata = src.nodata if src.nodata is not None else 0
        data, transform = rio_mask(
            src, geoms, crop=True, nodata=nodata, indexes=list(indexes)
        )
        crs = src.crs
        descriptions = [src.descriptions[i - 1] if src.descriptions else "" for i in indexes]
    data = data.astype("float32")
    if np.issubdtype(np.asarray(nodata).dtype, np.floating):
        data[np.isclose(data, float(nodata))] = np.nan
    else:
        data[data == nodata] = np.nan
    inside = ~geometry_mask(geoms, out_shape=data.shape[-2:], transform=transform, invert=False)
    profile = {
        "crs": crs,
        "transform": transform,
        "height": data.shape[-2],
        "width": data.shape[-1],
    }
    return data, profile, inside, descriptions


def ndvi(red, nir):
    """比值形式消去两波段共同的亮度倍数；分母过小或无效像元记为 nan。"""
    denom = nir + red
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (nir - red) / denom
    # 偏移改正后略小于 0 的反射率是暗像元噪声，做比值会冲出 [-1, 1]
    valid = np.isfinite(red) & np.isfinite(nir) & (np.abs(denom) > 1e-6)
    valid &= (red >= 0) & (nir >= 0) & (red <= 1.0) & (nir <= 1.0)
    out = np.where(valid, out, np.nan).astype("float32")
    if np.isfinite(out).any():
        outside = np.isfinite(out) & ((out < -1.05) | (out > 1.05))
        if outside.any():
            raise RuntimeError(f"NDVI 有 {int(outside.sum())} 个像元超出 [-1.05, 1.05]，请检查反射率换算")
    return out


def summarize(arr: np.ndarray, pixel_m: float = 10.0) -> dict:
    values = arr[np.isfinite(arr)]
    if values.size == 0:
        return {"n": 0}
    area_km2 = float(values.size) * (pixel_m ** 2) / 1e6
    return {
        "n": int(values.size),
        "area_km2": round(area_km2, 4),
        "min": round(float(values.min()), 4),
        "max": round(float(values.max()), 4),
        "mean": round(float(values.mean()), 4),
        "std": round(float(values.std()), 4),
        "p05": round(float(np.percentile(values, 5)), 4),
        "p50": round(float(np.percentile(values, 50)), 4),
        "p95": round(float(np.percentile(values, 95)), 4),
    }


def class_fractions(arr: np.ndarray) -> dict:
    values = arr[np.isfinite(arr)]
    total = int(values.size)
    out = {}
    if total == 0:
        return out
    for label, lo, hi in CLASS_BINS:
        if hi >= 1.0:
            count = int(((values >= lo) & (values <= hi)).sum())
        else:
            count = int(((values >= lo) & (values < hi)).sum())
        out[label] = {"n": count, "percent": round(100.0 * count / total, 2)}
    return out


def save_raster(path: Path, arr: np.ndarray, profile: dict, description: str, dtype="float32"):
    path.parent.mkdir(parents=True, exist_ok=True)
    out_profile = {
        "driver": "GTiff",
        "height": arr.shape[0],
        "width": arr.shape[1],
        "count": 1,
        "dtype": dtype,
        "crs": profile["crs"],
        "transform": profile["transform"],
        "nodata": NODATA,
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    if dtype == "float32":
        out_profile["predictor"] = 3
        payload = np.where(np.isfinite(arr), arr, NODATA).astype("float32")
    else:
        out_profile["predictor"] = 2
        payload = arr.astype(dtype)
    with rasterio.open(path, "w", **out_profile) as dst:
        dst.write(payload, 1)
        dst.set_band_description(1, description)
        dst.update_tags(FORMULA="(B08-B04)/(B08+B04)")
    print(f"已保存 {path}")


def coverage(arr, inside) -> dict:
    inside_n = int(inside.sum())
    valid_n = int((inside & np.isfinite(arr)).sum())
    return {
        "inside_pixels": inside_n,
        "valid_pixels": valid_n,
        "valid_percent_of_aoi": round(100.0 * valid_n / inside_n, 2) if inside_n else 0.0,
    }


def pixel_size_m(profile: dict) -> float:
    transform = profile["transform"]
    return float(abs(transform.a))


def zonal_table(summer, winter, diff, profile, zones_path: Path):
    if not zones_path.exists():
        return []
    gdf = read_aoi(zones_path, profile["crs"])
    if "zone_id" not in gdf.columns:
        gdf["zone_id"] = np.arange(1, len(gdf) + 1)
    if "name" not in gdf.columns:
        gdf["name"] = gdf["zone_id"].astype(str)
    shapes = [
        (geom, int(zone_id))
        for geom, zone_id in zip(gdf.geometry, gdf["zone_id"])
        if geom is not None and not geom.is_empty
    ]
    zones = rasterize(
        shapes,
        out_shape=summer.shape,
        transform=profile["transform"],
        fill=0,
        dtype="int16",
    )
    rows = []
    for _, row in gdf.sort_values("zone_id").iterrows():
        zone_id = int(row["zone_id"])
        mask = zones == zone_id
        rec = {"zone_id": zone_id, "name": str(row["name"]), "pixels_in_zone_and_aoi": int(mask.sum())}
        pixel_m = pixel_size_m(profile)
        for key, arr in (("summer", summer), ("winter", winter), ("diff", diff)):
            stats = summarize(np.where(mask, arr, np.nan), pixel_m)
            rec[f"{key}_n"] = stats.get("n", 0)
            rec[f"{key}_mean"] = stats.get("mean")
            rec[f"{key}_p50"] = stats.get("p50")
        rows.append(rec)
    return rows


def scl_means(ndvi_arr, scl_path, aoi_path, profile, pixel_m):
    if not scl_path or not Path(scl_path).exists():
        return {}
    scl, scl_profile, _, _ = clip_raster(Path(scl_path), aoi_path, [1])
    scl = scl[0]
    if scl.shape != ndvi_arr.shape:
        return {"warning": f"SCL 形状 {scl.shape} 与 NDVI {ndvi_arr.shape} 不一致，跳过交叉检查"}
    out = {}
    for code, label in ((6, "water_scl6"), (4, "vegetation_scl4"), (5, "not_vegetated_scl5")):
        stats = summarize(np.where(np.isclose(scl, code), ndvi_arr, np.nan), pixel_m)
        out[label] = stats
    return out


def draw_maps(summer, winter, diff, titles, out_png: Path, pixel_m: float):
    setup_font()
    ndvi_cmap = cmap_from_stops(NDVI_STOPS, "ndvi_fixed")
    diff_cmap = cmap_from_stops(DIFF_STOPS, "ndvi_diff_fixed")
    fig, axes = plt.subplots(1, 3, figsize=(14.6, 5.8), dpi=160, facecolor="white")
    panels = (
        (axes[0], summer, ndvi_cmap, -0.2, 0.8, titles[0], "NDVI"),
        (axes[1], winter, ndvi_cmap, -0.2, 0.8, titles[1], "NDVI"),
        (axes[2], diff, diff_cmap, -0.3, 0.3, titles[2], "夏季 − 冬季"),
    )
    height, width = summer.shape
    km = pixel_m / 1000.0
    extent = (0, width * km, 0, height * km)
    for ax, arr, cmap, vmin, vmax, title, cbar_label in panels:
        show = np.ma.masked_invalid(arr)
        im = ax.imshow(show, cmap=cmap, vmin=vmin, vmax=vmax, extent=extent, origin="upper", interpolation="nearest")
        mean = float(np.nanmean(arr)) if np.isfinite(arr).any() else float("nan")
        ax.set_title(f"{title}\n均值 {mean:.3f}", fontsize=12, pad=8)
        ax.set_xlabel("向东 km")
        ax.set_ylabel("向北 km")
        ax.plot([0.4, 1.4], [0.35, 0.35], color="black", lw=2.2, solid_capstyle="butt")
        ax.text(0.9, 0.52, "1 km", ha="center", va="bottom", fontsize=8)
        ax.annotate(
            "N",
            xy=(width * km - 0.45, height * km - 0.35),
            xytext=(width * km - 0.45, height * km - 1.15),
            ha="center",
            fontsize=9,
            arrowprops={"arrowstyle": "-|>", "lw": 1.1, "color": "black"},
        )
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label=cbar_label)
    fig.suptitle("Sentinel-2 L2A 地表反射率 NDVI · 澳门及近岸", fontsize=15, y=1.02)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"已保存 {out_png}")


def draw_hist(summer, winter, diff, out_png: Path):
    setup_font()
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.4), dpi=150, facecolor="white")
    bins = np.linspace(-0.6, 1.0, 65)
    axes[0].hist(summer[np.isfinite(summer)], bins=bins, histtype="step", density=True, lw=1.6, label="夏季", color="#1a9641")
    axes[0].hist(winter[np.isfinite(winter)], bins=bins, histtype="step", density=True, lw=1.6, label="冬季", color="#2c7bb6")
    axes[0].axvline(0.0, color="#888888", lw=0.8)
    axes[0].set_title("NDVI 分布")
    axes[0].set_xlabel("NDVI")
    axes[0].set_ylabel("密度")
    axes[0].legend(frameon=False)

    valid = diff[np.isfinite(diff)]
    gain = int((valid > CHANGE_THRESHOLD).sum())
    loss = int((valid < -CHANGE_THRESHOLD).sum())
    stable = int(valid.size - gain - loss)
    axes[1].bar(
        ["明显降低\n(< −0.10)", "基本稳定\n(|差值| ≤ 0.10)", "明显升高\n(> 0.10)"],
        [loss, stable, gain],
        color=["#8c510a", "#bdbdbd", "#01665e"],
    )
    axes[1].set_title("季节差值像元数（夏 − 冬）")
    axes[1].set_ylabel("像元")
    fig.tight_layout()
    fig.savefig(out_png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"已保存 {out_png}")


def write_csv(path: Path, rows: list):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def season_title(catalog, key, fallback):
    scene = (catalog.get("scenes") or {}).get(key) or {}
    date = scene.get("date")
    role = scene.get("role")
    if date and role:
        return f"{role} {date}"
    return fallback


def build_parser():
    parser = argparse.ArgumentParser(description="两期 NDVI 与季节差值")
    parser.add_argument("--demo", action="store_true", help="使用合成栅格，不代表真实地点")
    parser.add_argument("--summer", default="data/raw/s2_summer_boa.tif")
    parser.add_argument("--winter", default="data/raw/s2_winter_boa.tif")
    parser.add_argument("--summer-scl", default="data/raw/s2_summer_scl.tif")
    parser.add_argument("--winter-scl", default="data/raw/s2_winter_scl.tif")
    parser.add_argument("--aoi", default="data/raw/aoi.geojson")
    parser.add_argument("--zones", default="data/raw/zones.geojson")
    parser.add_argument("--red-band", type=int, default=3)
    parser.add_argument("--nir-band", type=int, default=4)
    parser.add_argument("--out", default="outputs")
    parser.add_argument("--no-clip", action="store_true")
    return parser


def apply_demo(args):
    args.summer = "data/raw/sim_summer.tif"
    args.winter = "data/raw/sim_winter.tif"
    args.red_band = 1
    args.nir_band = 2
    args.summer_scl = ""
    args.winter_scl = ""
    args.out = "outputs/demo"
    args.no_clip = True
    args.zones = ""


def main():
    args = build_parser().parse_args()
    if args.demo:
        apply_demo(args)
    out_dir = ROOT / args.out
    summer_path = ROOT / args.summer
    winter_path = ROOT / args.winter
    for path in (summer_path, winter_path):
        if not path.exists():
            raise SystemExit(f"找不到 {path}。真实数据请先运行 python scripts/fetch_s2_window.py")

    catalog = {} if args.demo else load_catalog()
    aoi_path = ROOT / args.aoi
    if args.no_clip:
        with rasterio.open(summer_path) as src:
            summer_stack = src.read([args.red_band, args.nir_band]).astype("float32")
            profile = {"crs": src.crs, "transform": src.transform, "height": src.height, "width": src.width}
            nodata = src.nodata
        with rasterio.open(winter_path) as src:
            winter_stack = src.read([args.red_band, args.nir_band]).astype("float32")
        for stack in (summer_stack, winter_stack):
            if nodata is not None:
                stack[np.isclose(stack, float(nodata))] = np.nan
        inside = np.ones(summer_stack.shape[1:], dtype=bool)
    else:
        summer_stack, profile, inside, _ = clip_raster(summer_path, aoi_path, [args.red_band, args.nir_band])
        winter_stack, winter_profile, _, _ = clip_raster(winter_path, aoi_path, [args.red_band, args.nir_band])
        same_grid = (
            summer_stack.shape == winter_stack.shape
            and profile["transform"] == winter_profile["transform"]
        )
        if not same_grid:
            raise SystemExit(
                "两期裁剪格网不一致："
                f"{summer_stack.shape} / {winter_stack.shape}。"
                "请用 fetch_s2_window.py 生成的同源窗口。"
            )

    summer = ndvi(summer_stack[0], summer_stack[1])
    winter = ndvi(winter_stack[0], winter_stack[1])
    # 裁剪框四角在多边形外，inside 为 False；这些位置即使有值也不进入统计
    summer = np.where(inside, summer, np.nan)
    winter = np.where(inside, winter, np.nan)
    diff = np.where(np.isfinite(summer) & np.isfinite(winter), summer - winter, np.nan).astype("float32")

    change = np.full(diff.shape, NODATA, dtype="float32")
    valid_diff = np.isfinite(diff)
    change[valid_diff & (diff > CHANGE_THRESHOLD)] = 1
    change[valid_diff & (diff < -CHANGE_THRESHOLD)] = -1
    change[valid_diff & (np.abs(diff) <= CHANGE_THRESHOLD)] = 0

    save_raster(out_dir / "ndvi_summer.tif", summer, profile, "NDVI_summer")
    save_raster(out_dir / "ndvi_winter.tif", winter, profile, "NDVI_winter")
    save_raster(out_dir / "ndvi_diff.tif", diff, profile, "NDVI_summer_minus_winter")
    save_raster(out_dir / "ndvi_change_class.tif", change, profile, "change_class_-1_0_1", dtype="float32")

    zones = zonal_table(summer, winter, diff, profile, ROOT / args.zones) if args.zones else []
    write_csv(out_dir / "zonal_stats.csv", zones)

    both = np.isfinite(summer) & np.isfinite(winter)
    pixel_m = pixel_size_m(profile)
    gain_n = int((valid_diff & (diff > CHANGE_THRESHOLD)).sum())
    loss_n = int((valid_diff & (diff < -CHANGE_THRESHOLD)).sum())
    stable_n = int((valid_diff & (np.abs(diff) <= CHANGE_THRESHOLD)).sum())
    pixel_km2 = (pixel_m ** 2) / 1e6
    stats = {
        "demo": bool(args.demo),
        "formula": "(B08 - B04) / (B08 + B04)",
        "difference": "summer - winter",
        "change_threshold": CHANGE_THRESHOLD,
        "change_threshold_note": "±0.10 是本练习用来划分明显升降的显示阈值，不是土地覆盖变化的法定标准。",
        "class_bins_note": "NDVI 分段是常用的目视解译区间，没有用地面样本做精度评价。",
        "display_stretch": {"ndvi": [-0.2, 0.8], "diff": [-0.3, 0.3]},
        "pixel_size_m": pixel_m,
        "summer": {
            **summarize(summer, pixel_m),
            "coverage": coverage(summer, inside),
            "classes": class_fractions(summer),
            "scl": scl_means(summer, args.summer_scl, aoi_path, profile, pixel_m) if args.summer_scl else {},
        },
        "winter": {
            **summarize(winter, pixel_m),
            "coverage": coverage(winter, inside),
            "classes": class_fractions(winter),
            "scl": scl_means(winter, args.winter_scl, aoi_path, profile, pixel_m) if args.winter_scl else {},
        },
        "diff": {
            **summarize(diff, pixel_m),
            "overlap_pixels": int(both.sum()),
            "gain_gt_threshold": gain_n,
            "gain_area_km2": round(gain_n * pixel_km2, 4),
            "loss_lt_threshold": loss_n,
            "loss_area_km2": round(loss_n * pixel_km2, 4),
            "stable": stable_n,
            "stable_area_km2": round(stable_n * pixel_km2, 4),
        },
        "zones": zones,
        "scenes": catalog.get("scenes", {}),
        "credit": catalog.get("credit"),
    }
    # 只在两期都有效、且夏季 SCL 判为植被的像元上看季节差，避免被大面积水体拉平
    if args.summer_scl and Path(args.summer_scl).exists():
        scl_s, _, _, _ = clip_raster(Path(ROOT / args.summer_scl), aoi_path, [1])
        if scl_s.shape[1:] == summer.shape:
            veg = np.isclose(scl_s[0], 4) & both
            stats["diff_on_summer_scl_vegetation"] = summarize(np.where(veg, diff, np.nan), pixel_m)
            water = np.isclose(scl_s[0], 6) & both
            stats["diff_on_summer_scl_water"] = summarize(np.where(water, diff, np.nan), pixel_m)

    stats_path = out_dir / "stats.json"
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 {stats_path}")

    if args.demo:
        titles = ("合成夏季", "合成冬季", "合成差值（夏−冬）")
        suptitle_note = "demo"
    else:
        titles = (
            season_title(catalog, "summer", "夏季"),
            season_title(catalog, "winter", "冬季"),
            "季节差值（夏 − 冬）",
        )
        suptitle_note = "real"
    fig_dir = out_dir / "figures"
    draw_maps(summer, winter, diff, titles, fig_dir / "ndvi_season_panel.png", pixel_m)
    draw_hist(summer, winter, diff, fig_dir / "ndvi_hist.png")

    print("\n===== 摘要 =====")
    print(f"模式：{'合成自测' if args.demo else '真实 L2A'} ({suptitle_note})")
    for name, block in (("夏季", stats["summer"]), ("冬季", stats["winter"]), ("差值", stats["diff"])):
        print(
            f"{name}  n={block.get('n')}  mean={block.get('mean')}  "
            f"p50={block.get('p50')}  min={block.get('min')}  max={block.get('max')}"
        )
    for row in zones:
        print(
            f"分区 {row['name']}: 夏 {row['summer_mean']} / 冬 {row['winter_mean']} / 差 {row['diff_mean']}"
        )


if __name__ == "__main__":
    main()
