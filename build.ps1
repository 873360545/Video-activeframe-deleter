# 监控分析器 — 一键构建便携 Windows 包
# 用法：powershell -ExecutionPolicy Bypass -File build.ps1   （或双击 build.bat）
# PS5.1 注意：native 程序的 stderr 输出（unittest -v / PyInstaller 日志）会被包装成
# ErrorRecord，若设为 'Stop' 会误杀脚本。故用 'Continue'，关键步骤显式检查 $LASTEXITCODE。
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'

Write-Host '[1/5] 检查虚拟环境...'
if (-not (Test-Path $py)) {
    py -3.14 -m venv (Join-Path $root '.venv')
}
if (-not (Test-Path $py)) { throw '未找到 Python 3.14（py 启动器），无法创建虚拟环境' }

Write-Host '[2/5] 安装依赖...'
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }

Write-Host '[3/5] 运行单元测试...'
& $py -m unittest discover -s tests -t $root -v
if ($LASTEXITCODE -ne 0) { throw '单元测试未通过' }

Write-Host '[4/5] PyInstaller 打包...'
& $py -m PyInstaller (Join-Path $root 'build\monitor_analyzer.spec') --noconfirm --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build\work')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败' }

Write-Host '[5/5] 压缩便携包...'
$zip = Join-Path $root 'dist\监控分析器_portable.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $root 'dist\监控分析器') -DestinationPath $zip -Force
Write-Host ''
Write-Host "完成！便携包：$zip"
