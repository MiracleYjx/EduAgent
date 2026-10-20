"""T087/T202：真实 PowerShell，只 Fake Docker 与主机端口观察边界。

所有合成配置、完整 stdin snapshot、内部 receipt 和 Fake 元数据位于 tmp_path。
Fake 只模拟命令造成的资源变化，不执行或复刻 run_demo.ps1 的护栏验证算法。
迁移前移的两个严格节点已在旧脚本上先行失败；历史结果见既有 TCR。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_demo.ps1"
COMPOSE = SCRIPT.parents[1] / "docker-compose.yml"
HEAD = "0023_grading_exam_question"
SERVICES = {"backend": (8000, 18000, "storage_data", "/app/storage"),
            "postgres": (5432, 15432, "postgres_data", "/var/lib/postgresql/data"),
            "redis": (6379, 16379, "redis_data", "/data")}
UP = ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "--no-recreate",
      "--wait", "--wait-timeout", "30"]

# Synthetic state changes only at actual Docker create/up/start/rm boundaries.
# In particular, create must never advance the backend's startup/recovery events.
DRIVER = r"""
$ErrorActionPreference = 'Stop'
$script:options = Get-Content -LiteralPath $env:T087_OPTIONS -Raw | ConvertFrom-Json -AsHashtable
$script:configPath = $env:T087_CONFIG
$script:callsPath = $env:T087_CALLS
$script:checksPath = $env:T087_CHECKS
$script:eventsPath = $env:T087_EVENTS
$script:snapshotsPath = $env:T087_SNAPSHOTS
$script:statePath = $env:T087_STATE
$script:scriptPath = $env:T087_SCRIPT
$script:restorePath = $env:T087_RESTORE
$script:state = @{ volumes = @(); networks = @(); containers = @(); environment = @{}; heads = @(); serial = 0 }
if ($script:options.reuse) { $script:state = Get-Content -LiteralPath $script:statePath -Raw | ConvertFrom-Json -AsHashtable }
$script:volumeLists = 0
$script:configCalls = 0
$script:callIndex = -1
$script:head = '0023_grading_exam_question'
$script:images = @{
    backend = @{ Id = "sha256:$('e' * 64)"; Config = @{ Env = @('PATH=/usr/local/bin:/usr/bin:/bin', 'PYTHONUNBUFFERED=1');
        Entrypoint = @(); Cmd = @('sh', '-c', 'python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000'); WorkingDir = '/app'; User = '' } };
    postgres = @{ Id = "sha256:$('d' * 64)"; Config = @{ Env = @('PATH=/usr/bin:/bin');
        Entrypoint = @('docker-entrypoint.sh'); Cmd = @('postgres'); WorkingDir = ''; User = '' } };
    redis = @{ Id = "sha256:$('c' * 64)"; Config = @{ Env = @('PATH=/usr/bin:/bin');
        Entrypoint = @('docker-entrypoint.sh'); Cmd = @('redis-server'); WorkingDir = '/data'; User = '' } }
}

function Write-FakeEvent([string]$Event, $Details = @{}) {
    $entry = @{ event = $Event; call_index = $script:callIndex }
    foreach ($key in $Details.Keys) { $entry[$key] = $Details[$key] }
    Add-Content -LiteralPath $script:eventsPath -Value ($entry | ConvertTo-Json -Depth 30 -Compress)
}
function Save-FakeState { $script:state | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $script:statePath -Encoding utf8 }

function Read-FakeConfig($Arguments, [string]$InputText = '') {
    $files = @()
    for ($i = 0; $i -lt $Arguments.Count; $i++) { if ($Arguments[$i] -eq '--file') { $files += $Arguments[$i + 1] } }
    if ('-' -in $files) {
        if (-not $InputText.Trim()) { throw 'sensitive-test stdin model missing' }
        return ($InputText | ConvertFrom-Json -AsHashtable)
    }
    $config = Get-Content -LiteralPath $script:configPath -Raw | ConvertFrom-Json -AsHashtable
    foreach ($file in @($files | Select-Object -Skip 1)) {
        $override = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json -AsHashtable
        foreach ($service in $override.services.Keys) {
            $config.services[$service].labels = $override.services[$service].labels
            if ($override.services[$service].environment) {
                foreach ($key in $override.services[$service].environment.Keys) {
                    $config.services[$service].environment[$key] = $override.services[$service].environment[$key]
                }
            }
        }
        foreach ($name in $override.volumes.Keys) { $config.volumes[$name].labels = $override.volumes[$name].labels }
        $config.networks.default.labels = $override.networks.default.labels
    }
    return $config
}

function Get-FakeImage([string]$Reference) {
    foreach ($service in @('postgres', 'redis', 'backend')) {
        if ($Reference -eq $script:images[$service].Id -or
            ($service -eq 'postgres' -and $Reference -like '*pgvector*') -or
            ($service -eq 'redis' -and $Reference -like 'redis:*')) { return $script:images[$service] }
    }
    return $script:images.backend
}

function Add-FakeVolume($Config, [string]$Logical) {
    $definition = $Config.volumes[$Logical]
    $name = [string]$definition.name
    $existing = @($script:state.volumes | Where-Object { $_.Name -eq $name })
    if ($existing.Count) { return $existing[0] }
    $labels = @{ 'com.docker.compose.project' = 'eduagent-demo'; 'com.docker.compose.volume' = $Logical }
    if ($definition.labels) { foreach ($key in $definition.labels.Keys) { $labels[$key] = $definition.labels[$key] } }
    $volume = @{ Name = $name; CreatedAt = '2026-10-15T03:00:00Z'; Driver = 'local'; Scope = 'local';
        Mountpoint = "/var/lib/docker/volumes/$name/_data"; Options = @{}; Labels = $labels }
    $script:state.volumes += $volume
    Write-FakeEvent 'volume-created' @{ name = $name }
    return $volume
}

function Add-FakeNetwork($Config) {
    if ($script:state.networks.Count) { return $script:state.networks[0] }
    $labels = @{ 'com.docker.compose.project' = 'eduagent-demo'; 'com.docker.compose.network' = 'default' }
    if ($Config.networks.default.labels) { foreach ($key in $Config.networks.default.labels.Keys) { $labels[$key] = $Config.networks.default.labels[$key] } }
    $network = @{ Name = 'eduagent-demo_default'; Id = 'a' * 64; Created = '2026-10-15T03:00:01Z';
        Driver = 'bridge'; Scope = 'local'; Internal = $false; Labels = $labels }
    $script:state.networks += $network
    Write-FakeEvent 'network-created' @{ id = $network.Id }
    return $network
}

