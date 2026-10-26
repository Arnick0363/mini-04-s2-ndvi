# -*- coding: utf-8 -*-
"""
一条命令复现：下载研究区窗口 → 计算 NDVI → 调用 QGIS 3.44 裁剪、计算并出图。

    python run_pipeline.py
    python run_pipeline.py --skip-fetch
    python run_pipeline.py --skip-qgis
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parent
QGIS_BAT = Path(r"E:\QGIS\bin\python-qgis-ltr.bat")


def run(cmd):
    print("\n>>>", " ".join(str(part) for part in cmd), flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PROJ_NETWORK"] = "OFF"
    subprocess.check_call(cmd, cwd=ROOT, env=env)


def summarize(path: Path):
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        transform = src.transform
        nodata = src.nodata
        shape = arr.shape
    if nodata is not None:
        arr[np.isclose(arr, float(nodata))] = np.nan
    values = arr[np.isfinite(arr)]
    return {
        "shape": list(shape),
        "n": int(values.size),
        "mean": None if values.size == 0 else round(float(values.mean()), 4),
        "std": None if values.size == 0 else round(float(values.std()), 4),
        "min": None if values.size == 0 else round(float(values.min()), 4),
        "max": None if values.size == 0 else round(float(values.max()), 4),
        "transform": [transform.a, transform.b, transform.c, transform.d, transform.e, transform.f],
    }


def compare_backends():
    pairs = {
        "summer": ("outputs/ndvi_summer.tif", "outputs/qgis/ndvi_summer.tif"),
        "winter": ("outputs/ndvi_winter.tif", "outputs/qgis/ndvi_winter.tif"),
        "diff": ("outputs/ndvi_diff.tif", "outputs/qgis/ndvi_diff.tif"),
    }
    report = {}
    for name, (py_rel, qgis_rel) in pairs.items():
        py_path = ROOT / py_rel
        qgis_path = ROOT / qgis_rel
        if not qgis_path.exists():
            report[name] = {"status": "缺少 QGIS 输出"}
            continue
        py_stats = summarize(py_path)
        qgis_stats = summarize(qgis_path)
        item = {"python": py_stats, "qgis": qgis_stats}
        if py_stats["shape"] == qgis_stats["shape"] and py_stats["transform"] == qgis_stats["transform"]:
            with rasterio.open(py_path) as a_src, rasterio.open(qgis_path) as b_src:
                a = a_src.read(1).astype("float64")
                b = b_src.read(1).astype("float64")
                if a_src.nodata is not None:
                    a[np.isclose(a, float(a_src.nodata))] = np.nan
                if b_src.nodata is not None:
                    b[np.isclose(b, float(b_src.nodata))] = np.nan
            both = np.isfinite(a) & np.isfinite(b)
            if both.any():
                rmse = float(np.sqrt(np.mean((a[both] - b[both]) ** 2)))
                item["overlap_n"] = int(both.sum())
                item["rmse"] = round(rmse, 6)
                item["max_abs"] = round(float(np.max(np.abs(a[both] - b[both]))), 6)
        else:
            item["note"] = "格网不完全一致，只比较全区统计。裁剪边缘的取舍会让形状差几个像元。"
        if py_stats["mean"] is not None and qgis_stats["mean"] is not None:
            item["mean_abs_diff"] = round(abs(py_stats["mean"] - qgis_stats["mean"]), 6)
        report[name] = item
        print(
            f"对照 {name}: Python mean={py_stats['mean']}  QGIS mean={qgis_stats['mean']}  "
            f"mean_abs_diff={item.get('mean_abs_diff')}  rmse={item.get('rmse')}"
        )
    dest = ROOT / "outputs" / "backend_compare.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 {dest}")
    return report


def main():
    parser = argparse.ArgumentParser(description="复现 NDVI 季节对比")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-qgis", action="store_true")
    parser.add_argument("--force-fetch", action="store_true")
    args = parser.parse_args()

    if not args.skip_fetch:
        cmd = [sys.executable, str(ROOT / "scripts" / "fetch_s2_window.py")]
        if args.force_fetch:
            cmd.append("--force")
        run(cmd)
    run([sys.executable, str(ROOT / "compute_ndvi.py")])
    if args.skip_qgis:
        print("已跳过 QGIS。需要出图时运行：powershell -ExecutionPolicy Bypass -File qgis\\run_qgis.ps1")
        return
    if not QGIS_BAT.exists():
        raise SystemExit(f"找不到 {QGIS_BAT}，Python 结果已写出，QGIS 制图未跑。")
    run(["cmd", "/c", str(QGIS_BAT), str(ROOT / "qgis" / "export_ndvi_maps.py")])
    compare_backends()


if __name__ == "__main__":
    main()
