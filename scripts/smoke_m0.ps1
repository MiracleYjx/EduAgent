<#
.SYNOPSIS
执行 M0 三容器、数据库迁移和 Redis 连接冒烟验证。

.DESCRIPTION
启动 PostgreSQL、Redis 和 Backend，等待容器健康检查与 Backend
健康端点可用，然后执行 Alembic 迁移和 Redis PING。脚本失败时会输出
脱敏诊断并以非零退出码结束。
#>

[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180,
    [int]$PollIntervalSeconds = 5
)

$ErrorActionPreference = "Stop"
$serviceNames = @("postgres", "redis", "backend")

function ConvertTo-SafeText {
    param(
        [AllowNull()]
        [string]$Text
    )

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return ""
    }

    $safeText = $Text
    $safeText = $safeText -replace "(?i)(api[_-]?key|password|token|authorization|secret)(\s*[:=]\s*)\S+", '$1$2[已脱敏]'
    $safeText = $safeText -replace "(?i)(postgres(?:ql)?\+?\w*://|redis://)\S+", '$1[已脱敏]'
    return $safeText
}

function Invoke-DockerCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = @(& docker @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    return [PSCustomObject]@{
        ExitCode = $exitCode
        Output   = $text
    }
}

function Write-CommandOutput {
    param(
        [AllowNull()]
        [string]$Output
    )

    if ([string]::IsNullOrWhiteSpace($Output)) {
        return
    }

    foreach ($line in ($Output -split "`r?`n")) {
        if (-not [string]::IsNullOrWhiteSpace($line)) {
            Write-Host (ConvertTo-SafeText $line)
        }
    }
}

function Get-ComposeServiceStates {
    $result = Invoke-DockerCommand -Arguments @(
        "compose",
        "ps",
        "--all",
        "--format",
        "{{.Service}}|{{.State}}|{{.Health}}"
    )
    if ($result.ExitCode -ne 0) {
        throw "读取 Compose 服务状态失败：$(ConvertTo-SafeText $result.Output)"
    }

    $states = @{}
    foreach ($line in ($result.Output -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }

        $parts = $line -split "\|", 3
        if ($parts.Count -lt 3) {
            continue
        }

        $states[$parts[0]] = [PSCustomObject]@{
            Service = $parts[0]
            State   = $parts[1]
            Health  = $parts[2]
        }
    }

    return ,$states
}

function Get-ServiceSummary {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$States
    )

    $items = foreach ($serviceName in $serviceNames) {
        if ($States.ContainsKey($serviceName)) {
            "$serviceName=$($States[$serviceName].State)/$($States[$serviceName].Health)"
        }
        else {
            "$serviceName=未发现"
        }
    }
    return ($items -join ", ")
}

function Wait-ForComposeServices {
    param(
        [Parameter(Mandatory = $true)]
        [datetime]$Deadline
    )

    $lastSummary = ""
    while ([DateTime]::UtcNow -lt $Deadline) {
        $states = Get-ComposeServiceStates
        $summary = Get-ServiceSummary -States $states
        if ($summary -ne $lastSummary) {
            Write-Host "等待容器健康检查：$summary"
            $lastSummary = $summary
        }

        $allHealthy = $true
        foreach ($serviceName in $serviceNames) {
            if (-not $states.ContainsKey($serviceName)) {
                $allHealthy = $false
                continue
            }

            $service = $states[$serviceName]
            if ($service.State -match "(?i)exited|dead|failed") {
                throw "服务 $serviceName 已异常退出，当前状态：$($service.State)。"
            }
            if ($service.State -notmatch "(?i)running|up" -or $service.Health -notmatch "(?i)^healthy$") {
                $allHealthy = $false
            }
        }

        if ($allHealthy) {
            Write-Host "PostgreSQL、Redis 和 Backend 容器健康检查已通过。"
            return
        }

        Start-Sleep -Seconds ([Math]::Max(1, $PollIntervalSeconds))
    }

    throw "等待三个容器健康检查超时（${TimeoutSeconds} 秒）。"
}