function Add-FakeContainer($Config, [string]$Service) {
    $definition = $Config.services[$Service]
    $name = if ($definition.container_name) { [string]$definition.container_name } else { "eduagent-demo-$Service-1" }
    $existing = @($script:state.containers | Where-Object { $_.Name -eq "/$name" })
    if ($existing.Count) { return $existing[0] }
    $network = Add-FakeNetwork $Config
    $labels = @{ 'com.docker.compose.project' = 'eduagent-demo'; 'com.docker.compose.service' = $Service;
        'com.docker.compose.container-number' = '1'; 'com.docker.compose.oneoff' = 'False'; 'com.docker.compose.config-hash' = 'fake-config-hash' }
    if ($definition.labels) { foreach ($key in $definition.labels.Keys) { $labels[$key] = $definition.labels[$key] } }
    $image = Get-FakeImage ([string]$definition.image)
    $environment = @{}
    foreach ($entry in $image.Config.Env) { $key, $value = $entry -split '=', 2; $environment[$key] = $value }
    if ($definition.environment) {
        foreach ($key in $definition.environment.Keys) { $environment[$key] = ([string]$definition.environment[$key]).Replace('$$', '$') }
    }
    $mounts = @()
    foreach ($mount in $definition.volumes) {
        $volume = Add-FakeVolume $Config ([string]$mount.source)
        $mounts += @{ Name = $volume.Name; Type = 'volume'; Driver = 'local'; Source = $volume.Mountpoint;
            Destination = $mount.target; RW = -not [bool]$mount.read_only }
    }
    $bindings = @{}
    foreach ($port in $definition.ports) { $bindings["$($port.target)/tcp"] = @(@{ HostIp = $port.host_ip; HostPort = [string]$port.published }) }
    $entrypoint = @()
    if ($definition.Contains('entrypoint')) { $entrypoint = @($definition.entrypoint) }
    else { $entrypoint = @($image.Config.Entrypoint) }
    $command = @()
    if ($definition.Contains('command')) { $command = @($definition.command) }
    else { $command = @($image.Config.Cmd) }
    $process = @($entrypoint) + @($command)
    $phase = [string]$labels['io.eduagent.phase']
    $healthcheck = $null
    if ($definition.healthcheck) {
        $healthcheck = @{ Test = @($definition.healthcheck.test | ForEach-Object { ([string]$_).Replace('$$', '$') }) }
    }
    $script:state.serial++
    $id = if ($Service -eq 'backend') { '{0:x64}' -f (1000 + $script:state.serial) } elseif ($Service -eq 'postgres') { '2' * 64 } elseif ($Service -eq 'redis') { '3' * 64 } else { '{0:x64}' -f (100 + $script:state.serial) }
    $container = @{ Id = $id; Name = "/$name"; Created = '2026-10-15T03:00:02Z'; Image = $image.Id;
        Path = [string]$process[0]; Args = @($process | Select-Object -Skip 1); State = @{ Running = $false; Status = 'created'; ExitCode = 0; Health = @{ Status = 'healthy' } };
        Config = @{ Image = [string]$definition.image; Labels = $labels; Env = @($environment.Keys | Sort-Object | ForEach-Object { "$_=$($environment[$_])" });
            Entrypoint = @($entrypoint); Cmd = @($command); WorkingDir = $image.Config.WorkingDir; User = $image.Config.User; Healthcheck = $healthcheck };
        Mounts = $mounts; HostConfig = @{ PortBindings = $bindings; ReadonlyRootfs = [bool]$definition.read_only;
            RestartPolicy = @{ Name = $(if ($definition.restart) { $definition.restart } else { 'no' }) }; Privileged = $false; NetworkMode = 'eduagent-demo_default' };
        NetworkSettings = @{ Ports = $bindings; Networks = @{ 'eduagent-demo_default' = @{ NetworkID = $network.Id; EndpointID = ('{0:x64}' -f (200 + $script:state.serial)) } } } }
    if ($Service -in @('backend', 'demo_step')) { $script:state.environment = $environment }
    if ($script:options.scenario -eq 'post-owner') { $script:state.volumes[0].Labels['io.eduagent.owner'] = 'wrong' }
    $prefix = if ($Service -eq 'demo_step') { $phase } else { $Service }
    switch ($script:options.scenario) {
        "$prefix-image-mismatch" { $container.Image = "sha256:$('f' * 64)" }
        "$prefix-env-mismatch" { $container.Config.Env += 'UNEXPECTED_PROVIDER_KEY=sensitive-test-injected' }
        "$prefix-command-mismatch" { $container.Config.Cmd = @('python', '-m', 'backend.main'); $container.Path = 'python'; $container.Args = @('-m', 'backend.main') }
        "$prefix-mount-mismatch" { $container.Mounts[0].Destination = '/app/development' }
        "$prefix-network-mismatch" { $container.NetworkSettings.Networks['eduagent-demo_default'].NetworkID = 'f' * 64 }
        "$prefix-healthcheck-mismatch" { $container.Config.Healthcheck = @{ Test = @('CMD', 'true') } }
        "$prefix-restart-mismatch" { $container.HostConfig.RestartPolicy.Name = 'always' }
        "$prefix-readonly-mismatch" { $container.HostConfig.ReadonlyRootfs = $false; $container.Mounts[0].RW = $true }
        "$prefix-unknown-id" { $container.Id = 'f' * 64; $container.Config.Labels['io.eduagent.owner'] = 'another-launcher' }
    }
    $script:state.containers += $container
    Write-FakeEvent 'container-created' @{ service = $Service; phase = $phase; id = $container.Id; image = $container.Image; running = $false }
    Save-FakeState
    return $container
}

function Get-FakeIdentity($Environment, [bool]$Migrated = $false) {
    $root = if ($script:options.scenario -eq 'post-root') { '/app/development' } else { '/app/storage' }
    $identifier = if ($script:options.scenario -eq 'post-environment-id') { 'wrong-id' } else { $Environment.EDUAGENT_ENVIRONMENT_ID }
    $paths = @{}
    foreach ($name in @('config', 'storage', 'cache', 'logs', 'temp', 'results', 'credentials')) { $paths[$name] = "/app/storage/$name" }
    if ($script:options.scenario -eq 'post-cache-root') { $paths.cache = '/development/cache' }
    $database = if ($script:options.scenario -eq 'post-database') { 'development' } else { 'eduagent_demo' }
    $user = if ($script:options.scenario -eq 'post-user') { 'development' } else { 'eduagent_demo' }
    $schema = if ($script:options.scenario -eq 'post-schema') { 'development' } else { 'public' }
    $system = if ($script:options.scenario -eq 'changed-pg-instance') { '9999' } else { '7694989589121413158' }
    $runId = if ($script:options.scenario -eq 'post-redis') { '' } elseif ($script:options.scenario -eq 'changed-redis-instance') { 'c' * 40 } else { 'b' * 40 }
    $actual = @{ database = $database; user = $user; schema = $schema; system_identifier = $system }
    if ($script:options.scenario -eq 'probe-pg-disagrees') { $actual.system_identifier = '1111' }
    if ($script:options.scenario -eq 'probe-redis-disagrees') { $runId = 'd' * 40 }
    $current = @($script:state.heads)
    if ($Migrated -and $script:options.scenario -eq 'migration-head-mismatch') { $current = @('old_revision') }
    return @{ environment = $Environment.EDUAGENT_ENVIRONMENT; runtime_kind = $Environment.EDUAGENT_RUNTIME_KIND;
        environment_id = $identifier; creation_id = $Environment.EDUAGENT_CREATION_ID; data_root = $Environment.EDUAGENT_DATA_ROOT;
        storage_root = $Environment.STORAGE_ROOT; real_root = $root; real_storage = '/app/storage/storage';
        device = $(if ($script:options.scenario -eq 'changed-storage-device') { '43' } else { '42' });
        inode = $(if ($script:options.scenario -eq 'changed-storage-inode') { '102' } else { '101' }); paths = $paths;
        database = @{ host = 'postgres'; port = 5432; database = '/eduagent_demo'; user = 'eduagent_demo'; actual = $actual };
        redis = @{ host = 'redis'; port = 6379; database = '/0'; run_id = $runId };
        current_heads = $current; target_heads = @($script:head); storage_compatible = ($script:options.scenario -ne 'storage-incompatible'); migration_completed = $Migrated }
}

function Get-FakeServiceNames($Arguments, [string]$Action, $Config) {
    $index = [Array]::IndexOf(@($Arguments), $Action)
    $selected = @($Arguments | Select-Object -Skip ($index + 1) | Where-Object { $_ -in $Config.services.Keys })
    if ($selected.Count) { return $selected }
    return @($Config.services.Keys)
}
function Start-FakeContainer($Container) {
    $service = [string]$Container.Config.Labels['com.docker.compose.service']
    $phase = [string]$Container.Config.Labels['io.eduagent.phase']
    $Container.State.Running = $true; $Container.State.Status = 'running'
    if (($service -eq 'backend' -and $script:options.scenario -in @('backend-unhealthy', 'setup-only')) -or
        ($service -eq 'postgres' -and $script:options.scenario -eq 'foundation-unhealthy')) { $Container.State.Health.Status = 'unhealthy' }
    if (($service -eq 'backend' -and $script:options.scenario -eq 'backend-no-health') -or
        ($service -eq 'postgres' -and $script:options.scenario -eq 'foundation-no-health')) { $null = $Container.State.Remove('Health') }
    Write-FakeEvent 'container-start' @{ service = $service; phase = $phase; id = $Container.Id }
    if ($service -eq 'backend') {
        Write-FakeEvent 'backend-start' @{ id = $Container.Id }
        Write-FakeEvent 'backend-recovery' @{ id = $Container.Id; heads = @($script:state.heads) }
    }
}

