# -*- coding: utf-8 -*-
"""
在 QGIS 3.44 里复现裁剪、NDVI、季节差值，并导出带图例、比例尺和指北针的 PNG。

不要用系统 Python 运行。请用 OSGeo4W 的 python-qgis：

    E:\\QGIS\\bin\\python-qgis-ltr.bat qgis\\export_ndvi_maps.py

输入是 scripts/fetch_s2_window.py 写出的地表反射率（波段 3 = B04，波段 4 = B08）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qgis.core import (
    QgsApplication,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPrintLayout,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsTextFormat,
    QgsUnitTypes,
)
from qgis.PyQt.QtGui import QColor, QFont

QGIS_PREFIX = Path(r"E:\QGIS\apps\qgis-ltr")
NORTH_ARROW = Path(r"E:\QGIS\apps\qgis-ltr\svg\arrows\NorthArrow_10.svg")
PYQGIS_PLUGINS = Path(r"E:\QGIS\apps\qgis-ltr\python\plugins")

NDVI_STOPS = (
    (-0.2, "#2c7bb6", "−0.2 低"),
    (0.0, "#ffffbf", "0 建成/裸地"),
    (0.25, "#a6d96a", "0.25"),
    (0.5, "#1a9641", "0.5 植被"),
    (0.8, "#004529", "0.8 密"),
)
DIFF_STOPS = (
    (-0.3, "#8c510a", "−0.3 冬季更高"),
    (0.0, "#f7f7f7", "0"),
    (0.3, "#01665e", "0.3 夏季更高"),
)


def yahei(point_size: int) -> QFont:
    return QFont("Microsoft YaHei", point_size)


def text_format(point_size: int, bold: bool = False) -> QgsTextFormat:
    fmt = QgsTextFormat()
    font = yahei(point_size)
    font.setBold(bold)
    fmt.setFont(font)
    fmt.setSize(point_size)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    return fmt


def ramp_type():
    if hasattr(QgsColorRampShader, "Interpolated"):
        return QgsColorRampShader.Interpolated
    return QgsColorRampShader.Type.Interpolated


def style_raster(layer: QgsRasterLayer, stops, vmin: float, vmax: float):
    shader = QgsRasterShader()
    function = QgsColorRampShader()
    function.setColorRampType(ramp_type())
    items = [
        QgsColorRampShader.ColorRampItem(value, QColor(color), label)
        for value, color, label in stops
    ]
    function.setColorRampItemList(items)
    shader.setRasterShaderFunction(function)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(vmin)
    renderer.setClassificationMax(vmax)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def run_clip_and_indices(processing):
    out_dir = ROOT / "outputs" / "qgis"
    out_dir.mkdir(parents=True, exist_ok=True)
    aoi = str(ROOT / "data" / "raw" / "aoi.geojson")
    summer_in = str(ROOT / "data" / "raw" / "s2_summer_boa.tif")
    winter_in = str(ROOT / "data" / "raw" / "s2_winter_boa.tif")
    summer_clip = str(out_dir / "summer_clip.tif")
    winter_clip = str(out_dir / "winter_clip.tif")
    summer_ndvi = str(out_dir / "ndvi_summer.tif")
    winter_ndvi = str(out_dir / "ndvi_winter.tif")
    diff_path = str(out_dir / "ndvi_diff.tif")

    def clip(src, dest):
        print(f"QGIS 裁剪 {src}")
        processing.run(
            "gdal:cliprasterbymasklayer",
            {
                "INPUT": src,
                "MASK": aoi,
                "NODATA": -9999,
                "ALPHA_BAND": False,
                "CROP_TO_CUTLINE": True,
                "KEEP_RESOLUTION": True,
                "SET_RESOLUTION": False,
                "MULTITHREADING": True,
                "DATA_TYPE": 0,
                "OUTPUT": dest,
            },
        )

    def calculator(path_a, band_a, path_b, band_b, formula, dest):
        print(f"QGIS 栅格计算 → {dest}")
        processing.run(
            "gdal:rastercalculator",
            {
                "INPUT_A": path_a,
                "BAND_A": band_a,
                "INPUT_B": path_b,
                "BAND_B": band_b,
                "FORMULA": formula,
                "NO_DATA": -9999,
                "EXTENT_OPT": 0,
                "RTYPE": 5,
                "OUTPUT": dest,
            },
        )

    clip(summer_in, summer_clip)
    clip(winter_in, winter_clip)
    # A = B04 红（第 3 波段），B = B08 近红外（第 4 波段）
    # 反射率 < 0 或无数据（-9999）都不参与比值，避免暗像元噪声把 NDVI 推出 [-1, 1]
    ndvi_formula = (
        "numpy.where((A<0)|(B<0)|(A>1)|(B>1)|(numpy.abs(A+B)<1e-6), -9999, (B-A)/(A+B))"
    )
    calculator(summer_clip, 3, summer_clip, 4, ndvi_formula, summer_ndvi)
    calculator(winter_clip, 3, winter_clip, 4, ndvi_formula, winter_ndvi)
    # A = 冬季 NDVI，B = 夏季 NDVI，输出夏季减冬季
    diff_formula = "numpy.where((A<-100)|(B<-100), -9999, B-A)"
    calculator(winter_ndvi, 1, summer_ndvi, 1, diff_formula, diff_path)
    return {
        "summer": summer_ndvi,
        "winter": winter_ndvi,
        "diff": diff_path,
    }


def raster_summary(path: str) -> dict:
    from osgeo import gdal
    import numpy as np

    dataset = gdal.Open(path)
    if dataset is None:
        raise RuntimeError(f"GDAL 无法打开 {path}")
    band = dataset.GetRasterBand(1)
    arr = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    valid = np.isfinite(arr) if nodata is None else (np.isfinite(arr) & ~np.isclose(arr, nodata))
    values = arr[valid]
    dataset = None
    if values.size == 0:
        return {"path": path, "n": 0}
    return {
        "path": path,
        "n": int(values.size),
        "min": round(float(values.min()), 4),
        "max": round(float(values.max()), 4),
        "mean": round(float(values.mean()), 4),
        "std": round(float(values.std()), 4),
    }


def apply_legend_fonts(legend):
    try:
        from qgis.core import QgsLegendStyle

        settings = legend.legendSettings()
        for component, size in (
            (QgsLegendStyle.Title, 10),
            (QgsLegendStyle.Subgroup, 8),
            (QgsLegendStyle.SymbolLabel, 8),
        ):
            style = settings.style(component)
            style.setTextFormat(text_format(size))
            settings.setStyle(component, style)
        if hasattr(legend, "setLegendSettings"):
            legend.setLegendSettings(settings)
    except Exception as exc:
        print(f"图例字体未设置：{exc}")


def publish_png(exporter, png_path: Path, settings):
    """先写到临时文件再替换，避免 PNG 驱动不能原地更新，也避免旧图被占用时整段失败。"""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = png_path.with_name(f"{png_path.stem}.writing.png")
    if temporary.exists():
        temporary.unlink()
    status = exporter.exportToImage(str(temporary), settings)
    if status != QgsLayoutExporter.Success:
        raise RuntimeError(f"导出失败 {png_path}，状态码 {status}")
    try:
        os.replace(temporary, png_path)
    except PermissionError:
        fallback = png_path.with_name(f"{png_path.stem}.new.png")
        os.replace(temporary, fallback)
        print(
            f"无法覆盖 {png_path.name}：文件正被其他程序打开。"
            f"请关掉 Cursor 预览、照片或资源管理器里的这张图。新图已写到 {fallback}"
        )
        return
    print(f"已导出 {png_path}")


def add_label(layout, text, x, y, w, h, size, bold=False):
    label = QgsLayoutItemLabel(layout)
    label.setText(text)
    label.setTextFormat(text_format(size, bold=bold))
    label.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
    label.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(label)
    return label


def set_band_description(path: str, description: str):
    from osgeo import gdal

    dataset = gdal.Open(path, gdal.GA_Update)
    if dataset is None:
        return
    dataset.GetRasterBand(1).SetDescription(description)
    dataset = None


def export_layout(path: str, title: str, subtitle: str, stops, vmin: float, vmax: float, legend_title: str, png_path: Path):
    resolution = "20 m"
    catalog_path = ROOT / "data" / "raw" / "scene_catalog.json"
    if catalog_path.exists():
        try:
            meta = json.loads(catalog_path.read_text(encoding="utf-8"))
            resolution = f"{float(meta.get('target_grid', {}).get('resolution_m', 20)):.0f} m"
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            resolution = "20 m"
    subtitle = f"{subtitle}  ·  {resolution}"

    project = QgsProject.instance()
    project.clear()
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:32649"))
    set_band_description(path, legend_title)
    layer = QgsRasterLayer(path, legend_title)
    if not layer.isValid():
        raise RuntimeError(f"图层无效：{path} / {layer.error().message()}")
    style_raster(layer, stops, vmin, vmax)
    project.addMapLayer(layer)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(title)
    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(297, 210, QgsUnitTypes.LayoutMillimeters))

    add_label(layout, title, 10, 3, 270, 9, 15, bold=True)
    add_label(layout, subtitle, 10, 12, 275, 8, 8, bold=False)

    map_item = QgsLayoutItemMap(layout)
    map_item.attemptMove(QgsLayoutPoint(10, 22, QgsUnitTypes.LayoutMillimeters))
    map_item.attemptResize(QgsLayoutSize(198, 160, QgsUnitTypes.LayoutMillimeters))
    map_item.setFrameEnabled(True)
    map_item.setBackgroundColor(QColor(208, 208, 208))
    map_item.setLayers([layer])
    extent = layer.extent()
    extent.scale(1.04)
    map_item.setExtent(extent)
    layout.addLayoutItem(map_item)

    legend = QgsLayoutItemLegend(layout)
    legend.setLinkedMap(map_item)
    legend.setTitle("图例")
    legend.setBackgroundEnabled(True)
    legend.setFrameEnabled(True)
    apply_legend_fonts(legend)
    legend.attemptMove(QgsLayoutPoint(214, 28, QgsUnitTypes.LayoutMillimeters))
    legend.attemptResize(QgsLayoutSize(72, 110, QgsUnitTypes.LayoutMillimeters))
    legend.setAutoUpdateModel(True)
    layout.addLayoutItem(legend)

    scale = QgsLayoutItemScaleBar(layout)
    scale.setLinkedMap(map_item)
    scale.setStyle("Single Box")
    scale.applyDefaultSettings()
    scale.setUnits(QgsUnitTypes.DistanceKilometers)
    scale.setNumberOfSegments(2)
    scale.setNumberOfSegmentsLeft(0)
    scale.setUnitsPerSegment(1)
    scale.setUnitLabel("km")
    scale.setTextFormat(text_format(8))
    scale.attemptMove(QgsLayoutPoint(14, 168, QgsUnitTypes.LayoutMillimeters))
    scale.attemptResize(QgsLayoutSize(60, 12, QgsUnitTypes.LayoutMillimeters))
    scale.update()
    layout.addLayoutItem(scale)

    if NORTH_ARROW.exists():
        arrow = QgsLayoutItemPicture(layout)
        arrow.setPicturePath(str(NORTH_ARROW))
        if hasattr(arrow, "setMode") and hasattr(QgsLayoutItemPicture, "FormatSVG"):
            arrow.setMode(QgsLayoutItemPicture.FormatSVG)
        arrow.setLinkedMap(map_item)
        north_mode = getattr(QgsLayoutItemPicture, "GridNorth", None)
        if north_mode is None and hasattr(QgsLayoutItemPicture, "NorthMode"):
            north_mode = QgsLayoutItemPicture.NorthMode.GridNorth
        if north_mode is not None and hasattr(arrow, "setNorthMode"):
            arrow.setNorthMode(north_mode)
        arrow.attemptMove(QgsLayoutPoint(190, 28, QgsUnitTypes.LayoutMillimeters))
        arrow.attemptResize(QgsLayoutSize(12, 16, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(arrow)

    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = 180
    publish_png(exporter, png_path, settings)
    project.removeAllMapLayers()


def scene_bits():
    catalog_path = ROOT / "data" / "raw" / "scene_catalog.json"
    if not catalog_path.exists():
        return "夏季", "冬季", ""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    scenes = catalog.get("scenes", {})
    summer = scenes.get("summer", {})
    winter = scenes.get("winter", {})
    summer_txt = f"{summer.get('role', '夏季')} {summer.get('date', '')}".strip()
    winter_txt = f"{winter.get('role', '冬季')} {winter.get('date', '')}".strip()

    def fmt_cloud(value):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    clouds = (
        f"整景云量 夏 {fmt_cloud(summer.get('eo_cloud_cover'))}% / "
        f"冬 {fmt_cloud(winter.get('eo_cloud_cover'))}%"
    )
    return summer_txt, winter_txt, clouds


def main():
    for path in (
        ROOT / "data" / "raw" / "s2_summer_boa.tif",
        ROOT / "data" / "raw" / "s2_winter_boa.tif",
        ROOT / "data" / "raw" / "aoi.geojson",
    ):
        if not path.exists():
            raise SystemExit(f"缺少输入 {path}。先运行 python scripts/fetch_s2_window.py")

    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        if str(PYQGIS_PLUGINS) not in sys.path:
            sys.path.append(str(PYQGIS_PLUGINS))
        from processing.core.Processing import Processing
        import processing
        from qgis.analysis import QgsNativeAlgorithms
        from processing.algs.gdal.GdalAlgorithmProvider import GdalAlgorithmProvider

        Processing.initialize()
        registry = QgsApplication.processingRegistry()
        if not registry.algorithmById("gdal:rastercalculator"):
            registry.addProvider(GdalAlgorithmProvider())
        if not registry.algorithmById("native:rasterlayerstatistics"):
            registry.addProvider(QgsNativeAlgorithms())

        paths = run_clip_and_indices(processing)
        summaries = {key: raster_summary(value) for key, value in paths.items()}
        summary_path = ROOT / "outputs" / "qgis" / "qgis_stats.json"
        summary_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存 {summary_path}")
        for key, block in summaries.items():
            print(f"QGIS {key}: n={block.get('n')} mean={block.get('mean')} min={block.get('min')} max={block.get('max')}")

        summer_txt, winter_txt, clouds = scene_bits()
        formula = "NDVI = (B08 − B04) / (B08 + B04)，L2A 地表反射率"
        export_layout(
            paths["summer"],
            f"澳门近岸 NDVI · {summer_txt}",
            formula + "  ·  " + clouds,
            NDVI_STOPS,
            -0.2,
            0.8,
            "夏季 NDVI",
            ROOT / "outputs" / "maps" / "ndvi_summer_qgis.png",
        )
        export_layout(
            paths["winter"],
            f"澳门近岸 NDVI · {winter_txt}",
            formula + "  ·  " + clouds,
            NDVI_STOPS,
            -0.2,
            0.8,
            "冬季 NDVI",
            ROOT / "outputs" / "maps" / "ndvi_winter_qgis.png",
        )
        export_layout(
            paths["diff"],
            "NDVI 季节差值（夏季 − 冬季）",
            "正值表示夏季 NDVI 更高；|差值| 的配色范围是 −0.3 到 0.3  ·  " + clouds,
            DIFF_STOPS,
            -0.3,
            0.3,
            "NDVI 差值",
            ROOT / "outputs" / "maps" / "ndvi_diff_qgis.png",
        )
    finally:
        qgs.exitQgis()


if __name__ == "__main__":
    main()
