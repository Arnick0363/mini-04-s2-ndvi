# -*- coding: utf-8 -*-
"""
make_test_scene.py — 生成合成双波段栅格，只用来离线检查 NDVI 公式是否跑通。

这不是 Sentinel-2，也不位于澳门。坐标只是 EPSG:32649 下的一块占位格网，
地物反射率是按典型光谱手写的。作品集结果请用 run_pipeline.py 下载的真实 L2A。

用法：python make_test_scene.py
      python compute_ndvi.py --demo
"""

import os

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.crs import CRS


def write_tif(path, red, nir, transform, crs):
    """把红/近红外两个波段写成一个 GeoTIFF。"""
    with rasterio.open(
        path, "w", driver="GTiff", height=red.shape[0], width=red.shape[1],
        count=2, dtype="float32", crs=crs, transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(red.astype("float32"), 1)   # B04 红
        dst.write(nir.astype("float32"), 2)   # B08 近红外
        dst.set_band_description(1, "B04_Red")
        dst.set_band_description(2, "B08_NIR")


def build_scene(season: str):
    """模拟一景影像：城区 + 水体 + 植被混合地表。"""
    h, w = 600, 600
    rng = np.random.default_rng(7 if season == "summer" else 8)

    yy, xx = np.mgrid[0:h, 0:w]

    # 地物图案：左下城区 / 右侧水体 / 上方植被
    urban = (xx < w * 0.35) & (yy > h * 0.55)
    water = xx > w * 0.75
    veg = ~urban & ~water  # 其余为植被

    # L2A 是地表反射率（0-1 量级），典型光谱特征：
    # 植被: 红低近红外高（强吸收/强反射）；水体: 双低；城区: 双中等
    red = np.full((h, w), 0.08)
    nir = np.full((h, w), 0.10)

    red[veg] = 0.03
    nir[veg] = 0.38 if season == "summer" else 0.15   # 冬季植被枯黄：近红外大幅下降
    red[water] = 0.02
    nir[water] = 0.01
    red[urban] = 0.12
    nir[urban] = 0.14

    # 加一点传感器噪声让画面更真实
    red += rng.normal(0, 0.004, (h, w))
    nir += rng.normal(0, 0.006, (h, w))
    np.clip(red, 0, 1, out=red)
    np.clip(nir, 0, 1, out=nir)
    return red, nir


def main():
    os.makedirs("data/raw", exist_ok=True)
    # 占位格网，约 6 km × 6 km，EPSG:32649。不要把它当成澳门的真实位置。
    bounds = (530000, 2455000, 536000, 2461000)
    transform = from_bounds(*bounds, 600, 600)
    crs = CRS.from_epsg(32649)

    for season in ("summer", "winter"):
        red, nir = build_scene(season)
        path = f"data/raw/sim_{season}.tif"
        write_tif(path, red, nir, transform, crs)
        print(f"已生成合成栅格：{path}（600x600，波段1=红，波段2=近红外，不是真实卫星数据）")
    print("未改写 data/raw/aoi.geojson（该文件是澳门研究区，与合成格网无关）")


if __name__ == "__main__":
    main()