if ($script:options.scenario -in @('unknown-volume', 'unknown-network', 'unknown-container', 'docker-port-conflict')) {
    $fakeConfig = Read-FakeConfig @()
    $external = Add-FakeContainer $fakeConfig 'backend'
    if ($script:options.scenario -eq 'unknown-volume') { $script:state.networks = @(); $script:state.containers = @() }
    if ($script:options.scenario -eq 'unknown-network') { $script:state.volumes = @(); $script:state.containers = @() }
    if ($script:options.scenario -eq 'unknown-container') { $script:state.volumes = @(); $script:state.networks = @() }
    if ($script:options.scenario -eq 'docker-port-conflict') {
        $external.Id = 'f' * 64; $external.Name = '/unrelated-container'; $external.Config.Labels = @{ 'com.docker.compose.project' = 'development' }
        $external.Mounts = @(); $external.NetworkSettings.Networks = @{}; $external.State.Running = $true
        $script:state.volumes = @(); $script:state.networks = @()
    }
}
$existingBackend = @($script:state.containers | Where-Object { $_.Config.Labels['com.docker.compose.service'] -eq 'backend' })
if ($script:options.scenario -eq 'wrong-owner') { $script:state.volumes[0].Labels['io.eduagent.owner'] = 'another-launcher' }
if ($script:options.scenario -eq 'wrong-container-id') { $existingBackend[0].Id = 'f' * 64 }
if ($script:options.scenario -eq 'wrong-network-id') { $script:state.networks[0].Id = 'f' * 64 }
if ($script:options.scenario -eq 'wrong-created-at') { $script:state.volumes[0].CreatedAt = '2026-10-15T03:01:00Z' }
if ($script:options.scenario -eq 'wrong-container-created-at') { $existingBackend[0].Created = '2026-10-15T03:01:00Z' }
if ($script:options.scenario -eq 'wrong-network-created-at') { $script:state.networks[0].Created = '2026-10-15T03:01:00Z' }
if ($script:options.scenario -eq 'wrong-mount') { $existingBackend[0].Mounts[0].Source = '/another/volume' }
if ($script:options.scenario -eq 'running-needs-upgrade') { $script:state.heads = @() }
if ($script:options.scenario -eq 'running-image-changed') { $script:images.backend.Id = "sha256:$('f' * 64)" }
if ($script:options.scenario -eq 'running-env-changed') { $existingBackend[0].Config.Env += 'UNEXPECTED_PROVIDER_KEY=sensitive-test-running' }
if ($script:options.scenario -eq 'running-command-changed') { $existingBackend[0].Config.Cmd = @('python', '-m', 'backend.main'); $existingBackend[0].Path = 'python'; $existingBackend[0].Args = @('-m', 'backend.main') }
if ($script:options.scenario -eq 'running-healthcheck-changed') { $existingBackend[0].Config.Healthcheck = @{ Test = @('CMD', 'true') } }
if ($script:options.scenario -in @('stopped-needs-upgrade', 'stopped-config-changed')) {
    $existingBackend[0].State.Running = $false; $existingBackend[0].State.Status = 'exited'; $script:state.heads = @()
    if ($script:options.scenario -eq 'stopped-config-changed') { $existingBackend[0].Image = "sha256:$('f' * 64)" }
}

function global:Get-NetTCPConnection {
    param($State, $ErrorAction)
    Add-Content -LiteralPath $script:checksPath -Value '"host-ports"'
    if ($script:options.scenario -eq 'port-observation-error') { throw 'sensitive-test host error' }
    $config = Read-FakeConfig @()
    if ($script:options.scenario -eq 'host-port-conflict') {
        [pscustomobject]@{ LocalPort = [int]$config.services.backend.ports[0].published; LocalAddress = '127.0.0.1' }
    } elseif ($script:options.reuse) {
        foreach ($container in $script:state.containers) {
            if ($container.State.Running) {
                foreach ($target in $container.NetworkSettings.Ports.Keys) {
                    foreach ($binding in $container.NetworkSettings.Ports[$target]) {
                        [pscustomobject]@{ LocalPort = [int]$binding.HostPort; LocalAddress = $binding.HostIp }
                    }
                }
            }
        }
    }
}

function global:docker {
    $stdinText = @($input) -join "`n"
    $arguments = @($args)
    $script:callIndex++
    Add-Content -LiteralPath $script:callsPath -Value (ConvertTo-Json -InputObject $arguments -Compress)
    $leaked = @('COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES', 'POSTGRES_DB',
        'JWT_SECRET_KEY', 'LLM_PROVIDER', 'UNRELATED_PROVIDER_SECRET', 'EDUAGENT_ENVIRONMENT') | Where-Object {
        [Environment]::GetEnvironmentVariable($_, 'Process')
    }
    Add-Content -LiteralPath $script:checksPath -Value (ConvertTo-Json -InputObject @{ inherited = @($leaked); env_file = $env:EDUAGENT_ENV_FILE } -Compress)
    $global:LASTEXITCODE = 0
    $container = $null
    if ($args[0] -eq 'container' -and $args[1] -in @('start', 'rm')) {
        $id = [string]$args[-1]
        $found = @($script:state.containers | Where-Object { $_.Id -eq $id })
        if ($found.Count -ne 1) { throw 'sensitive-test exact container id not found' }
        $container = $found[0]
    }
    $phase = if ($container) { [string]$container.Config.Labels['io.eduagent.phase'] } else { '' }
    $semanticFailure = ($args[0] -eq 'container' -and $args[1] -eq 'start' -and
        ($script:options.fail -eq "$phase-start" -or ($script:options.fail -eq 'alembic' -and $phase -eq 'migration'))) -or
        ($args -contains 'up' -and $args[-1] -eq 'backend' -and $script:options.fail -eq 'backend-up')
    if (($script:options.fail -and $args -contains $script:options.fail) -or $semanticFailure) {
        Write-FakeEvent 'command-failed' @{ phase = $phase; action = $(if ($args -contains 'up') { 'up' } else { [string]$args[1] }) }
        Write-Output 'sensitive-test docker stdout'; Write-Error 'sensitive-test docker stderr' -ErrorAction Continue
        $global:LASTEXITCODE = 23
        return
    }
    if ($args[0] -eq 'compose') {
        $config = Read-FakeConfig $arguments $stdinText
        Add-Content -LiteralPath $script:snapshotsPath -Value (@{ call_index = $script:callIndex; stdin = [bool]$stdinText; model = $config } | ConvertTo-Json -Depth 45 -Compress)
        if ($args -contains 'config') {
            $script:configCalls++
            if ($script:options.scenario -eq 'malformed-config') { return '{sensitive-test invalid json' }
            if ($script:options.scenario -eq 'source-mutated' -and $script:configCalls -eq 2) {
                $changed = Read-FakeConfig @()
                $changed.services.backend.environment.JWT_SECRET_KEY = 'sensitive-test-source-was-reloaded'
                $changed | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $script:configPath -Encoding utf8
                Set-Content -LiteralPath $script:options.env_argument -Value 'JWT_SECRET_KEY=sensitive-test-source-was-reloaded'
                Set-Content -LiteralPath (Join-Path (Split-Path (Split-Path $script:scriptPath)) 'docker-compose.yml') -Value 'name: changed-after-selection'
                Write-FakeEvent 'original-sources-mutated'
            }
            return ($config | ConvertTo-Json -Depth 40 -Compress)
        }
        if ($args -contains 'pull' -and $args -contains '--policy') { Write-FakeEvent 'images-pulled'; return 'sensitive-test pulled' }
        if ($args -contains 'build' -and $args -notcontains 'up') { Write-FakeEvent 'backend-built'; return 'sensitive-test built' }
        if ($args -contains 'create' -or $args -contains 'up') {
            $action = if ($args -contains 'create') { 'create' } else { 'up' }
            foreach ($service in @(Get-FakeServiceNames $arguments $action $config)) {
                $created = Add-FakeContainer $config ([string]$service)
                if ($action -eq 'up') { Start-FakeContainer $created }
            }
            Save-FakeState
            return 'sensitive-test startup output must not escape'
        }
        if ($args -contains 'psql') {
            Write-FakeEvent 'postgres-observed'
            $observation = Get-FakeIdentity $script:state.environment
            if ($script:options.scenario -eq 'probe-pg-disagrees') { $observation.database.actual.system_identifier = '7694989589121413158' }
            return ($observation.database.actual | ConvertTo-Json -Compress)
        }
        if ($args -contains 'redis-cli') {
            Write-FakeEvent 'redis-observed'
            $observation = Get-FakeIdentity $script:state.environment
            $runId = if ($script:options.scenario -eq 'probe-redis-disagrees') { 'b' * 40 } else { $observation.redis.run_id }
            return "# Server`nredis_version:7.4.0`nrun_id:$runId`n"
        }
        if ($args -contains 'scripts.demo_seed') { Write-FakeEvent 'seed' @{ heads = @($script:state.heads) }; return 'sensitive-test business output must not escape' }
        if ($args -contains '-c' -and [string]$args[-1] -match 'T232_DEMO_BOOTSTRAP_STATUS') {
            Write-FakeEvent 'bootstrap-status'
            $available = $script:options.scenario -notin @('backend-unhealthy', 'backend-no-health')
            return (@{ setup_http = $(if ($available) { 200 } else { 503 }); setup_ready = $available;
                business_ready = $available -and $script:options.scenario -ne 'setup-only' } | ConvertTo-Json -Compress)
        }
        if ($args -contains '-c') {
            Write-FakeEvent 'backend-identity' @{ heads = @($script:state.heads) }
            return ((Get-FakeIdentity $script:state.environment) | ConvertTo-Json -Depth 20 -Compress)
        }
        if ($args -contains 'ps') {
            $services = @(Get-FakeServiceNames $arguments 'ps' $config)
            foreach ($item in $script:state.containers) { if ($item.Config.Labels['com.docker.compose.service'] -in $services) { Write-Output $item.Id } }
            return
        }
        throw 'sensitive-test unexpected compose action'
    }
    $kind = [string]$args[0]
    if ($kind -eq 'image' -and $args[1] -eq 'inspect') {
        Write-FakeEvent 'image-inspected'
        $images = @($args | Select-Object -Skip 2 | ForEach-Object { Get-FakeImage ([string]$_) })
        return (ConvertTo-Json -InputObject $images -Depth 20 -Compress)
    }
    if ($kind -eq 'container' -and $args[1] -eq 'start') {
        Start-FakeContainer $container
        if ($phase -eq 'migration') {
            Write-FakeEvent 'migration' @{ id = $container.Id }
            if ($script:options.scenario -eq 'migration-exit-nonzero') { $container.State.ExitCode = 7 }
            elseif ($script:options.scenario -ne 'migration-head-mismatch') { $script:state.heads = @($script:head); Write-FakeEvent 'head-complete' @{ id = $container.Id; heads = @($script:state.heads) } }
        } elseif ($phase -eq 'identity') { Write-FakeEvent 'identity-probe' @{ id = $container.Id; heads = @($script:state.heads) } }
        $container.State.Running = $false; $container.State.Status = 'exited'
        Save-FakeState
        return ((Get-FakeIdentity $script:state.environment ($phase -eq 'migration')) | ConvertTo-Json -Depth 20 -Compress)
    }
    if ($kind -eq 'container' -and $args[1] -eq 'rm') {
        if ($container.State.Running) { $global:LASTEXITCODE = 1; return 'sensitive-test cannot remove running container without force' }
        Write-FakeEvent 'container-removed' @{ id = $container.Id; service = $container.Config.Labels['com.docker.compose.service']; phase = $phase }
        $script:state.containers = @($script:state.containers | Where-Object { $_.Id -ne $container.Id }); Save-FakeState
        return $container.Id
    }
    if ($args[1] -eq 'ls') {
        if ($kind -eq 'volume') {
            $script:volumeLists++
            if ($script:options.scenario -eq 'race-volume' -and $script:volumeLists -eq 2) { $null = Add-FakeVolume (Read-FakeConfig @()) 'storage_data' }
        }
        $key = @{ volume = 'volumes'; network = 'networks'; container = 'containers' }[$kind]
        foreach ($resource in $script:state[$key]) {
            if ($kind -eq 'container') { Write-Output $resource.Id; continue }
            $labels = @($resource.Labels.Keys | ForEach-Object { "$_=$($resource.Labels[$_])" }) -join ','
            @{ Name = $resource.Name; Labels = $labels } | ConvertTo-Json -Compress
        }
        return
    }
    if ($args[1] -eq 'inspect') {
        $key = @{ volume = 'volumes'; network = 'networks'; container = 'containers' }[$kind]
        $requested = [string]$args[2]
        $found = @($script:state[$key] | Where-Object { $_.Name -eq $requested -or $_.Name -eq "/$requested" -or $_.Id -eq $requested })
        if ($found.Count -ne 1) { throw 'sensitive-test inspect not found' }
        if ($kind -eq 'container') { Write-FakeEvent 'container-inspected' @{ id = $found[0].Id; phase = [string]$found[0].Config.Labels['io.eduagent.phase']; running = [bool]$found[0].State.Running; exit_code = $found[0].State.ExitCode; health = $found[0].State.Health.Status } }
        return (ConvertTo-Json -InputObject @($found) -Depth 40 -Compress)
    }
    throw 'sensitive-test unexpected docker action'
}