function Get-BackendHostPort {
    $result = Invoke-DockerCommand -Arguments @("compose", "port", "backend", "8000")
    if ($result.ExitCode -ne 0) {
        throw "读取 Backend 映射端口失败：$(ConvertTo-SafeText $result.Output)"
    }

    $match = [regex]::Match($result.Output.Trim(), ":(\d+)\s*$")
    if (-not $match.Success) {
        throw "无法从 Compose 输出解析 Backend 映射端口。"
    }
    return [int]$match.Groups[1].Value
}

function Wait-ForBackendHealth {
    param(
        [Parameter(Mandatory = $true)]
        [datetime]$Deadline
    )

    $backendPort = Get-BackendHostPort
    $healthUrl = "http://127.0.0.1:$backendPort/health"
    $lastError = ""

    while ([DateTime]::UtcNow -lt $Deadline) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                Write-Host "Backend 健康端点检查已通过：$healthUrl"
                return
            }
            $lastError = "HTTP 状态码 $($response.StatusCode)"
        }
        catch {
            $lastError = $_.Exception.Message
        }

        Start-Sleep -Seconds ([Math]::Max(1, $PollIntervalSeconds))
    }

    throw "Backend 健康端点检查超时：$healthUrl；最近错误：$(ConvertTo-SafeText $lastError)"
}

function Invoke-ComposeCheck {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host "正在执行$Description..."
    $result = Invoke-DockerCommand -Arguments $Arguments
    if ($result.ExitCode -ne 0) {
        throw "$Description失败：$(ConvertTo-SafeText $result.Output)"
    }
    Write-CommandOutput -Output $result.Output
    Write-Host "$Description已通过。"
    return $result.Output
}

function Write-Diagnostics {
    Write-Host "开始输出 M0 冒烟诊断信息："

    try {
        $status = Invoke-DockerCommand -Arguments @("compose", "ps", "--all")
        Write-Host "Compose 服务状态："
        Write-CommandOutput -Output $status.Output
    }
    catch {
        Write-Host "读取 Compose 服务状态失败：$(ConvertTo-SafeText $_.Exception.Message)"
    }

    try {
        $logs = Invoke-DockerCommand -Arguments @(
            "compose",
            "logs",
            "--no-color",
            "--tail",
            "40",
            "postgres",
            "redis",
            "backend"
        )
        Write-Host "最近服务日志（已截取并脱敏）："
        Write-CommandOutput -Output $logs.Output
    }
    catch {
        Write-Host "读取服务日志失败：$(ConvertTo-SafeText $_.Exception.Message)"
    }
}

try {
    if ($TimeoutSeconds -lt 1) {
        throw "TimeoutSeconds 必须大于或等于 1。"
    }
    if ($PollIntervalSeconds -lt 1) {
        throw "PollIntervalSeconds 必须大于或等于 1。"
    }

    Write-Host "开始执行 M0 冒烟验证。"
    Invoke-ComposeCheck -Description "Compose 配置检查" -Arguments @("compose", "config", "--quiet") | Out-Null
    Invoke-ComposeCheck -Description "三容器启动" -Arguments @("compose", "up", "-d") | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    Wait-ForComposeServices -Deadline $deadline
    Wait-ForBackendHealth -Deadline $deadline

    Invoke-ComposeCheck `
        -Description "Alembic 数据库迁移" `
        -Arguments @("compose", "exec", "-T", "backend", "alembic", "upgrade", "head") | Out-Null

    $currentOutput = Invoke-ComposeCheck `
        -Description "Alembic 当前版本检查" `
        -Arguments @("compose", "exec", "-T", "backend", "alembic", "current")
    if ($currentOutput -notmatch "(?i)head") {
        throw "Alembic 当前版本未到达 head。"
    }

    $redisOutput = Invoke-ComposeCheck `
        -Description "Redis PING 检查" `
        -Arguments @("compose", "exec", "-T", "redis", "redis-cli", "ping")
    if ($redisOutput -notmatch "(?im)^\s*PONG\s*$") {
        throw "Redis PING 未返回 PONG。"
    }

    Write-Host "M0 冒烟验证通过：三个容器、Backend 健康端点、Alembic 迁移和 Redis PING 均正常。"
    exit 0
}
catch {
    Write-Host "M0 冒烟验证失败：$(ConvertTo-SafeText $_.Exception.Message)" -ForegroundColor Red
    Write-Diagnostics
    exit 1
}
