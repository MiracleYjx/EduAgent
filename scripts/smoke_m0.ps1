<#
.SYNOPSIS
执行 M0 三容器、数据库迁移和 Redis 连接冒烟验证。

.DESCRIPTION
启动 PostgreSQL、Redis 并等待健康检查，先执行 Alembic 迁移，再启动
Backend 并检查全部容器、就绪端点和 Redis PING。脚本失败时会输出
脱敏诊断并以非零退出码结束。
#>

[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180,
    [int]$PollIntervalSeconds = 5
)

$ErrorActionPreference = "Stop"
$serviceNames = @("postgres", "redis", "backend")
$isolationVerified = $false
$containerProfileSource = $null
$isolationEnvironment = @{
    "COMPOSE_PROJECT_NAME" = "eduagent-test"
    "POSTGRES_PORT"        = "15432"
    "REDIS_PORT"           = "16379"
    "BACKEND_PORT"         = "18000"
    "EDUAGENT_ENVIRONMENT" = "test"
    "POSTGRES_DB"          = "eduagent_test"
    "POSTGRES_USER"        = "eduagent_test_runner"
}

function Assert-IsolatedEnvironment {
    $script:containerProfileSource = $null
    foreach ($name in $isolationEnvironment.Keys) {
        $actual = [Environment]::GetEnvironmentVariable($name)
        if ([string]::IsNullOrWhiteSpace($actual)) {
            throw "M0 冒烟必须设置隔离环境变量 $name；拒绝使用默认 Compose 项目或端口。"
        }
        if ($actual -ne $isolationEnvironment[$name]) {
            throw "M0 冒烟环境变量 $name 必须固定为 $($isolationEnvironment[$name])，当前值不安全。"
        }
    }
    $profilePath = [Environment]::GetEnvironmentVariable("EDUAGENT_TEST_RESOURCE_PROFILE")
    if ([string]::IsNullOrWhiteSpace($profilePath) -or
        -not [System.IO.Path]::IsPathRooted($profilePath) -or
        -not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        throw "M0 冒烟缺少可读取的绝对 Test 资源 profile；拒绝构建或启动 Compose。"
    }
    try {
        $profile = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "M0 冒烟 Test 资源 profile 无法解析。"
    }
    if ($profile.environment -ne "test" -or
        $profile.database_identity.database -ne "eduagent_test" -or
        $profile.database_identity.user -ne "eduagent_test_runner" -or
        [string]::IsNullOrWhiteSpace($profile.database_identity.instance_id)) {
        throw "M0 冒烟 Test 资源 profile 身份不符。"
    }
    $containerProfilePath = [Environment]::GetEnvironmentVariable("EDUAGENT_TEST_CONTAINER_PROFILE")
    if ([string]::IsNullOrWhiteSpace($profile.root) -or
        -not [System.IO.Path]::IsPathRooted($profile.root) -or
        [System.IO.Path]::GetFullPath($profile.root) -ne $profile.root) {
        throw "M0 冒烟主机 Test profile 的资源根必须为规范绝对路径。"
    }
    $expectedContainerProfile = Join-Path (Join-Path $profile.root "config") "container-resource-profile.json"
    if ([string]::IsNullOrWhiteSpace($containerProfilePath) -or
        -not [System.IO.Path]::IsPathRooted($containerProfilePath) -or
        [System.IO.Path]::GetFullPath($containerProfilePath) -ne $containerProfilePath -or
        $containerProfilePath -ne $expectedContainerProfile) {
        throw "M0 冒烟容器 profile 来源必须为本次主机 Test 资源根内的 config/container-resource-profile.json。"
    }
    if (-not (Test-Path -LiteralPath $containerProfilePath -PathType Leaf)) {
        throw "M0 冒烟容器 Test profile 必须已存在且可读取；拒绝自动创建来源路径。"
    }
    $ancestor = $containerProfilePath
    while (-not [string]::IsNullOrWhiteSpace($ancestor)) {
        $item = Get-Item -LiteralPath $ancestor -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "M0 冒烟容器 Test profile 来源不得经过重解析路径。"
        }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) {
            break
        }
        $ancestor = $parent
    }
    try {
        $containerProfile = Get-Content -LiteralPath $containerProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "M0 冒烟容器 Test profile 无法读取或解析。"
    }
    $allowedProfileFields = @("environment", "environment_id", "database_identity", "maintenance_identity", "redis_instance_id", "root")
    $hostEnvironmentId = [Guid]::Empty
    $containerEnvironmentId = [Guid]::Empty
    if ($containerProfile.environment -cne "test" -or
        $containerProfile.environment -cne $profile.environment -or
        $containerProfile.root -cne "/app" -or
        $containerProfile.database_identity.database -cne "eduagent_test" -or
        $containerProfile.database_identity.user -cne "eduagent_test_runner" -or
        @($containerProfile.PSObject.Properties.Name | Where-Object { $_ -cnotin $allowedProfileFields }).Count -ne 0 -or
        -not [Guid]::TryParse([string]$profile.environment_id, [ref]$hostEnvironmentId) -or
        $hostEnvironmentId -eq [Guid]::Empty -or
        -not [Guid]::TryParse([string]$containerProfile.environment_id, [ref]$containerEnvironmentId) -or
        $containerEnvironmentId -ne $hostEnvironmentId -or
        $containerProfile.redis_instance_id -cne $profile.redis_instance_id) {
        throw "M0 冒烟容器 Test profile 的环境、资源根或资源身份与主机不一致。"
    }
    foreach ($identityName in @("database_identity", "maintenance_identity")) {
        $hostIdentity = $profile.$identityName
        $containerIdentity = $containerProfile.$identityName
        if ($identityName -eq "maintenance_identity" -and $null -eq $hostIdentity -and $null -eq $containerIdentity) {
            continue
        }
        if ($null -eq $hostIdentity -or $null -eq $containerIdentity -or
            (@($hostIdentity.PSObject.Properties.Name | Sort-Object) -join ",") -cne "database,host,instance_id,port,schema_name,user" -or
            (@($containerIdentity.PSObject.Properties.Name | Sort-Object) -join ",") -cne "database,host,instance_id,port,schema_name,user" -or
            $containerIdentity.host -cne "postgres" -or $containerIdentity.port -ne 5432 -or
            $hostIdentity.schema_name -cne "public") {
            throw "M0 冒烟容器 Test profile 的数据库身份字段或容器地址不符合约定。"
        }
        foreach ($fact in @("database", "user", "schema_name", "instance_id")) {
            if ([string]::IsNullOrWhiteSpace([string]$hostIdentity.$fact) -or
                $containerIdentity.$fact -cne $hostIdentity.$fact) {
                throw "M0 冒烟容器 Test profile 不得改写主机数据库资源事实。"
            }
        }
    }
    if ($null -ne $profile.maintenance_identity -and
        ($profile.maintenance_identity.host -cne $profile.database_identity.host -or
         $profile.maintenance_identity.port -ne $profile.database_identity.port -or
         $profile.maintenance_identity.database -cne "postgres" -or
         $profile.maintenance_identity.user -ceq $profile.database_identity.user -or
         $profile.maintenance_identity.instance_id -cne $profile.database_identity.instance_id)) {
        throw "M0 冒烟 Test maintenance 身份不属于同一数据库实例。"
    }
    $envFile = [Environment]::GetEnvironmentVariable("EDUAGENT_ENV_FILE")
    if ([string]::IsNullOrWhiteSpace($envFile) -or $envFile -eq ".env" -or
        -not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw "M0 冒烟必须显式选择独立 Test 环境文件。"
    }
    $authMethod = [Environment]::GetEnvironmentVariable("POSTGRES_HOST_AUTH_METHOD")
    if ([string]::IsNullOrWhiteSpace($authMethod) -or $authMethod -eq "trust") {
        throw "M0 冒烟拒绝无认证的 PostgreSQL 初始化。"
    }
    $script:containerProfileSource = $containerProfilePath
}