$before = @{}
Get-ChildItem Env: | ForEach-Object { $before[$_.Name] = $_.Value }
$arguments = @{ WaitTimeoutSeconds = 30 }
if (-not $script:options.default_env) { $arguments.EnvFile = $script:options.env_argument }
$global:LASTEXITCODE = 0
& $script:scriptPath @arguments
$code = $LASTEXITCODE
$after = @{}
Get-ChildItem Env: | ForEach-Object { $after[$_.Name] = $_.Value }
$restored = $before.Count -eq $after.Count
foreach ($key in $before.Keys) { if ($before[$key] -cne $after[$key]) { $restored = $false } }
@{ restored = $restored } | ConvertTo-Json -Compress | Set-Content -LiteralPath $script:restorePath -Encoding utf8
if (-not $restored) { exit 99 }
exit $code
"""
# Global fake functions can be called from a different .ps1 script scope.
DRIVER = DRIVER.replace('$script:', '$global:T087_')


@pytest.fixture
def powershell():
    command = os.environ.get("T202_POWERSHELL") or shutil.which("pwsh") or shutil.which("powershell")
    if not command:
        pytest.skip("T087 脚本验证需要 PowerShell。")
    return command


def test_run_demo_script_exists_and_parses(powershell):
    assert SCRIPT.is_file()
    result = subprocess.run([
        powershell, "-NoProfile", "-NonInteractive", "-Command",
        ("$tokens=$null; $errors=$null; "
         "[System.Management.Automation.Language.Parser]::ParseFile("
         f"'{SCRIPT.as_posix()}', [ref]$tokens, [ref]$errors) | Out-Null; "
         "if ($errors.Count) { $errors; exit 1 }"),
    ], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _compose_config(missing=None, environment_changes=None, backend_port=18000):
    environment = {
        "JWT_SECRET_KEY": "sensitive-test-secret-" * 3,
        "DEV_MODE": "true", "LLM_PROVIDER": "deepseek",
        "DEEPSEEK_MODEL": "deepseek-chat", "DEEPSEEK_BASE_URL": "https://example.invalid",
        "DEEPSEEK_API_KEY": "sensitive-test-api-key", "RERANK_PROVIDER": "llm",
        "CONFIDENCE_THRESHOLD": "0.80", "EMBEDDING_MODEL": "test-model-1024",
        "EMBEDDING_API_KEY": "sensitive-test-embedding-key", "STORAGE_ROOT": "/app/storage",
        "DATABASE_URL": "postgresql+psycopg://eduagent@postgres:5432/eduagent",
        "REDIS_URL": "redis://redis:6379/0",
    }
    if missing:
        environment.pop(missing)
    environment.update(environment_changes or {})
    config = {"name": "eduagent-demo", "services": {}, "volumes": {},
              "networks": {"default": {"name": "eduagent-demo_default"}}}
    for service, (target, published, volume, mount) in SERVICES.items():
        config["services"][service] = {
            "environment": {}, "networks": {"default": None}, "restart": "unless-stopped",
            "healthcheck": {"test": ["CMD", "synthetic-healthcheck"]},
            "ports": [{"target": target, "published": str(backend_port if service == "backend" else published),
                       "host_ip": "127.0.0.1", "protocol": "tcp"}],
            "volumes": [{"type": "volume", "source": volume, "target": mount}],
        }
        config["volumes"][volume] = {"name": f"eduagent-demo_{volume}"}
    config["services"]["backend"].update({
        "environment": environment, "env_file": [{"path": "synthetic-original.env", "required": True}],
        "build": {"context": "/synthetic-demo-context", "dockerfile": "Dockerfile"},
        "depends_on": {"postgres": {"condition": "service_healthy"}, "redis": {"condition": "service_healthy"}},
    })
    config["services"]["backend"]["healthcheck"] = {"test": [
        "CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4)",
    ]}
    config["services"]["postgres"].update({
        "image": "pgvector/pgvector:pg16", "environment": {"POSTGRES_DB": "eduagent", "POSTGRES_USER": "eduagent"},
        "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]},
    })
    config["services"]["redis"].update({"image": "redis:7.4-alpine", "command": ["redis-server", "--appendonly", "yes"],
                                          "healthcheck": {"test": ["CMD", "redis-cli", "ping"]}})
    return config


def _business_calls(calls):
    return [call for call in calls if
            (call[0] == "compose" and any(action in call for action in ("up", "alembic", "scripts.demo_seed")))
            or call[:2] == ["container", "start"]]


def _events(run_demo, name):
    return [event for event in run_demo.events if event["event"] == name]


def _assert_no_business_backend(run_demo):
    assert not _events(run_demo, "backend-start")
    assert not _events(run_demo, "backend-recovery")
    assert not _events(run_demo, "seed")


def _assert_no_destructive_calls(calls):
    assert not any(action in call for call in calls for action in ("stop", "down", "prune", "reset"))
    assert not any(call[:2] == ["container", "rm"] and any(flag in call for flag in ("-f", "--force", "-v", "--volumes"))
                   for call in calls)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]


@pytest.fixture
def run_demo(powershell, tmp_path):
    project = tmp_path / "project"
    script = project / "scripts" / "run_demo.ps1"
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT.read_text(encoding="utf-8-sig"), encoding="utf-8")
    (project / "docker-compose.yml").write_text(COMPOSE.read_text(encoding="utf-8-sig"), encoding="utf-8")
    for path in (project / ".env", tmp_path / ".env", tmp_path / "Development.env"):
        path.write_text("JWT_SECRET_KEY=sensitive-test-development\n", encoding="utf-8")
    local = tmp_path / "local"
    internal = local / "EduAgent" / "demo" / "config"
    internal.mkdir(parents=True)
    default_env = internal / "demo.env"
    default_env.write_text("# synthetic Demo only\n", encoding="utf-8")
    explicit_env = tmp_path / "explicit config" / "demo.env"
    explicit_env.parent.mkdir()
    explicit_env.write_text("# explicitly selected synthetic env\n", encoding="utf-8")
    paths = {name: tmp_path / name for name in ("compose.json", "calls.jsonl", "checks.jsonl", "options.json",
                                               "state.json", "events.jsonl", "snapshots.jsonl", "restored.json", "driver.ps1")}
    paths["driver.ps1"].write_text(DRIVER, encoding="utf-8")

    def execute(*, missing=None, fail="", scenario="", reuse=False, default_env_selected=False,
                default_missing=False, relative_env=False, environment_changes=None, backend_port=18000):
        if default_missing:
            default_env.unlink(missing_ok=True)
        paths["compose.json"].write_text(json.dumps(_compose_config(missing, environment_changes, backend_port)), encoding="utf-8")
        env_argument = str(explicit_env.relative_to(tmp_path) if relative_env else explicit_env)
        paths["options.json"].write_text(json.dumps({"fail": fail, "scenario": scenario, "reuse": reuse,
                                                    "default_env": default_env_selected, "env_argument": env_argument}), encoding="utf-8")
        for name in ("calls.jsonl", "checks.jsonl", "events.jsonl", "snapshots.jsonl"):
            paths[name].write_text("", encoding="utf-8")
        process_environment = {
            **os.environ, "T087_CONFIG": str(paths["compose.json"]), "T087_CALLS": str(paths["calls.jsonl"]),
            "T087_CHECKS": str(paths["checks.jsonl"]), "T087_OPTIONS": str(paths["options.json"]),
            "T087_EVENTS": str(paths["events.jsonl"]), "T087_SNAPSHOTS": str(paths["snapshots.jsonl"]),
            "T087_STATE": str(paths["state.json"]), "T087_SCRIPT": str(script), "T087_RESTORE": str(paths["restored.json"]),
            "LOCALAPPDATA": str(local), "EDUAGENT_ENV_FILE": "original-test.env", "EDUAGENT_ENVIRONMENT": "development",
            "COMPOSE_FILE": "sensitive-test-development.yml", "COMPOSE_PROJECT_NAME": "development",
            "COMPOSE_PROFILES": "development", "COMPOSE_ENV_FILES": "sensitive-test-development.env",
            "POSTGRES_DB": "development", "JWT_SECRET_KEY": "sensitive-test-inherited-jwt",
            "LLM_PROVIDER": "sensitive-test-inherited-provider", "UNRELATED_PROVIDER_SECRET": "sensitive-test-arbitrary-key",
        }
        result = subprocess.run([
            powershell, "-NoProfile", "-NonInteractive", "-File", str(paths["driver.ps1"]),
        ], cwd=tmp_path, env=process_environment, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        calls = _read_jsonl(paths["calls.jsonl"])
        checks = _read_jsonl(paths["checks.jsonl"])
        execute.events = _read_jsonl(paths["events.jsonl"])
        execute.snapshots = _read_jsonl(paths["snapshots.jsonl"])
        execute.state = json.loads(paths["state.json"].read_text(encoding="utf-8-sig")) if paths["state.json"].exists() else None
        assert "sensitive-test" not in result.stdout + result.stderr
        assert "sensitive-test" not in json.dumps(calls)
        assert json.loads(paths["restored.json"].read_text(encoding="utf-8-sig"))["restored"]
        for check in checks:
            if isinstance(check, dict):
                assert check["inherited"] == []
                assert Path(check["env_file"]) == (default_env if default_env_selected else explicit_env)
        for name in ("demo-creation.json", "demo-compose.override.json"):
            if (internal / name).exists():
                assert "sensitive-test" not in (internal / name).read_text(encoding="utf-8-sig")
        execute.checks = checks
        return result, calls

    execute.internal = internal
    execute.default_env = default_env
    execute.explicit_env = explicit_env
    execute.base_compose = project / "docker-compose.yml"
    return execute


def test_run_demo_waits_then_migrates_then_seeds_before_reporting_success(run_demo):
    result, calls = run_demo()
    assert result.returncode == 0, result.stdout + result.stderr
    starts = [call for call in calls if call[0] == "compose" and "up" in call]
    assert [call[call.index("up"):] for call in starts] == [UP + ["postgres", "redis"], UP[:7] + ["backend"]]
    assert calls[0][-3:] == ["config", "--format", "json"]
    assert len([snap for snap in run_demo.snapshots if not snap["stdin"]]) == 2
    for kind in ("volume", "network", "container"):
        assert any(call[:2] == [kind, "inspect"] for call in calls)
    assert run_demo.checks.count("host-ports") >= 2
    order = [run_demo.events.index(_events(run_demo, name)[0]) for name in (
        "identity-probe", "migration", "head-complete", "backend-start", "backend-recovery", "backend-identity", "seed")]
    assert order == sorted(order) and len(set(order)) == len(order)
    assert _events(run_demo, "seed")[0]["heads"] == [HEAD]
    assert not any("alembic" in call for call in calls)
    assert next(call for call in calls if "scripts.demo_seed" in call)[-6:] == [
        "exec", "-T", "backend", "python", "-m", "scripts.demo_seed"]
    assert "DEMO_PREVIEW" in result.stdout
    assert "http://127.0.0.1:18000/gradio" in result.stdout
    assert "dev_teacher / dev_student / dev_admin" in result.stdout
    assert "Demo UI running" in result.stdout and "Demo ready" not in result.stdout
    assert "awaiting_teacher_review" in result.stdout and "awaiting_exam_scoring" in result.stdout
    receipt = json.loads((run_demo.internal / "demo-creation.json").read_text(encoding="utf-8-sig"))
    assert receipt["schema_version"] == 2
    assert receipt["state"] == "identity_verified" and receipt["last_stage"] == "seed_completed"
    assert receipt["identity"]["database"]["system_identifier"] == "7694989589121413158"
    assert receipt["identity"]["redis_run_id"] == "b" * 40
    assert len(receipt["resources"]["containers"]) == 3
    assert all(item["created_at"] and item["mountpoint"] for item in receipt["resources"]["volumes"])
    _assert_no_destructive_calls(calls)


@pytest.mark.parametrize("missing", ["DEV_MODE"])
def test_run_demo_missing_config_stops_before_startup(run_demo, missing):
    result, calls = run_demo(missing=missing)
    assert result.returncode == 1 and len(calls) == 1
    assert "DEMO_" in result.stdout and "Demo ready" not in result.stdout
    _assert_no_business_backend(run_demo)


@pytest.mark.parametrize("stage", ["up", "alembic", "scripts.demo_seed"])
def test_run_demo_command_failure_stops_later_steps(run_demo, stage):
    result, calls = run_demo(fail=stage)
    assert result.returncode == 1
    assert "DEMO_STEP_FAILED" in result.stdout and "Demo UI running" not in result.stdout
    assert "Demo ready" not in result.stdout and _events(run_demo, "command-failed")
    assert not _events(run_demo, "seed")
    if stage != "scripts.demo_seed":
        _assert_no_business_backend(run_demo)
    if stage == "up":
        assert not _events(run_demo, "identity-probe") and not _events(run_demo, "migration")
    receipt = json.loads((run_demo.internal / "demo-creation.json").read_text(encoding="utf-8-sig"))
    assert receipt["last_error_code"] == "DEMO_STEP_FAILED"
    _assert_no_destructive_calls(calls)


def _assert_compose_boundary(run_demo, calls, selected_env):
    previews = 0
    for call in calls:
        if call[0] != "compose":
            continue
        assert call[call.index("--project-name") + 1] == "eduagent-demo"
        file = call[call.index("--file") + 1]
        if file == "-":
            assert previews == 2
            assert call.count("--file") == 1
            assert call[call.index("--env-file") + 1].upper() == "NUL"
            assert Path(call[call.index("--project-directory") + 1]) == run_demo.base_compose.parent
        else:
            previews += 1
            assert call[-3:] == ["config", "--format", "json"]
            assert Path(file) == run_demo.base_compose and Path(file).is_absolute()
            assert Path(call[call.index("--env-file") + 1]) == selected_env
    assert previews == 2


def test_demo_project_is_independent_in_every_compose_call(run_demo):
    result, calls = run_demo()
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_compose_boundary(run_demo, calls, run_demo.explicit_env)


def test_zero_arguments_selects_only_independent_demo_env(run_demo):
    result, calls = run_demo(default_env_selected=True)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_compose_boundary(run_demo, calls, run_demo.default_env)


def test_missing_default_env_does_not_fall_back_or_copy_development_secrets(run_demo):
    result, calls = run_demo(default_env_selected=True, default_missing=True)
    assert result.returncode == 1 and calls == []
    assert "DEMO_CONFIG_MISSING" in result.stdout and not run_demo.default_env.exists()
    assert not (run_demo.internal / "demo-creation.json").exists()
    assert not (run_demo.internal / "demo-compose.override.json").exists()


def test_explicit_relative_envfile_with_spaces_is_preserved(run_demo):
    result, calls = run_demo(relative_env=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(calls[0][calls[0].index("--env-file") + 1]) == run_demo.explicit_env


def test_published_ui_port_comes_from_parsed_compose_config(run_demo):
    result, _ = run_demo(backend_port=28123)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "http://127.0.0.1:28123/gradio" in result.stdout and "http://127.0.0.1:18000/gradio" not in result.stdout


@pytest.mark.parametrize("scenario", ["unknown-volume", "unknown-network", "unknown-container", "race-volume"])
def test_unknown_resources_are_never_adopted_even_with_correct_project_name(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1 and not _business_calls(calls)
    assert not any(call[0] == "compose" and "create" in call for call in calls)
    assert "DEMO_RESOURCE_UNOWNED" in result.stdout and not (run_demo.internal / "demo-creation.json").exists()
    assert not any(action in call for call in calls for action in ("rm", "down", "prune", "reset", "stop"))


@pytest.mark.parametrize("scenario", ["host-port-conflict", "docker-port-conflict", "port-observation-error"])
def test_port_observation_conflicts_fail_closed_before_up(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1 and not _business_calls(calls)
    assert "DEMO_PORT_" in result.stdout and not (run_demo.internal / "demo-creation.json").exists()


@pytest.mark.parametrize(("field", "value"), [
    ("EDUAGENT_ENVIRONMENT", "development"), ("EDUAGENT_RUNTIME_KIND", "windows"),
    ("EDUAGENT_DATA_ROOT", "/app/development"), ("STORAGE_ROOT", "/app/development/storage"),
    ("EDUAGENT_ENVIRONMENT_ID", "11111111-1111-4111-8111-111111111111"),
    ("DATABASE_URL", "postgresql+psycopg://development@postgres:5432/development"),
    ("REDIS_URL", "redis://development:6379/0"), ("EDUAGENT_ENV_FILE", "/app/Development.env"),
])
def test_base_config_cross_environment_is_rejected_not_hidden_by_override(run_demo, field, value):
    result, calls = run_demo(environment_changes={field: value})
    assert result.returncode == 1 and len(calls) == 1
    assert "DEMO_CONFIG_ENVIRONMENT_MISMATCH" in result.stdout
    assert not (run_demo.internal / "demo-compose.override.json").exists()


@pytest.mark.parametrize("scenario", ["malformed-config"])
def test_parser_errors_are_controlled_and_do_not_echo_config(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1 and len(calls) == 1
    assert "DEMO_CONFIG_INVALID" in result.stdout and "stage=configuration" in result.stdout


@pytest.mark.parametrize("stage", ["config", "inspect", "psql", "redis-cli"])
def test_readonly_docker_failures_stop_before_any_business_write(run_demo, stage):
    result, calls = run_demo(fail=stage)
    assert result.returncode == 1
    _assert_no_business_backend(run_demo)
    assert not _events(run_demo, "migration")
    assert not any("alembic" in call or "scripts.demo_seed" in call for call in calls)
    assert "Demo UI running" not in result.stdout


@pytest.mark.parametrize("scenario", ["post-owner", "post-database", "post-user", "post-schema", "post-redis",
                                      "post-root", "post-environment-id", "post-cache-root"])
def test_actual_created_identity_must_match_before_migration_or_seed(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1
    assert len([call for call in calls if call[0] == "compose" and "up" in call]) == 1
    _assert_no_business_backend(run_demo)
    assert not _events(run_demo, "migration") and "Demo UI running" not in result.stdout
    receipt = json.loads((run_demo.internal / "demo-creation.json").read_text(encoding="utf-8-sig"))
    assert receipt["state"] == "creating"
    assert receipt["last_error_code"].startswith("DEMO_")


def test_complete_internal_receipt_and_exact_actual_resources_can_be_reused(run_demo):
    first, _ = run_demo()
    assert first.returncode == 0, first.stdout + first.stderr
    path = run_demo.internal / "demo-creation.json"
    before = json.loads(path.read_text(encoding="utf-8-sig"))
    second, calls = run_demo(reuse=True)
    assert second.returncode == 0, second.stdout + second.stderr
    after = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("environment_id", "creation_id", "resources", "identity"):
        assert after[key] == before[key]
    assert not any(call[0] == "compose" and "up" in call for call in calls)
    assert not _events(run_demo, "migration") and not _events(run_demo, "backend-recovery")
    assert len(_events(run_demo, "identity-probe")) == 1 and len(_events(run_demo, "seed")) == 1
    assert _events(run_demo, "identity-probe")[0]["heads"] == [HEAD]
    assert run_demo.events.index(_events(run_demo, "identity-probe")[0]) < run_demo.events.index(_events(run_demo, "seed")[0])
    assert any("psql" in call for call in calls) and any("redis-cli" in call for call in calls)
    _assert_no_destructive_calls(calls)


@pytest.mark.parametrize("scenario", ["wrong-owner", "wrong-container-id", "wrong-network-id", "wrong-created-at",
                                      "wrong-mount", "post-root", "changed-pg-instance", "changed-redis-instance",
                                      "wrong-container-created-at", "wrong-network-created-at",
                                      "changed-storage-device", "changed-storage-inode"])
def test_receipt_is_not_sufficient_when_actual_owner_id_root_or_instance_changes(run_demo, scenario):
    first, _ = run_demo()
    assert first.returncode == 0, first.stdout + first.stderr
    result, calls = run_demo(reuse=True, scenario=scenario)
    assert result.returncode == 1
    _assert_no_business_backend(run_demo)
    assert not _events(run_demo, "migration")
    assert not any(call[0] == "compose" and "up" in call for call in calls)
    assert "DEMO_" in result.stdout and "Demo UI running" not in result.stdout
    assert not any(action in call for call in calls for action in ("rm", "down", "prune", "reset", "stop"))


def test_partial_creation_receipt_cannot_silently_adopt_residual_resources(run_demo):
    first, _ = run_demo(scenario="post-database")
    assert first.returncode == 1
    second, calls = run_demo(reuse=True)
    assert second.returncode == 1 and calls == []
    assert "DEMO_RECEIPT_INCOMPLETE" in second.stdout


def test_first_up_explicitly_starts_only_postgres_and_redis(run_demo):
    result, calls = run_demo()
    assert result.returncode == 0, result.stdout + result.stderr
    starts = [call for call in calls if call[0] == "compose" and "up" in call]
    assert starts, "Demo must explicitly start its prerequisite services"
    assert starts[0][starts[0].index("up"):] == UP + ["postgres", "redis"], (
        "The first up must not implicitly create/start the business backend")
    end_of_base = next(index for index, event in enumerate(run_demo.events)
                       if event["call_index"] > calls.index(starts[0]))
    base_events = run_demo.events[:end_of_base]
    assert {event["service"] for event in base_events if event["event"] == "container-created"} == {"postgres", "redis"}
    assert {event["name"] for event in base_events if event["event"] == "volume-created"} == {
        "eduagent-demo_postgres_data", "eduagent-demo_redis_data"}


def test_business_backend_cannot_start_or_recover_before_oneoff_migration_head(run_demo):
    result, calls = run_demo()
    assert result.returncode == 0, result.stdout + result.stderr
    heads = _events(run_demo, "head-complete")
    assert len(heads) == 1 and heads[0]["heads"] == [HEAD]
    for name in ("backend-start", "backend-recovery", "seed"):
        events = _events(run_demo, name)
        assert len(events) == 1 and events[0]["call_index"] > heads[0]["call_index"]
    created = [event for event in _events(run_demo, "container-created") if event["service"] == "backend"]
    assert len(created) == 1 and created[0]["call_index"] > heads[0]["call_index"]
    backend_up = next(index for index, call in enumerate(calls) if call[0] == "compose" and "up" in call and call[-1] == "backend")
    assert any(event["event"] == "container-inspected" and event["id"] == created[0]["id"]
               and not event["running"] and created[0]["call_index"] < event["call_index"] < backend_up
               for event in run_demo.events)


def test_frozen_model_uses_only_stdin_then_pins_all_runtime_images(run_demo):
    result, calls = run_demo(scenario="source-mutated")
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_compose_boundary(run_demo, calls, run_demo.explicit_env)
    assert _events(run_demo, "original-sources-mutated")
    frozen = [snapshot for snapshot in run_demo.snapshots if snapshot["stdin"]]
    assert frozen and all(snapshot["model"]["services"]["backend"]["environment"]["JWT_SECRET_KEY"] == "sensitive-test-secret-" * 3
                          for snapshot in frozen)
    pulls = [call for call in calls if call[0] == "compose" and "pull" in call and "--policy" in call]
    assert len(pulls) == 1 and pulls[0][pulls[0].index("pull"):] == ["pull", "--policy", "missing", "postgres", "redis"]
    builds = [call for call in calls if call[0] == "compose" and "build" in call]
    assert len(builds) == 1 and builds[0][-2:] == ["build", "backend"]
    built = next(snapshot["model"]["services"]["backend"] for snapshot in frozen if snapshot["call_index"] == calls.index(builds[0]))
    assert built["image"].startswith("eduagent-demo") and "sha256:" not in built["image"]
    runtime = [snapshot for snapshot in frozen if any(action in calls[snapshot["call_index"]] for action in ("up", "create", "exec"))]
    for snapshot in runtime:
        for service, letter in (("backend", "e"), ("postgres", "d"), ("redis", "c")):
            definition = snapshot["model"]["services"][service]
            assert definition["image"] == "sha256:" + letter * 64
            assert all(key not in definition for key in ("build", "env_file", "depends_on"))


def test_oneoff_steps_clone_backend_without_running_business_entrypoint(run_demo):
    result, calls = run_demo()
    assert result.returncode == 0, result.stdout + result.stderr
    step_snapshots = [snapshot for snapshot in run_demo.snapshots
                      if "create" in calls[snapshot["call_index"]] and calls[snapshot["call_index"]][-1] == "demo_step"]
    assert len(step_snapshots) == 2
    names = []
    for snapshot, phase, marker in zip(step_snapshots, ("identity", "migration"),
                                       ("# T202_DEMO_IDENTITY_PROBE", "# T202_DEMO_MIGRATION"), strict=True):
        model = snapshot["model"]
        step, backend = model["services"]["demo_step"], model["services"]["backend"]
        assert step["labels"]["io.eduagent.phase"] == phase
        assert step["image"] == backend["image"] and step["environment"] == backend["environment"]
        assert step["entrypoint"] == ["python", "-B", "-c"] and len(step["command"]) == 1
        code = step["command"][0]
        assert marker in code and "get_settings" not in code
        assert "DATABASE_URL" in code and "REDIS_URL" in code
        assert "import backend.main" not in code and "from backend.main" not in code
        assert "scripts.demo_seed" not in code
        if phase == "identity":
            assert "upgrade(" not in code and "mkdir(" not in code and "write_text(" not in code
        else:
            assert "upgrade" in code and "head" in code
        assert step["read_only"] is True
        assert len(step["volumes"]) == 1 and step["volumes"][0]["read_only"] is True
        assert step["volumes"][0]["source"] == backend["volumes"][0]["source"]
        assert step["volumes"][0]["target"] == backend["volumes"][0]["target"]
        assert all(key not in step for key in ("ports", "depends_on", "restart", "healthcheck", "build", "env_file"))
        names.append(step["container_name"])
        assert step["container_name"].startswith("eduagent-demo-step-") and step["container_name"].endswith("-" + phase)
        create_call = calls[snapshot["call_index"]]
        assert create_call[create_call.index("create"):] == ["create", "--no-build", "--pull", "never", "demo_step"]
        created = next(event for event in _events(run_demo, "container-created") if event["phase"] == phase)
        start_index = next(index for index, call in enumerate(calls) if call[:3] == ["container", "start", "--attach"] and call[-1] == created["id"])
        assert any(event["event"] == "container-inspected" and event["id"] == created["id"] and not event["running"]
                   and created["call_index"] < event["call_index"] < start_index for event in run_demo.events)
        remove_index = next(index for index, call in enumerate(calls) if call == ["container", "rm", created["id"]])
        assert any(event["event"] == "container-inspected" and event["id"] == created["id"]
                   and event["exit_code"] == 0 and start_index < event["call_index"] < remove_index for event in run_demo.events)
    assert len(set(names)) == 2


@pytest.mark.parametrize(("phase", "difference"), [
    (phase, difference) for phase in ("identity", "migration", "backend")
    for difference in ("image", "env", "command", "mount", "network", "restart")
] + [("identity", "readonly"), ("migration", "readonly"), ("identity", "unknown-id"), ("backend", "healthcheck")])
def test_wrong_actual_container_config_is_rejected_before_its_entrypoint(run_demo, phase, difference):
    suffix = difference if difference == "unknown-id" else difference + "-mismatch"
    result, calls = run_demo(scenario=f"{phase}-{suffix}")
    assert result.returncode == 1 and "Demo UI running" not in result.stdout
    _assert_no_business_backend(run_demo)
    if phase != "backend":
        assert not any(event["event"] == "container-start" and event.get("phase") == phase for event in run_demo.events)
    if phase == "identity":
        assert not _events(run_demo, "migration")
    failed = [event for event in _events(run_demo, "container-created") if event["phase"] == phase or (phase == "backend" and event["service"] == "backend")]
    assert failed and not any(call[:2] == ["container", "rm"] and call[-1] == failed[-1]["id"] for call in calls)
    assert any(container["Id"] == failed[-1]["id"] for container in run_demo.state["containers"])
    resource_mismatch = (phase, difference) in {
        ("backend", "mount"), ("backend", "network"), ("identity", "unknown-id"),
    }
    expected_error = "DEMO_RESOURCE_UNOWNED" if resource_mismatch else "DEMO_CONTAINER_CONFIG_MISMATCH"
    assert expected_error in result.stdout
    _assert_no_destructive_calls(calls)


@pytest.mark.parametrize("stage", ["pull", "build", "create", "identity-start", "migration-start", "backend-up"])
def test_every_new_command_phase_failure_stops_later_stages_and_keeps_residuals(run_demo, stage):
    result, calls = run_demo(fail=stage)
    assert result.returncode == 1 and "DEMO_STEP_FAILED" in result.stdout
    _assert_no_business_backend(run_demo)
    if stage != "backend-up":
        assert not _events(run_demo, "head-complete")
    if stage in ("pull", "build", "create", "identity-start"):
        assert not _events(run_demo, "migration")
    if stage.endswith("-start"):
        phase = stage.removesuffix("-start")
        created = next(event for event in _events(run_demo, "container-created") if event["phase"] == phase)
        assert any(container["Id"] == created["id"] for container in run_demo.state["containers"])
        assert ["container", "rm", created["id"]] not in calls
    assert "Demo UI running" not in result.stdout
    _assert_no_destructive_calls(calls)


@pytest.mark.parametrize("scenario", ["probe-pg-disagrees", "probe-redis-disagrees", "storage-incompatible"])
def test_probe_actual_connections_and_storage_are_checked_before_any_upgrade(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1 and "DEMO_IDENTITY_MISMATCH" in result.stdout
    assert not _events(run_demo, "migration")
    _assert_no_business_backend(run_demo)
    _assert_no_destructive_calls(calls)


@pytest.mark.parametrize("scenario", ["migration-head-mismatch", "migration-exit-nonzero"])
def test_migration_must_exit_zero_and_report_actual_target_heads_before_backend(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1
    assert "DEMO_MIGRATION_HEAD_MISMATCH" in result.stdout if scenario == "migration-head-mismatch" else "DEMO_STEP_FAILED" in result.stdout
    assert len(_events(run_demo, "migration")) == 1
    _assert_no_business_backend(run_demo)
    migration = next(event for event in _events(run_demo, "container-created") if event["phase"] == "migration")
    assert ["container", "rm", migration["id"]] not in calls
    assert any(container["Id"] == migration["id"] for container in run_demo.state["containers"])


@pytest.mark.parametrize("scenario", ["running-needs-upgrade", "running-image-changed", "running-env-changed",
                                      "running-command-changed", "running-healthcheck-changed"])
def test_running_backend_needing_upgrade_or_changed_config_requires_controlled_manual_stop(run_demo, scenario):
    first, _ = run_demo()
    assert first.returncode == 0, first.stdout + first.stderr
    result, calls = run_demo(reuse=True, scenario=scenario)
    assert result.returncode == 1 and "DEMO_CONTROLLED_STOP_REQUIRED" in result.stdout
    _assert_no_business_backend(run_demo)
    assert not _events(run_demo, "migration")
    assert not any(call[0] == "compose" and "up" in call for call in calls)
    assert not any(event["event"] == "container-removed" and event["service"] == "backend" for event in run_demo.events)
    _assert_no_destructive_calls(calls)


def test_manually_stopped_known_backend_can_upgrade_before_any_recovery(run_demo):
    first, _ = run_demo()
    assert first.returncode == 0, first.stdout + first.stderr
    before = json.loads((run_demo.internal / "demo-creation.json").read_text(encoding="utf-8-sig"))
    original_id = next(container["id"] for container in before["resources"]["containers"] if container["service"] == "backend")
    result, calls = run_demo(reuse=True, scenario="stopped-needs-upgrade")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["container", "rm", original_id] in calls
    backend = next(container for container in run_demo.state["containers"]
                   if container["Config"]["Labels"]["com.docker.compose.service"] == "backend")
    assert backend["Id"] != original_id
    head = _events(run_demo, "head-complete")
    assert len(head) == 1 and len(_events(run_demo, "backend-recovery")) == 1
    assert _events(run_demo, "backend-recovery")[0]["call_index"] > head[0]["call_index"]
    assert _events(run_demo, "seed")[0]["call_index"] > _events(run_demo, "backend-recovery")[0]["call_index"]
    _assert_no_destructive_calls(calls)


def test_canonical_literal_dollars_survive_snapshot_and_actual_container_env(run_demo):
    changes = {"DEEPSEEK_API_KEY": "sensitive-test-$$cash-$${LITERAL}-$$$$tail",
               "JWT_SECRET_KEY": "sensitive-test-$$jwt-$${JWT_LITERAL}-" * 2}
    result, _ = run_demo(environment_changes=changes)
    assert result.returncode == 0, result.stdout + result.stderr
    backend = next(container for container in run_demo.state["containers"]
                   if container["Config"]["Labels"]["com.docker.compose.service"] == "backend")
    actual = dict(entry.split("=", 1) for entry in backend["Config"]["Env"])
    assert all(actual[key] == value.replace("$$", "$") for key, value in changes.items())
    assert all(snapshot["model"]["services"]["backend"]["environment"][key] == value
               for snapshot in run_demo.snapshots if snapshot["stdin"] for key, value in changes.items())


@pytest.mark.parametrize("scenario", ["backend-unhealthy", "backend-no-health", "foundation-unhealthy", "foundation-no-health"])
def test_zero_up_exit_code_does_not_replace_actual_container_health_observation(run_demo, scenario):
    result, calls = run_demo(scenario=scenario)
    assert result.returncode == 1 and "Demo UI running" not in result.stdout
    assert not _events(run_demo, "command-failed"), "Docker up itself succeeded in this scenario"
    assert not _events(run_demo, "seed")
    service = "postgres" if scenario.startswith("foundation-") else "backend"
    actual = next(container for container in run_demo.state["containers"]
                  if container["Config"]["Labels"]["com.docker.compose.service"] == service)
    assert actual["State"]["Running"] is True
    assert actual["State"].get("Health", {}).get("Status") != "healthy"
    assert any(event["id"] == actual["Id"] and event["health"] != "healthy"
               for event in _events(run_demo, "container-inspected"))
    if service == "postgres":
        assert not _events(run_demo, "identity-probe") and not _events(run_demo, "migration")
        _assert_no_business_backend(run_demo)
    else:
        assert _events(run_demo, "head-complete")
        assert _events(run_demo, "backend-start")[0]["call_index"] > _events(run_demo, "head-complete")[0]["call_index"]
        assert not _events(run_demo, "backend-identity")
    receipt = json.loads((run_demo.internal / "demo-creation.json").read_text(encoding="utf-8-sig"))
    assert receipt["state"] == "creating" and receipt["last_stage"] != "seed_completed"
    assert receipt["last_error_code"].startswith("DEMO_")
    _assert_no_destructive_calls(calls)


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", 1), ("images", {}), ("identity", {}), ("resources", {}),
    ("active_step", {"id": "f" * 64, "phase": "migration"}),
])
def test_incomplete_or_old_receipt_never_authorizes_existing_resource_reuse(run_demo, field, value):
    first, _ = run_demo()
    assert first.returncode == 0, first.stdout + first.stderr
    path = run_demo.internal / "demo-creation.json"
    receipt = json.loads(path.read_text(encoding="utf-8-sig"))
    receipt[field] = value
    path.write_text(json.dumps(receipt), encoding="utf-8")
    result, calls = run_demo(reuse=True)
    assert result.returncode == 1 and calls == []
    assert "DEMO_RECEIPT_" in result.stdout
    _assert_no_business_backend(run_demo)
    assert "Demo UI running" not in result.stdout


@pytest.mark.parametrize("missing", ["EMBEDDING_API_KEY", "JWT_SECRET_KEY", "DEEPSEEK_API_KEY"])
def test_missing_secrets_keeps_setup_available_without_seed(run_demo, missing):
    result, calls = run_demo(missing=missing, scenario="setup-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Demo Setup running" in result.stdout and "Demo UI running" not in result.stdout
    assert "business readiness remains unavailable" in result.stdout
    assert _events(run_demo, "bootstrap-status") and not _events(run_demo, "seed")
    assert _events(run_demo, "migration")[0]["call_index"] < _events(run_demo, "backend-start")[0]["call_index"]
    backend = next(c for c in run_demo.state["containers"] if c["Config"]["Labels"]["com.docker.compose.service"] == "backend")
    assert backend["State"]["Running"] and backend["State"]["Health"]["Status"] == "unhealthy"
    receipt = json.loads((run_demo.internal / "demo-creation.json").read_text(encoding="utf-8-sig"))
    assert receipt["state"] == "identity_verified" and receipt["last_stage"] == "setup_ready"
    _assert_no_destructive_calls(calls)


def test_owned_setup_only_instance_is_reused_without_migration_or_seed(run_demo):
    first, _ = run_demo(scenario="setup-only")
    assert first.returncode == 0, first.stdout + first.stderr
    second, calls = run_demo(scenario="setup-only", reuse=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Demo Setup running" in second.stdout
    assert not _events(run_demo, "migration") and not _events(run_demo, "backend-start")
    assert not _events(run_demo, "seed") and _events(run_demo, "bootstrap-status")
    _assert_no_destructive_calls(calls)
