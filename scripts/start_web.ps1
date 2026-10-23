[CmdletBinding()]
param(
    [int]$Port = 8000,
    [int]$PostgresPort = 25432,
    [int]$RedisPort = 26379,
    [string]$ProjectName = 'eduagent-local',
    [string]$DebianMirror = 'deb.debian.org',
    [switch]$Rebuild,
    [switch]$Setup,
    [switch]$NoBrowser,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 旧版控制台可能不允许修改编码；不影响启动。
}

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-HttpStatus {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return [int]$response.StatusCode
    } catch {
        return 0
    }
}

function Test-SetupReady {
    return (Get-HttpStatus "http://127.0.0.1:$Port/setup-ready") -eq 200
}

function Test-BusinessReady {
    return (Get-HttpStatus "http://127.0.0.1:$Port/ready") -eq 200
}

function Test-DockerEngine {
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $command) {
        return $false
    }
    & docker version --format '{{.Server.Version}}' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Docker {
    if (Test-DockerEngine) {
        return
    }

    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        throw '未找到 docker 命令。请先安装 Docker Desktop，并确认它已加入 PATH。'
    }

    Write-Step 'Docker 引擎未运行，正在尝试启动 Docker Desktop...'
    $dockerExe = $dockerCommand.Source
    $dockerRoot = Split-Path (Split-Path (Split-Path $dockerExe -Parent) -Parent) -Parent
    $desktop = Join-Path $dockerRoot 'Docker Desktop.exe'
    if (Test-Path $desktop) {
        Start-Process $desktop | Out-Null
    } else {
        Write-Warning '未找到 Docker Desktop.exe，请手动启动 Docker Desktop。'
    }

    $deadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-DockerEngine) {
            Write-Step 'Docker 引擎已就绪。'
            return
        }
    }
    throw 'Docker 引擎未在 150 秒内就绪。请打开 Docker Desktop，等待状态变为 Running 后重试。'
}

function Test-Compose {
    & docker compose version 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-MirrorIp {
    param([string]$HostName)
    if (-not $HostName) {
        return $null
    }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        return $null
    }
    try {
        $url = "https://dns.alidns.com/resolve?name=$HostName&type=A"
        $raw = & curl.exe -sS --ssl-no-revoke --max-time 10 --resolve 'dns.alidns.com:443:223.5.5.5' $url 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) {
            return $null
        }
        $data = ($raw -join '') | ConvertFrom-Json
        $answer = $data.Answer | Where-Object { $_.type -eq 1 } | Select-Object -First 1
        if ($answer -and $answer.data -match '^\d{1,3}(\.\d{1,3}){3}$') {
            return $answer.data
        }
    } catch {
        return $null
    }
    return $null
}

Write-Host ''
Write-Host 'EduAgent 一键启动' -ForegroundColor Green
Write-Host "项目目录: $repoRoot"
Write-Host ''

$baseUrl = "http://127.0.0.1:$Port"

if (Test-SetupReady) {
    Write-Step "检测到 EduAgent 已在 $baseUrl 运行。"
} else {
    Ensure-Docker
    if (-not (Test-Compose)) {
        throw 'Docker Compose 不可用。请升级 Docker Desktop，确认 docker compose version 能正常执行。'
    }

    $occupied = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($occupied) {
        throw "端口 $Port 已被其他程序占用。请先关闭占用程序，或使用 -Port 指定其他端口。"
    }

    $env:POSTGRES_PORT = [string]$PostgresPort
    $env:REDIS_PORT = [string]$RedisPort
    $env:BACKEND_PORT = [string]$Port

    $compose = @('compose', '-p', $ProjectName)
    $imageIds = @(& docker @compose 'images' '-q' 'backend' 2>$null)
    $hasImage = (($imageIds -join '').Trim().Length -gt 0)
    if ($Rebuild -or -not $hasImage) {
        $buildArgs = @('build')
        if ($DebianMirror) {
            $buildArgs += @('--build-arg', "DEBIAN_MIRROR=$DebianMirror")
            $mirrorIp = Resolve-MirrorIp $DebianMirror
            if ($mirrorIp) {
                $buildArgs += @('--add-host', ($DebianMirror + ':' + $mirrorIp))
                Write-Step "已将 $DebianMirror 固定到真实 IP $mirrorIp，避开 Docker fake-IP。"
            }
        }
        $buildArgs += @('-t', ($ProjectName + '-backend'), '.')
        Write-Step "首次启动或指定了 -Rebuild，开始构建镜像（Debian 源: $DebianMirror）..."
        & docker @buildArgs
        if ($LASTEXITCODE -ne 0) {
            throw '镜像构建失败。请检查上方日志、网络和 Docker Desktop 状态。'
        }
    } else {
        Write-Step '启动现有镜像...'
    }

    $upArgs = $compose + @('up', '-d')
    & docker @upArgs
    if ($LASTEXITCODE -ne 0) {
        throw '容器启动失败。请检查 Docker Desktop 状态和上方日志。'
    }
}

Write-Step "等待服务就绪（最长 $TimeoutSeconds 秒）..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if (Test-SetupReady) {
        break
    }
    Start-Sleep -Seconds 2
}

if (-not (Test-SetupReady)) {
    Write-Host '--- backend 最近日志 ---' -ForegroundColor Yellow
    & docker compose -p $ProjectName logs --tail 50 backend
    throw "服务未在 $TimeoutSeconds 秒内就绪。请查看上方日志，或运行 docker compose -p $ProjectName logs backend 排查。"
}

$targetUrl = $null
if (-not $Setup -and (Test-BusinessReady)) {
    $targetUrl = "$baseUrl/"
    Write-Step '业务已就绪，直接打开应用。'
} else {
    Write-Step '业务尚未初始化或指定了 -Setup，正在获取一次性设置授权链接...'
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        $output = @(& docker compose -p $ProjectName exec -T backend python -m backend.app.deployment.container_control --port $Port 2>&1)
        if ($LASTEXITCODE -eq 0) {
            $line = $output | Where-Object { [string]$_ -match '^https?://' } | Select-Object -Last 1
            if ($line) {
                $targetUrl = ([string]$line).Trim()
                break
            }
        }
        Start-Sleep -Seconds 2
    }

    if (-not $targetUrl) {
        Write-Host '--- backend 最近日志 ---' -ForegroundColor Yellow
        & docker compose -p $ProjectName logs --tail 50 backend
        throw '未能获取设置授权链接。请确认 backend 容器正常运行，并查看上方日志。'
    }
}

if ($NoBrowser) {
    Write-Host ''
    Write-Host "请在浏览器打开: $targetUrl" -ForegroundColor Yellow
} else {
    Write-Step '正在打开浏览器...'
    try {
        Start-Process $targetUrl | Out-Null
    } catch {
        Write-Warning "无法自动打开浏览器，请手动访问: $targetUrl"
    }
}

Write-Host ''
Write-Host 'EduAgent 已启动。' -ForegroundColor Green
Write-Host "应用地址: $baseUrl/"
Write-Host "停止服务: docker compose -p $ProjectName stop"
Write-Host "查看日志: docker compose -p $ProjectName logs -f backend"
if ($targetUrl -ne "$baseUrl/") {
    Write-Host "设置地址: $targetUrl"
}