function Assert-IsolatedComposeConfiguration {
    $composeFile = [Environment]::GetEnvironmentVariable("COMPOSE_FILE")
    $expectedFile = [System.IO.Path]::GetFullPath(
        (Join-Path (Split-Path -Parent $PSScriptRoot) "docker-compose.test.yml")
    )
    if ([string]::IsNullOrWhiteSpace($composeFile) -or
        -not [System.IO.Path]::IsPathRooted($composeFile) -or
        -not (Test-Path -LiteralPath $composeFile -PathType Leaf) -or
        [System.IO.Path]::GetFullPath($composeFile) -ne $expectedFile -or
        [Environment]::GetEnvironmentVariable("COMPOSE_DISABLE_ENV_FILE") -ne "1") {
        throw "M0 冒烟必须显式使用仓库 Test Compose 文件并禁用隐式 .env。"
    }

    $result = Invoke-DockerCommand -Arguments @("compose", "config", "--format", "json")
    if ($result.ExitCode -ne 0) {
        throw "M0 冒烟无法解析 Test Compose 配置。"
    }
    try {
        $config = $result.Output | ConvertFrom-Json
    }
    catch {
        throw "M0 冒烟 Test Compose 配置不是有效 JSON。"
    }
    if ($config.name -ne "eduagent-test" -or
        (@($config.services.PSObject.Properties.Name | Sort-Object) -join ",") -ne "backend,postgres,redis" -or
        $config.services.postgres.environment.POSTGRES_USER -ne "eduagent_test_admin" -or
        $config.services.postgres.environment.POSTGRES_DB -ne "postgres") {
        throw "M0 冒烟项目、服务或数据库初始化身份不符合 Test 配置。"
    }
    $backendEnvironment = $config.services.backend.environment
    if ($backendEnvironment.EDUAGENT_ENVIRONMENT -cne "test" -or
        $backendEnvironment.EDUAGENT_RUNTIME_KIND -cne "docker" -or
        $backendEnvironment.EDUAGENT_DATA_ROOT -cne "/app" -or
        $backendEnvironment.EDUAGENT_TEST_RESOURCE_PROFILE -cne "/run/eduagent/test-resource-profile.json" -or
        $backendEnvironment.STORAGE_ROOT -cne "/app/storage") {
        throw "M0 冒烟 Backend 必须显式使用容器 Test profile 与原 canonical storage。"
    }
    $databaseUri = $null
    if (-not [Uri]::TryCreate([string]$backendEnvironment.DATABASE_URL, [UriKind]::Absolute, [ref]$databaseUri) -or
        $databaseUri.Scheme -cne "postgresql+psycopg" -or
        $databaseUri.Host -cne "postgres" -or $databaseUri.Port -ne 5432 -or
        $databaseUri.AbsolutePath -cne "/eduagent_test" -or
        [Uri]::UnescapeDataString(($databaseUri.UserInfo -split ":", 2)[0]) -cne "eduagent_test_runner" -or
        -not [string]::IsNullOrEmpty($databaseUri.Query) -or
        -not [string]::IsNullOrEmpty($databaseUri.Fragment) -or
        $backendEnvironment.REDIS_URL -cne "redis://redis:6379/0") {
        throw "M0 冒烟 Backend 数据库或 Redis 路由不符合容器 Test 身份。"
    }
    $expectedPorts = @{
        postgres = "127.0.0.1:15432:5432"
        redis = "127.0.0.1:16379:6379"
        backend = "127.0.0.1:18000:8000"
    }
    foreach ($service in $expectedPorts.Keys) {
        $ports = @($config.services.$service.ports)
        if ($ports.Count -ne 1 -or
            "$($ports[0].host_ip):$($ports[0].published):$($ports[0].target)" -ne $expectedPorts[$service]) {
            throw "M0 冒烟 $service 发布端口不符合 Test 约定。"
        }
    }
    if ((@($config.volumes.PSObject.Properties.Name | Sort-Object) -join ",") -cne "postgres_data,redis_data,storage_data") {
        throw "M0 冒烟仅允许原三项 Test 卷，不允许未知卷。"
    }
    foreach ($volume in @("postgres_data", "redis_data", "storage_data")) {
        if ($config.volumes.$volume.name -ne "eduagent-test_$volume" -or
            $config.volumes.$volume.external -eq $true) {
            throw "M0 冒烟 $volume 不属于 Test 项目。"
        }
    }
    $dependencyMounts = @{
        postgres = @("postgres_data", "/var/lib/postgresql/data")
        redis = @("redis_data", "/data")
    }
    foreach ($service in $dependencyMounts.Keys) {
        $mounts = @($config.services.$service.volumes)
        if ($mounts.Count -ne 1 -or $mounts[0].type -cne "volume" -or
            $mounts[0].source -cne $dependencyMounts[$service][0] -or
            $mounts[0].target -cne $dependencyMounts[$service][1] -or
            $mounts[0].read_only -eq $true) {
            throw "M0 冒烟 $service 仅允许原可写 Test 数据卷，不允许未知来源或挂载目标。"
        }
    }
    $backendMounts = @($config.services.backend.volumes)
    $storageMounts = @($backendMounts | Where-Object {
        $_.type -ceq "volume" -and $_.source -ceq "storage_data" -and $_.target -ceq "/app/storage"
    })
    $profileMounts = @($backendMounts | Where-Object {
        $_.type -ceq "bind" -and $_.target -ceq "/run/eduagent/test-resource-profile.json"
    })
    if ($backendMounts.Count -ne 2 -or $storageMounts.Count -ne 1 -or $profileMounts.Count -ne 1) {
        throw "M0 冒烟 Backend 仅允许原 storage 卷和单一容器 Test profile bind。"
    }
    $profileMount = $profileMounts[0]
    if ([string]::IsNullOrWhiteSpace($script:containerProfileSource) -or
        [string]::IsNullOrWhiteSpace($profileMount.source) -or
        -not [System.IO.Path]::IsPathRooted($profileMount.source) -or
        [System.IO.Path]::GetFullPath($profileMount.source) -ne $script:containerProfileSource -or
        $profileMount.read_only -isnot [bool] -or -not $profileMount.read_only -or
        $profileMount.bind.create_host_path -isnot [bool] -or $profileMount.bind.create_host_path -or
        $storageMounts[0].read_only -eq $true) {
        throw "M0 冒烟 Backend 挂载必须保留原可写 storage，并只读绑定已核对的 profile，且不得自动创建主机路径。"
    }
}

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
        [datetime]$Deadline,
        [string[]]$RequiredServices = @("postgres", "redis", "backend")
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
        foreach ($serviceName in $RequiredServices) {
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
            Write-Host "容器健康检查已通过：$($RequiredServices -join ", ")。"
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

function Wait-ForBackendReady {
    param(
        [Parameter(Mandatory = $true)]
        [datetime]$Deadline
    )

    $backendPort = Get-BackendHostPort
    $readyUrl = "http://127.0.0.1:$backendPort/ready"
    $lastError = ""

    while ([DateTime]::UtcNow -lt $Deadline) {
        try {
            $response = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 4
            if ($response.StatusCode -eq 200) {
                Write-Host "Backend 就绪端点检查已通过：$readyUrl"
                return
            }
            $lastError = "HTTP 状态码 $($response.StatusCode)"
        }
        catch {
            $lastError = $_.Exception.Message
        }

        Start-Sleep -Seconds ([Math]::Max(1, $PollIntervalSeconds))
    }

    throw "Backend 就绪端点检查超时：$readyUrl；最近错误：$(ConvertTo-SafeText $lastError)"
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

    Assert-IsolatedEnvironment
    Assert-IsolatedComposeConfiguration
    $isolationVerified = $true
    Write-Host "开始执行 M0 冒烟验证。"
    Invoke-ComposeCheck -Description "Compose 配置检查" -Arguments @("compose", "config", "--quiet") | Out-Null
    Invoke-ComposeCheck -Description "Backend 镜像构建" -Arguments @("compose", "build", "backend") | Out-Null
    Invoke-ComposeCheck -Description "数据库和缓存启动" -Arguments @("compose", "up", "-d", "postgres", "redis") | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    Wait-ForComposeServices -Deadline $deadline -RequiredServices @("postgres", "redis")
    Invoke-ComposeCheck `
        -Description "Alembic 数据库迁移" `
        -Arguments @("compose", "run", "--rm", "--no-deps", "backend", "alembic", "upgrade", "head") | Out-Null

    Invoke-ComposeCheck -Description "Backend 启动" -Arguments @("compose", "up", "-d", "backend") | Out-Null
    Wait-ForComposeServices -Deadline $deadline
    Wait-ForBackendReady -Deadline $deadline

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

    Write-Host "M0 冒烟验证通过：三个容器、Backend 就绪端点、Alembic 迁移和 Redis PING 均正常。"
    exit 0
}
catch {
    Write-Host "M0 冒烟验证失败：$(ConvertTo-SafeText $_.Exception.Message)" -ForegroundColor Red
    if ($isolationVerified) {
        Write-Diagnostics
    }
    exit 1
}
