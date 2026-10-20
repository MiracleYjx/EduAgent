param(
    [string]$Python = 'python',
    [string]$Npm = 'npm.cmd',
    [string]$BuildRoot = '',
    [switch]$WithLocalModels
)
$ErrorActionPreference = 'Stop'
$taskRepo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $BuildRoot) { $BuildRoot = Join-Path $taskRepo '.cache/exe-build' }
$taskBuild = [IO.Path]::GetFullPath($BuildRoot)
if (-not $taskBuild.StartsWith($taskRepo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'BuildRoot 必须位于仓库内部的独立构建目录。'
}
New-Item -ItemType Directory -Path $taskBuild -Force | Out-Null
$taskRuntime = Join-Path $taskBuild 'runtime'
$taskPython = Join-Path $taskRuntime 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    & $Python -m venv $taskRuntime
    if ($LASTEXITCODE -ne 0) { throw '创建独立构建环境失败。' }
}
& $taskPython -c 'import sys; assert sys.version_info >= (3,12), "Requires Python 3.12+"'
if ($LASTEXITCODE -ne 0) { throw '构建 Python 版本不支持。' }
& $taskPython -m pip install -r (Join-Path $taskRepo 'packaging/requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw '固定构建依赖安装失败。' }
if ($WithLocalModels) {
    & $taskPython -m pip install 'sentence-transformers>=3.0'
    if ($LASTEXITCODE -ne 0) { throw '本地模型运行库安装失败。' }
    $env:EDUAGENT_BUILD_LOCAL_MODELS = '1'
} else { $env:EDUAGENT_BUILD_LOCAL_MODELS = '0' }
& $taskPython -m pip check
if ($LASTEXITCODE -ne 0) { throw '构建环境依赖冲突。' }
# Each build gets a new output directory; a failed build cannot replace a prior package.
$taskSession = Join-Path $taskBuild ('build-' + [guid]::NewGuid().ToString('N'))
$taskDist = Join-Path $taskSession 'dist'
$taskWork = Join-Path $taskSession 'work'
$taskPreviousPython = $env:EDUAGENT_PYTHON
$env:EDUAGENT_PYTHON = $taskPython
Push-Location (Join-Path $taskRepo 'frontend')
try {
    & $Npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw '锁定前端依赖安装失败。' }
    & $Npm run build
    if ($LASTEXITCODE -ne 0) { throw '设置页类型检查或生产构建失败。' }
} finally {
    Pop-Location
    $env:EDUAGENT_PYTHON = $taskPreviousPython
}
Push-Location $taskRepo
try {
    & $taskPython -m PyInstaller --noconfirm --distpath $taskDist --workpath $taskWork (Join-Path $taskRepo 'packaging/EduAgent.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller onedir 构建失败。' }
    $taskPackage = Join-Path $taskDist 'EduAgent'
    Copy-Item -LiteralPath (Join-Path $taskRepo 'packaging/README.md') -Destination (Join-Path $taskPackage 'README.md')
    Copy-Item -LiteralPath (Join-Path $taskRepo 'packaging/Configure-EduAgent.cmd') -Destination (Join-Path $taskPackage 'Configure-EduAgent.cmd')
    & $taskPython (Join-Path $taskRepo 'packaging/build_receipt.py') --package $taskPackage --output (Join-Path $taskPackage 'build-receipt.json')
    if ($LASTEXITCODE -ne 0) { throw '构建回执生成失败。' }
    Write-Host "构建完成：$taskPackage"
    Write-Host '完整目录复制交付；配置/数据库/Redis/模型及实际验收见 README。'
} finally {
    Pop-Location
    Remove-Item Env:EDUAGENT_BUILD_LOCAL_MODELS -ErrorAction SilentlyContinue
}
