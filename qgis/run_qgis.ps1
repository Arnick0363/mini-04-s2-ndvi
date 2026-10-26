# 用本机 QGIS 3.44.14（OSGeo4W，E:\QGIS）跑裁剪、NDVI 和制图。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Bat = "E:\QGIS\bin\python-qgis-ltr.bat"
if (-not (Test-Path -LiteralPath $Bat)) {
    throw "找不到 $Bat 。请确认 QGIS 3.44.14 安装在 E:\QGIS。"
}
Set-Location -LiteralPath $Root
& $Bat (Join-Path $PSScriptRoot "export_ndvi_maps.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
