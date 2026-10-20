<#
.SYNOPSIS
Preview an isolated Docker Demo, verify its creation receipt, then migrate and seed.
.DESCRIPTION
Zero arguments selects %LOCALAPPDATA%/EduAgent/demo/config/demo.env only.
An explicit -EnvFile is still parsed by Compose, never by a custom dotenv reader.
Unknown or partially created resources are retained for diagnosis, never adopted/reset.
#>
[CmdletBinding()]
param(
    [string]$EnvFile,
    [ValidateRange(30, 1800)]
    [int]$WaitTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectName = 'eduagent-demo'
$baseCompose = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../docker-compose.yml'))
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$frozenComposeArgs = @('compose', '--project-name', $projectName, '--project-directory', $projectDirectory,
    '--env-file', 'NUL', '--file', '-')
$stage = 'configuration'
$failureCode = 'DEMO_CONFIG_INVALID'
$receipt = $null
$canRecordFailure = $false
$frozenConfig = $null
$imageDetails = @{}
$launchId = [guid]::NewGuid().ToString('N')
$launchLock = $null
$inherited = @{}
Get-ChildItem Env: | ForEach-Object { $inherited[$_.Name] = $_.Value }
$volumeNames = @('eduagent-demo_postgres_data', 'eduagent-demo_redis_data', 'eduagent-demo_storage_data')
$networkName = 'eduagent-demo_default'
$serviceVolumes = @{ postgres = 'postgres_data'; redis = 'redis_data'; backend = 'storage_data' }
$mountTargets = @{ postgres = '/var/lib/postgresql/data'; redis = '/data'; backend = '/app/storage' }
$servicePorts = @{ postgres = 5432; redis = 6379; backend = 8000 }
$labelOwner = 'io.eduagent.owner'
$labelEnvironment = 'io.eduagent.environment'
$labelId = 'io.eduagent.environment_id'
$labelCreation = 'io.eduagent.creation_id'
$labelRoot = 'io.eduagent.root'

function Stop-Demo([string]$Code) { throw [InvalidOperationException]::new($Code) }

function Assert-DemoLocalPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                Stop-Demo 'DEMO_LOCAL_PATH_INVALID'
            }
        }
        $current = Split-Path -Parent $current
    }
}

function Read-DemoJson([string]$Text, [string]$Code = 'DEMO_OBSERVATION_INVALID') {
    try { return ($Text | ConvertFrom-Json -AsHashtable -ErrorAction Stop) }
    catch { Stop-Demo $Code }
}

function Invoke-DemoDocker([string[]]$Arguments, [string]$Code = 'DEMO_OBSERVATION_FAILED', [string]$InputText) {
    $global:LASTEXITCODE = 0
    try {
        if ($InputText) { $output = @($InputText | & docker @Arguments 2>$null) }
        else { $output = @(& docker @Arguments 2>$null) }
    }
    catch { Stop-Demo $Code }
    if ($LASTEXITCODE -ne 0) { Stop-Demo $Code }
    return ($output -join "`n")
}

function Invoke-DemoModelCompose($Model, [string[]]$Arguments, [string]$Code = 'DEMO_STEP_FAILED') {
    # The canonical Compose model stays in process memory, not a plaintext secret file or argv.
    $payload = $Model | ConvertTo-Json -Depth 60 -Compress
    return (Invoke-DemoDocker -Arguments ($frozenComposeArgs + $Arguments) -Code $Code -InputText $payload)
}

function Invoke-DemoCompose([string[]]$Arguments, [string]$Code = 'DEMO_STEP_FAILED') {
    if ($null -ne $frozenConfig) { return (Invoke-DemoModelCompose $frozenConfig $Arguments $Code) }
    return (Invoke-DemoDocker -Arguments ($composeArgs + $Arguments) -Code $Code)
}

function Get-DemoRows([string[]]$Arguments) {
    $text = Invoke-DemoDocker -Arguments $Arguments
    foreach ($line in ($text -split "`n")) {
        if ($line.Trim()) { Read-DemoJson $line }
    }
}

function Get-DemoLabels($Labels) {
    $result = [ordered]@{}
    if ($Labels) {
        foreach ($key in ($Labels.Keys | Sort-Object)) { $result[$key] = [string]$Labels[$key] }
    }
    return $result
}

function Test-DemoEqual($Left, $Right) {
    if ($null -eq $Left -or $null -eq $Right) { return ($null -eq $Left -and $null -eq $Right) }
    if ($Left -is [Collections.IDictionary] -or $Right -is [Collections.IDictionary]) {
        if ($Left -isnot [Collections.IDictionary] -or $Right -isnot [Collections.IDictionary] -or $Left.Count -ne $Right.Count) { return $false }
        foreach ($key in $Left.Keys) {
            if (-not $Right.Contains($key) -or -not (Test-DemoEqual $Left[$key] $Right[$key])) { return $false }
        }
        return $true
    }
    if (($Left -is [Collections.IEnumerable] -and $Left -isnot [string]) -or
        ($Right -is [Collections.IEnumerable] -and $Right -isnot [string])) {
        $a = @($Left); $b = @($Right)
        if ($a.Count -ne $b.Count) { return $false }
        for ($index = 0; $index -lt $a.Count; $index++) {
            if (-not (Test-DemoEqual $a[$index] $b[$index])) { return $false }
        }
        return $true
    }
    return ((ConvertTo-Json -InputObject $Left -Compress) -ceq (ConvertTo-Json -InputObject $Right -Compress))
}

function Get-DemoStableResources($Resources) {
    $copy = Read-DemoJson ($Resources | ConvertTo-Json -Depth 30 -Compress)
    foreach ($container in $copy.containers) {
        $null = $container.Remove('running')
        $null = $container.Remove('health')
        # Stopping a container removes its live endpoint, not its declared network target or ownership.
        $null = $container.Remove('networks')
    }
    return $copy
}

function Assert-DemoConfig($Config, [bool]$Final) {
    if ($Config.name -ne $projectName -or
        (@($Config.services.Keys | Sort-Object) -join ',') -ne 'backend,postgres,redis' -or
        (@($Config.volumes.Keys | Sort-Object) -join ',') -ne 'postgres_data,redis_data,storage_data' -or
        (@($Config.networks.Keys) -join ',') -ne 'default') { Stop-Demo 'DEMO_CONFIG_INVALID' }
    foreach ($logical in @('postgres_data', 'redis_data', 'storage_data')) {
        $volume = $Config.volumes[$logical]
        if ($volume.name -ne "${projectName}_$logical" -or $volume.external -or $volume.driver_opts -or
            ($volume.driver -and $volume.driver -ne 'local')) { Stop-Demo 'DEMO_CONFIG_INVALID' }
    }
    if ($Config.networks.default.name -ne $networkName -or $Config.networks.default.external -or
        ($Config.networks.default.driver -and $Config.networks.default.driver -ne 'bridge')) {
        Stop-Demo 'DEMO_CONFIG_INVALID'
    }
    $ports = @()
    foreach ($service in @('backend', 'postgres', 'redis')) {
        $definition = $Config.services[$service]
        $mounts = @($definition.volumes)
        $bindings = @($definition.ports)
        if ($definition.container_name -or $definition.network_mode -or $definition.privileged -or
            $mounts.Count -ne 1 -or $mounts[0].type -ne 'volume' -or
            $mounts[0].source -ne $serviceVolumes[$service] -or
            $mounts[0].target -ne $mountTargets[$service] -or $mounts[0].read_only -or
            (@($definition.networks.Keys) -join ',') -ne 'default' -or $bindings.Count -ne 1) {
            Stop-Demo 'DEMO_CONFIG_INVALID'
        }
        $binding = $bindings[0]
        $number = 0
        if ($binding.target -ne $servicePorts[$service] -or
            -not [int]::TryParse([string]$binding.published, [ref]$number) -or $number -lt 1 -or $number -gt 65535 -or
            $binding.host_ip -notin @('127.0.0.1', '::1') -or
            ($binding.protocol -and $binding.protocol -ne 'tcp')) { Stop-Demo 'DEMO_CONFIG_INVALID' }
        $ports += [ordered]@{ service = $service; target = [int]$binding.target; published = $number; host_ip = [string]$binding.host_ip }
    }
    if (@($ports.published | Select-Object -Unique).Count -ne 3) { Stop-Demo 'DEMO_PORT_CONFLICT' }
    $environment = $Config.services.backend.environment
    if (([string]$environment.DEV_MODE).ToLowerInvariant() -notin @('1', 'true', 't', 'yes', 'y', 'on')) {
        Stop-Demo 'DEMO_DEV_MODE_REQUIRED'
    }
    foreach ($entry in @{ EDUAGENT_ENVIRONMENT = 'demo'; EDUAGENT_RUNTIME_KIND = 'docker'; EDUAGENT_DATA_ROOT = '/app/storage' }.GetEnumerator()) {
        if (($Final -or $environment.Contains($entry.Key)) -and $environment[$entry.Key] -cne $entry.Value) {
            Stop-Demo 'DEMO_CONFIG_ENVIRONMENT_MISMATCH'
        }
    }
    if ($environment.EDUAGENT_ENV_FILE -or $environment.EDUAGENT_TEST_RESOURCE_PROFILE -or
        $environment.STORAGE_ROOT -notin $(if ($Final) { @('/app/storage/storage') } else { @('/app/storage', '/app/storage/storage') })) {
        Stop-Demo 'DEMO_CONFIG_ENVIRONMENT_MISMATCH'
    }
    foreach ($entry in @{ EDUAGENT_ENVIRONMENT_ID = $environmentId; EDUAGENT_CREATION_ID = $creationId }.GetEnumerator()) {
        if (($Final -or $environment.Contains($entry.Key)) -and
            ([string]::IsNullOrEmpty($entry.Value) -or $environment[$entry.Key] -cne $entry.Value)) {
            Stop-Demo 'DEMO_CONFIG_ENVIRONMENT_MISMATCH'
        }
    }
    $database = $Config.services.postgres.environment
    $allowed = if ($Final) { @('eduagent_demo') } else { @('eduagent', 'eduagent_demo') }
    if ($database.POSTGRES_DB -notin $allowed -or $database.POSTGRES_USER -cne $database.POSTGRES_DB -or
        $environment.DATABASE_URL -cne "postgresql+psycopg://$($database.POSTGRES_USER)@postgres:5432/$($database.POSTGRES_DB)" -or
        $environment.REDIS_URL -cne 'redis://redis:6379/0') { Stop-Demo 'DEMO_CONFIG_ENVIRONMENT_MISMATCH' }
    if ($Final) {
        foreach ($service in @('backend', 'postgres', 'redis')) { Assert-DemoLabels $Config.services[$service].labels }
        foreach ($logical in @('postgres_data', 'redis_data', 'storage_data')) { Assert-DemoLabels $Config.volumes[$logical].labels }
        Assert-DemoLabels $Config.networks.default.labels
    }
    return ,$ports
}

function Assert-DemoLabels($Labels) {
    if (-not $Labels -or $Labels[$labelOwner] -cne 'run_demo.ps1' -or $Labels[$labelEnvironment] -cne 'demo' -or
        $Labels[$labelId] -cne $environmentId -or $Labels[$labelCreation] -cne $creationId -or
        $Labels[$labelRoot] -cne '/app/storage') { Stop-Demo 'DEMO_RESOURCE_UNOWNED' }
}

function Get-DemoInventory([string]$ExcludeContainerId) {
    $volumes = @()
    foreach ($row in @(Get-DemoRows @('volume', 'ls', '--format', '{{json .}}'))) {
        if ($row.Name -in $volumeNames -or $row.Name -like 'eduagent-demo*' -or
            $row.Labels -match '(^|,)(com.docker.compose.project=eduagent-demo|io.eduagent.environment=demo)(,|$)') {
            $volume = @(Read-DemoJson (Invoke-DemoDocker @('volume', 'inspect', [string]$row.Name)))[0]
            $volumes += [ordered]@{ name = [string]$volume.Name; created_at = [string]$volume.CreatedAt;
                driver = [string]$volume.Driver; scope = [string]$volume.Scope; mountpoint = [string]$volume.Mountpoint;
                options = (Get-DemoLabels $volume.Options); labels = (Get-DemoLabels $volume.Labels) }
        }
    }
    $networks = @()
    foreach ($row in @(Get-DemoRows @('network', 'ls', '--format', '{{json .}}'))) {
        if ($row.Name -eq $networkName -or $row.Name -like 'eduagent-demo*' -or
            $row.Labels -match '(^|,)(com.docker.compose.project=eduagent-demo|io.eduagent.environment=demo)(,|$)') {
            $network = @(Read-DemoJson (Invoke-DemoDocker @('network', 'inspect', [string]$row.Name)))[0]
            $networks += [ordered]@{ name = [string]$network.Name; id = [string]$network.Id;
                created_at = [string]$network.Created; driver = [string]$network.Driver; scope = [string]$network.Scope;
                internal = [bool]$network.Internal; labels = (Get-DemoLabels $network.Labels) }
        }
    }
    $containers = @()
    $published = @()
    $ids = Invoke-DemoDocker @('container', 'ls', '-a', '--no-trunc', '--format', '{{.ID}}')
    foreach ($id in ($ids -split "`n")) {
        if (-not $id.Trim()) { continue }
        if ($id.Trim() -cnotmatch '^[a-f0-9]{64}$') { Stop-Demo 'DEMO_OBSERVATION_INVALID' }
        $container = @(Read-DemoJson (Invoke-DemoDocker @('container', 'inspect', $id.Trim())))[0]
        if ($container.Id -cne $id.Trim()) { Stop-Demo 'DEMO_OBSERVATION_INVALID' }
        if ($ExcludeContainerId -and $container.Id -ceq $ExcludeContainerId) { continue }
        if ($container.State.Running) {
            foreach ($key in $container.NetworkSettings.Ports.Keys) {
                foreach ($binding in $container.NetworkSettings.Ports[$key]) {
                    if ($binding) { $published += @{ id = [string]$container.Id; port = [int]$binding.HostPort; host_ip = [string]$binding.HostIp } }
                }
            }
        }
        $labels = $container.Config.Labels
        if (-not $labels) { $labels = @{} }
        if ($labels['com.docker.compose.project'] -ne $projectName -and $labels[$labelEnvironment] -ne 'demo' -and
            $container.Name -notlike '/eduagent-demo*' -and
            -not @($container.Mounts | Where-Object { $_.Name -in $volumeNames }).Count -and
            $networkName -notin @($container.NetworkSettings.Networks.Keys)) { continue }
        $mounts = @($container.Mounts | Sort-Object Destination | ForEach-Object {
            [ordered]@{ name = [string]$_.Name; type = [string]$_.Type; source = [string]$_.Source;
                target = [string]$_.Destination; rw = [bool]$_.RW }
        })
        $attached = @($container.NetworkSettings.Networks.Keys | Sort-Object | ForEach-Object {
            [ordered]@{ name = [string]$_; id = [string]$container.NetworkSettings.Networks[$_].NetworkID;
                endpoint_id = [string]$container.NetworkSettings.Networks[$_].EndpointID }
        })
        $bindings = @($container.HostConfig.PortBindings.Keys | Sort-Object | ForEach-Object {
            $target = $_
            foreach ($binding in $container.HostConfig.PortBindings[$target]) {
                [ordered]@{ target = [string]$target; published = [string]$binding.HostPort; host_ip = [string]$binding.HostIp }
            }
        })
        $containers += [ordered]@{ service = [string]$labels['com.docker.compose.service']; id = [string]$container.Id;
            created_at = [string]$container.Created; image = [string]$container.Image; labels = (Get-DemoLabels $labels);
            mounts = $mounts; networks = $attached; network_mode = [string]$container.HostConfig.NetworkMode;
            ports = $bindings; running = [bool]$container.State.Running;
            health = [string]$container.State.Health.Status }
    }
    return @{ resources = [ordered]@{ volumes = @($volumes | Sort-Object { $_.name });
            networks = @($networks | Sort-Object { $_.name }); containers = @($containers | Sort-Object { $_.service }) };
        published = $published }
}

function Assert-DemoResources($Snapshot, $Recorded = $null,
    [string[]]$Services = @('backend', 'postgres', 'redis'), [bool]$StoragePresent = $true, [bool]$AllowStoppedBackend = $false) {
    $resources = $Snapshot.resources
    if (-not $receipt) {
        if ($resources.volumes.Count -or $resources.networks.Count -or $resources.containers.Count) { Stop-Demo 'DEMO_RESOURCE_UNOWNED' }
        return
    }
    $requiredVolumes = @($volumeNames | Where-Object { $StoragePresent -or $_ -cne 'eduagent-demo_storage_data' })
    if ($resources.volumes.Count -ne $requiredVolumes.Count -or $resources.networks.Count -ne 1 -or $resources.containers.Count -ne $Services.Count -or
        (@($resources.volumes.name | Sort-Object) -join ',') -cne (@($requiredVolumes | Sort-Object) -join ',') -or
        (@($resources.containers.service | Sort-Object) -join ',') -cne (@($Services | Sort-Object) -join ',')) { Stop-Demo 'DEMO_RESOURCE_UNOWNED' }
    foreach ($volume in $resources.volumes) {
        Assert-DemoLabels $volume.labels
        if (-not $volume.created_at -or -not $volume.mountpoint -or $volume.driver -ne 'local' -or
            $volume.scope -ne 'local' -or $volume.options.Count -or
            $volume.labels['com.docker.compose.project'] -cne $projectName -or
            $volume.labels['com.docker.compose.volume'] -cne $volume.name.Substring($projectName.Length + 1)) {
            Stop-Demo 'DEMO_RESOURCE_UNOWNED'
        }
    }
    $network = $resources.networks[0]
    Assert-DemoLabels $network.labels
    if ($network.name -cne $networkName -or $network.id -cnotmatch '^[a-f0-9]{64}$' -or
        -not $network.created_at -or $network.driver -ne 'bridge' -or $network.scope -ne 'local' -or $network.internal -or
        $network.labels['com.docker.compose.project'] -cne $projectName -or $network.labels['com.docker.compose.network'] -cne 'default') {
        Stop-Demo 'DEMO_RESOURCE_UNOWNED'
    }
    foreach ($container in $resources.containers) {
        Assert-DemoLabels $container.labels
        $service = $container.service
        $volume = @($resources.volumes | Where-Object { $_.name -ceq "${projectName}_$($serviceVolumes[$service])" })[0]
        $port = @($expectedPorts | Where-Object { $_.service -ceq $service })[0]
        $stoppedBackend = $AllowStoppedBackend -and $service -ceq 'backend' -and -not $container.running
        # Bootstrap may be up while the business-only healthcheck is not ready.
        $setupBackend = $service -ceq 'backend' -and $container.running -and $container.health -in @('starting', 'unhealthy')
        if ($container.id -cnotmatch '^[a-f0-9]{64}$' -or -not $container.created_at -or -not $container.image -or
            (-not $stoppedBackend -and (-not $container.running -or ($container.health -cne 'healthy' -and -not $setupBackend))) -or
            $container.labels['com.docker.compose.project'] -cne $projectName -or
            $container.labels['com.docker.compose.container-number'] -cne '1' -or $container.labels['com.docker.compose.oneoff'] -ine 'false' -or
            $container.mounts.Count -ne 1 -or $container.mounts[0].name -cne $volume.name -or
            $container.mounts[0].source -cne $volume.mountpoint -or $container.mounts[0].target -cne $mountTargets[$service] -or
            $container.mounts[0].type -cne 'volume' -or -not $container.mounts[0].rw -or
            $container.network_mode -notin @($networkName, $network.id) -or
            (-not $stoppedBackend -and $container.networks.Count -ne 1) -or $container.networks.Count -gt 1 -or
            ($container.networks.Count -eq 1 -and ($container.networks[0].name -cne $networkName -or
                ($container.networks[0].id -and $container.networks[0].id -cne $network.id) -or
                (-not $stoppedBackend -and $container.networks[0].endpoint_id -cnotmatch '^[a-f0-9]{64}$'))) -or
            $container.ports.Count -ne 1 -or $container.ports[0].target -cne "$($port.target)/tcp" -or
            $container.ports[0].published -cne [string]$port.published -or $container.ports[0].host_ip -cne $port.host_ip -or
            (-not $stoppedBackend -and @($Snapshot.published | Where-Object { $_.id -ceq $container.id -and $_.port -eq $port.published -and $_.host_ip -ceq $port.host_ip }).Count -ne 1)) {
            Stop-Demo 'DEMO_RESOURCE_UNOWNED'
        }
    }
    if ($Recorded -and -not (Test-DemoEqual (Get-DemoStableResources $resources) (Get-DemoStableResources $Recorded))) {
        Stop-Demo 'DEMO_RESOURCE_IDENTITY_MISMATCH'
    }
}

function Assert-DemoPorts($Snapshot) {
    $owned = @($Snapshot.resources.containers.id)
    foreach ($binding in $Snapshot.published) {
        if ($binding.port -in $expectedPorts.published -and $binding.id -notin $owned) { Stop-Demo 'DEMO_PORT_CONFLICT' }
    }
    try { $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop) }
    catch { Stop-Demo 'DEMO_PORT_OBSERVATION_FAILED' }
    foreach ($listener in $listeners) {
        if ($listener.LocalPort -in $expectedPorts.published -and
            -not @($Snapshot.published | Where-Object {
                $_.id -in $owned -and $_.port -eq $listener.LocalPort -and $_.host_ip -ceq $listener.LocalAddress
            }).Count) { Stop-Demo 'DEMO_PORT_CONFLICT' }
    }
}

function Get-DemoContainer([string]$NameOrId) {
    $rows = @(Read-DemoJson (Invoke-DemoDocker @('container', 'inspect', $NameOrId)))
    if ($rows.Count -ne 1 -or $rows[0].Id -cnotmatch '^[a-f0-9]{64}$') { Stop-Demo 'DEMO_OBSERVATION_INVALID' }
    return $rows[0]
}

function Get-DemoEnvironment($Values) {
    $result = @{}
    foreach ($entry in @($Values)) {
        $name, $value = ([string]$entry).Split('=', 2)
        if (-not $name -or $result.ContainsKey($name)) { Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH' }
        $result[$name] = [string]$value
    }
    return $result
}

function Assert-DemoContainerConfig($Container, [string]$Service, $Definition, $Snapshot, [string]$Phase = '') {
    Assert-DemoLabels $Container.Config.Labels
    $detail = $imageDetails[$(if ($Service -eq 'demo_step') { 'backend' } else { $Service })]
    if (-not $detail -or $Container.Image -cne $Definition.image -or $Container.Image -cne $detail.Id -or
        $Container.Config.Labels['com.docker.compose.project'] -cne $projectName -or
        $Container.Config.Labels['com.docker.compose.service'] -cne $Service -or
        $Container.Config.Labels['com.docker.compose.oneoff'] -ine 'false' -or
        $Container.HostConfig.Privileged -or [bool]$Container.HostConfig.ReadonlyRootfs -ne [bool]$Definition.read_only) {
        Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH'
    }
    $expectedRestart = if ($Definition.restart) { [string]$Definition.restart } else { 'no' }
    $actualRestart = [string]$Container.HostConfig.RestartPolicy.Name
    if (($actualRestart -cne $expectedRestart -and -not ($expectedRestart -ceq 'no' -and $actualRestart -ceq '')) -or
        ($Phase -and $Container.HostConfig.AutoRemove)) { Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH' }
    if ($Definition.healthcheck.test) {
        $expectedHealth = @($Definition.healthcheck.test | ForEach-Object { ([string]$_).Replace('$$', '$') })
        if (-not (Test-DemoEqual @($Container.Config.Healthcheck.Test) $expectedHealth)) {
            Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH'
        }
    }
    if ($Phase -and $Container.Config.Healthcheck.Test -and
        -not (Test-DemoEqual @($Container.Config.Healthcheck.Test) @('NONE'))) { Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH' }
    $expectedEnv = Get-DemoEnvironment $detail.Config.Env
    foreach ($key in $Definition.environment.Keys) {
        # `compose config --format json` canonically re-escapes literal dollars.
        $expectedEnv[$key] = ([string]$Definition.environment[$key]).Replace('$$', '$')
    }
    if (-not (Test-DemoEqual (Get-DemoEnvironment $Container.Config.Env) $expectedEnv)) {
        Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH'
    }
    $command = if ($Definition.Contains('command')) { $Definition.command } else { $detail.Config.Cmd }
    $entrypoint = if ($Definition.Contains('entrypoint')) { $Definition.entrypoint } else { $detail.Config.Entrypoint }
    $directory = if ($Definition.working_dir) { $Definition.working_dir } else { $detail.Config.WorkingDir }
    $user = if ($Definition.Contains('user')) { [string]$Definition.user } else { [string]$detail.Config.User }
    if (-not (Test-DemoEqual @($Container.Config.Cmd) @($command)) -or
        -not (Test-DemoEqual @($Container.Config.Entrypoint) @($entrypoint)) -or
        [string]$Container.Config.WorkingDir -cne [string]$directory -or [string]$Container.Config.User -cne $user) {
        Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH'
    }
    $mount = @($Container.Mounts)
    $logical = $Definition.volumes[0].source
    $volume = @($Snapshot.resources.volumes | Where-Object { $_.name -ceq "${projectName}_$logical" })
    $attachments = $Container.NetworkSettings.Networks
    if ($null -eq $attachments) { $attachments = @{} }
    $networks = @($attachments.Keys)
    $network = $Snapshot.resources.networks[0]
    if ($volume.Count -ne 1 -or $mount.Count -ne 1 -or $mount[0].Type -cne 'volume' -or
        $mount[0].Name -cne $volume[0].name -or $mount[0].Source -cne $volume[0].mountpoint -or
        $mount[0].Destination -cne $Definition.volumes[0].target -or
        [bool]$mount[0].RW -eq [bool]$Definition.volumes[0].read_only -or
        $Container.HostConfig.NetworkMode -notin @($networkName, $network.id) -or
        ($networks.Count -gt 0 -and ($networks.Count -ne 1 -or $networks[0] -cne $networkName)) -or
        ($attachments[$networkName].NetworkID -and $attachments[$networkName].NetworkID -cne $network.id)) {
        Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH'
    }
    if ($Phase) {
        if ($Container.Name -cne "/$($Definition.container_name)" -or
            $Container.Config.Labels['io.eduagent.phase'] -cne $Phase -or
            @($Container.HostConfig.PortBindings.Keys).Count -ne 0 -or $Container.State.Running -or
            $Container.State.Status -cne 'created') { Stop-Demo 'DEMO_CONTAINER_CONFIG_MISMATCH' }
    }
}

function Set-DemoImages {
    $frozenConfig.services.backend.image = "eduagent-demo-backend:$launchId"
    $null = Invoke-DemoCompose @('pull', '--policy', 'missing', 'postgres', 'redis')
    $null = Invoke-DemoCompose @('build', 'backend')
    foreach ($service in @('postgres', 'redis', 'backend')) {
        $name = [string]$frozenConfig.services[$service].image
        $rows = @(Read-DemoJson (Invoke-DemoDocker @('image', 'inspect', $name) 'DEMO_IMAGE_FAILED'))
        if ($rows.Count -ne 1 -or $rows[0].Id -cnotmatch '^sha256:[a-f0-9]{64}$' -or -not $rows[0].Config) {
            Stop-Demo 'DEMO_IMAGE_INVALID'
        }
        $imageDetails[$service] = $rows[0]
        $frozenConfig.services[$service].image = [string]$rows[0].Id
        $frozenConfig.services[$service].pull_policy = 'never'
        $null = $frozenConfig.services[$service].Remove('build')
        $null = $frozenConfig.services[$service].Remove('env_file')
        $null = $frozenConfig.services[$service].Remove('depends_on')
    }
    $roundtrip = Read-DemoJson (Invoke-DemoCompose @('config', '--format', 'json') 'DEMO_CONFIG_UNSTABLE')
    if (-not (Test-DemoEqual $roundtrip $frozenConfig)) { Stop-Demo 'DEMO_CONFIG_UNSTABLE' }
}

function Invoke-DemoOneOff([string]$Phase, [string]$Program, [string[]]$PermanentServices) {
    $model = Read-DemoJson ($frozenConfig | ConvertTo-Json -Depth 60 -Compress)
    $definition = Read-DemoJson ($frozenConfig.services.backend | ConvertTo-Json -Depth 40 -Compress)
    foreach ($field in @('ports', 'depends_on', 'restart', 'healthcheck')) { $null = $definition.Remove($field) }
    $definition.container_name = "eduagent-demo-step-$launchId-$Phase"
    $definition.labels['io.eduagent.phase'] = $Phase
    $definition.entrypoint = @('python', '-B', '-c')
    $definition.command = @($Program)
    $definition.read_only = $true
    $definition.volumes[0].read_only = $true
    $model.services.demo_step = $definition
    $null = Invoke-DemoModelCompose $model @('create', '--no-build', '--pull', 'never', 'demo_step')
    $container = Get-DemoContainer $definition.container_name
    $snapshot = Get-DemoInventory $container.Id
    Assert-DemoResources $snapshot $null $PermanentServices $true $true
    Assert-DemoContainerConfig $container 'demo_step' $definition $snapshot $Phase
    $receipt.active_step = [ordered]@{ id = [string]$container.Id; phase = $Phase; image = [string]$container.Image }
    Save-DemoReceipt
    $output = Invoke-DemoDocker @('container', 'start', '--attach', [string]$container.Id) 'DEMO_STEP_FAILED'
    $finished = Get-DemoContainer $container.Id
    if ($finished.Id -cne $container.Id -or $finished.State.Running -or
        $finished.State.Status -cne 'exited' -or $finished.State.ExitCode -ne 0) { Stop-Demo 'DEMO_STEP_FAILED' }
    $observation = Read-DemoJson $output 'DEMO_IDENTITY_FAILED'
    Assert-DemoHeads $observation ($Phase -ceq 'migration')
    $verifiedIdentity = Get-DemoIdentity $observation
    if ($receipt.identity -and -not (Test-DemoEqual $verifiedIdentity $receipt.identity)) { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    # Successful exact owned temporary containers only; never force, remove volumes, or widen a failure cleanup.
    $null = Invoke-DemoDocker @('container', 'rm', [string]$container.Id) 'DEMO_STEP_FAILED'
    $null = $receipt.Remove('active_step')
    Save-DemoReceipt
    return $observation
}

function Assert-DemoHeads($Observation, [bool]$RequireHead = $false) {
    if (@($Observation.target_heads).Count -ne 1 -or
        [string]$Observation.target_heads[0] -cnotmatch '^[A-Za-z0-9_]{1,128}$' -or
        $Observation.storage_compatible -isnot [bool] -or -not $Observation.storage_compatible) {
        Stop-Demo 'DEMO_IDENTITY_MISMATCH'
    }
    if ($RequireHead -and -not (Test-DemoEqual @($Observation.current_heads) @($Observation.target_heads))) {
        Stop-Demo 'DEMO_MIGRATION_HEAD_MISMATCH'
    }
}

function Get-DemoIdentity($Observation = $null) {
    $sql = "SELECT json_build_object('database', current_database(), 'user', current_user, 'schema', current_schema(), 'system_identifier', (SELECT system_identifier::text FROM pg_control_system()))::text;"
    $pg = Read-DemoJson (Invoke-DemoCompose @('exec', '-T', 'postgres', 'psql', '-U', 'eduagent_demo', '-d', 'eduagent_demo', '-A', '-t', '-c', $sql) 'DEMO_IDENTITY_FAILED')
    if ($pg.database -cne 'eduagent_demo' -or $pg.user -cne 'eduagent_demo' -or $pg.schema -cne 'public' -or
        $pg.system_identifier -cnotmatch '^[0-9]+$') { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    $redis = Invoke-DemoCompose @('exec', '-T', 'redis', 'redis-cli', 'INFO', 'server') 'DEMO_IDENTITY_FAILED'
    if ($redis -cnotmatch '(?m)^run_id:([a-f0-9]{40})\r?$') { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    $redisId = $Matches[1]
    $backend = $Observation
    if ($null -eq $backend) {
        $program = Get-DemoProgram 'identity'
        $backend = Read-DemoJson (Invoke-DemoCompose @('exec', '-T', 'backend', 'python', '-B', '-c', $program) 'DEMO_IDENTITY_FAILED')
    }
    Assert-DemoHeads $backend ($null -eq $Observation)
    foreach ($entry in @{ environment = 'demo'; runtime_kind = 'docker'; environment_id = $environmentId; creation_id = $creationId;
            data_root = '/app/storage'; storage_root = '/app/storage/storage'; real_root = '/app/storage'; real_storage = '/app/storage/storage' }.GetEnumerator()) {
        if ($backend[$entry.Key] -cne $entry.Value) { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    }
    foreach ($name in @('config', 'storage', 'cache', 'logs', 'temp', 'results', 'credentials')) {
        if ($backend.paths[$name] -cne "/app/storage/$name") { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    }
    if ($backend.device -cnotmatch '^[0-9]+$' -or $backend.inode -cnotmatch '^[1-9][0-9]*$' -or
        $backend.database.host -cne 'postgres' -or $backend.database.port -ne 5432 -or
        $backend.database.database -cne '/eduagent_demo' -or $backend.database.user -cne 'eduagent_demo' -or
        -not (Test-DemoEqual $backend.database.actual $pg) -or
        $backend.redis.host -cne 'redis' -or $backend.redis.port -ne 6379 -or $backend.redis.database -cne '/0' -or
        $backend.redis.run_id -cne $redisId) {
        Stop-Demo 'DEMO_IDENTITY_MISMATCH'
    }
    # Persist only an allowlisted identity, never raw inspect/config/exec output.
    return [ordered]@{ database = [ordered]@{ database = 'eduagent_demo'; user = 'eduagent_demo'; schema = 'public';
            system_identifier = [string]$pg.system_identifier }; redis_run_id = [string]$redisId;
        backend = [ordered]@{ environment_id = $environmentId; creation_id = $creationId; data_root = '/app/storage';
            storage_root = '/app/storage/storage'; device = [string]$backend.device; inode = [string]$backend.inode } }
}

function Get-DemoProgram([string]$Phase, $Expected = $null) {
    $observe = @'
def observe():
    import os
    import sys
    from pathlib import Path, PurePosixPath
    from urllib.parse import urlsplit
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text
    from redis import Redis
    from backend.app.core.environment import environment_path, resolve_environment_profile

    if 'backend.main' in sys.modules or 'backend.app.core.app' in sys.modules:
        raise ValueError('BUSINESS_APPLICATION_IMPORTED')
    database_raw, redis_raw = os.environ['DATABASE_URL'], os.environ['REDIS_URL']
    profile = resolve_environment_profile(storage_root=Path(os.environ['STORAGE_ROOT']))
    storage_root = profile.storage_root
    root = Path('/app/storage')
    paths = {name: str(environment_path(profile, name)) for name in
             ('config', 'storage', 'cache', 'logs', 'temp', 'results', 'credentials')}
    stat = root.stat()
    database = urlsplit(database_raw)
    redis_url = urlsplit(redis_raw)
    engine = create_engine(database_raw, connect_args={'connect_timeout': 5}, pool_pre_ping=True)
    client = Redis.from_url(redis_raw, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
    references = []
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                actual = dict(connection.execute(text(
                    "SELECT current_database() AS database, current_user AS user, "
                    "current_schema() AS schema, "
                    "(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier"
                )).mappings().one())
                current = sorted(MigrationContext.configure(connection).get_current_heads())
                scripts = ScriptDirectory.from_config(Config('alembic.ini'))
                target = sorted(scripts.get_heads())
                for revision in current:
                    if scripts.get_revision(revision) is None:
                        raise ValueError('UNKNOWN_MIGRATION_HEAD')
                inspector = inspect(connection)
                tables = set(inspector.get_table_names(schema=actual['schema']))
                if tables and not current:
                    raise ValueError('UNVERSIONED_SCHEMA')
                for table in ('documents', 'export_files', 'source_pages', 'question_assets'):
                    if table not in tables:
                        continue
                    columns = {column['name'] for column in inspector.get_columns(table, schema=actual['schema'])}
                    for column in ('storage_path', 'file_path'):
                        if column in columns:
                            references.extend(connection.execute(text(
                                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                            )).scalars())
                if 'extracted_questions' in tables and 'assets' in {
                    column['name'] for column in inspector.get_columns('extracted_questions', schema=actual['schema'])
                }:
                    for assets in connection.execute(text('SELECT assets FROM extracted_questions')).scalars():
                        for asset in assets or []:
                            if isinstance(asset, dict) and asset.get('storage_path'):
                                references.append(asset['storage_path'])
                available = False
                for locator in references:
                    if not isinstance(locator, str) or not locator or '\\' in locator or ':' in locator:
                        raise ValueError('STORAGE_REFERENCE_UNVERIFIED')
                    relative = PurePosixPath(locator)
                    parts = locator.split('/')[1:] if relative.is_absolute() else locator.split('/')
                    if any(part in {'.', '..', ''} for part in parts):
                        raise ValueError('STORAGE_REFERENCE_UNVERIFIED')
                    candidate = Path(locator) if relative.is_absolute() else storage_root / locator
                    if candidate != candidate.resolve() or not candidate.is_relative_to(storage_root):
                        raise ValueError('STORAGE_REFERENCE_OUTSIDE_ENVIRONMENT')
                    available = available or candidate.is_file()
        if not client.ping():
            raise ValueError('REDIS_NOT_READY')
        redis_id = client.info('server').get('run_id')
    finally:
        client.close()
        engine.dispose()
    return {
        'environment': profile.environment.value, 'runtime_kind': profile.runtime_kind.value,
        'environment_id': str(profile.environment_id), 'creation_id': os.environ.get('EDUAGENT_CREATION_ID'),
        'data_root': os.environ.get('EDUAGENT_DATA_ROOT'), 'storage_root': str(storage_root),
        'real_root': str(root.resolve()), 'real_storage': str(storage_root.resolve()),
        'device': str(stat.st_dev), 'inode': str(stat.st_ino), 'paths': paths,
        'database': {'host': database.hostname, 'port': database.port, 'database': database.path,
                     'user': database.username, 'actual': actual},
        'redis': {'host': redis_url.hostname, 'port': redis_url.port, 'database': redis_url.path, 'run_id': redis_id},
        'current_heads': current, 'target_heads': target, 'storage_compatible': not references or available,
    }

def identity_of(value):
    return {
        'database': value['database']['actual'], 'redis_run_id': value['redis']['run_id'],
        'backend': {name: value[name] for name in
                    ('environment_id', 'creation_id', 'data_root', 'storage_root', 'device', 'inode')},
    }
'@
    if ($Phase -ceq 'identity') {
        return $observe + @'

# T202_DEMO_IDENTITY_PROBE
import json
try:
    print(json.dumps(observe()))
except Exception as exc:
    print(json.dumps({'error': 'DEMO_PROBE_FAILED', 'type': type(exc).__name__}))
    raise SystemExit(1)
'@
    }
    $expectedJson = ConvertTo-Json -InputObject ($Expected | ConvertTo-Json -Depth 20 -Compress) -Compress
    $migration = @'

# T202_DEMO_MIGRATION
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from backend.app.services.settings.database_initialization import run_alembic_upgrade
try:
    expected = json.loads(__EXPECTED_JSON_LITERAL__)
    before = observe()
    if identity_of(before) != expected['identity'] or before['target_heads'] != expected['target_heads'] or not before['storage_compatible']:
        raise ValueError('IDENTITY_CHANGED_BEFORE_MIGRATION')
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        run_alembic_upgrade(os.environ['DATABASE_URL'])  # upgrade head with an explicit connection
    after = observe()
    if identity_of(after) != expected['identity'] or after['current_heads'] != expected['target_heads'] or after['target_heads'] != expected['target_heads']:
        raise ValueError('MIGRATION_HEAD_MISMATCH')
    after['migration_completed'] = True
    print(json.dumps(after))
except Exception as exc:
    print(json.dumps({'error': 'DEMO_MIGRATION_FAILED', 'type': type(exc).__name__}))
    raise SystemExit(1)
'@
    return $observe + $migration.Replace('__EXPECTED_JSON_LITERAL__', $expectedJson)
}

function Wait-DemoBootstrap {
    $program = @'
# T232_DEMO_BOOTSTRAP_STATUS
import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

def status(path):
    try:
        with urlopen('http://127.0.0.1:8000' + path, timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, {}
    except (URLError, OSError, ValueError):
        return 0, {}

setup_code, setup = status('/setup-ready')
business_code, business = status('/ready')
print(json.dumps({
    'setup_http': setup_code,
    'setup_ready': setup_code == 200 and setup.get('status') == 'ok'
        and setup.get('setup', {}).get('static_resources') == 'available',
    'business_ready': business_code == 200 and business.get('status') == 'ok',
}))
'@
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($WaitTimeoutSeconds)
    do {
        $status = Read-DemoJson (Invoke-DemoCompose @('exec', '-T', 'backend', 'python', '-B', '-c', $program))
        if ($status.setup_ready -eq $true) { return $status }
        if ($status.setup_http -ne 0) { Stop-Demo 'DEMO_SETUP_NOT_READY' }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    Stop-Demo 'DEMO_SETUP_NOT_READY'
}

function Save-DemoReceipt {
    Assert-DemoLocalPath $receiptPath
    $receipt | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $receiptPath -Encoding utf8
}

try {
    if (-not $inherited.LOCALAPPDATA) { Stop-Demo 'DEMO_LOCAL_CONFIG_REQUIRED' }
    $configRoot = [IO.Path]::GetFullPath((Join-Path $inherited.LOCALAPPDATA 'EduAgent/demo/config'))
    $receiptPath = Join-Path $configRoot 'demo-creation.json'
    $overridePath = Join-Path $configRoot 'demo-compose.override.json'
    Assert-DemoLocalPath $configRoot
    Assert-DemoLocalPath $receiptPath
    Assert-DemoLocalPath $overridePath
    if (-not $PSBoundParameters.ContainsKey('EnvFile')) { $EnvFile = Join-Path $configRoot 'demo.env' }
    if ([string]::IsNullOrWhiteSpace($EnvFile) -or -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        Stop-Demo 'DEMO_CONFIG_MISSING'
    }
    $selectedEnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
    if (-not (Test-Path -LiteralPath $baseCompose -PathType Leaf)) { Stop-Demo 'DEMO_CONFIG_INVALID' }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Stop-Demo 'DEMO_DOCKER_MISSING' }
    New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
    $lockPath = Join-Path $configRoot 'demo-launch.lock'
    Assert-DemoLocalPath $lockPath
    try { $launchLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { Stop-Demo 'DEMO_LAUNCH_IN_PROGRESS' }
    # Compose can interpolate arbitrary keys inside the explicitly chosen env file.
    # Preserve only OS/tool transport variables; restore the entire snapshot in finally.
    $systemVariables = '^(PATH|PATHEXT|SystemRoot|SystemDrive|WINDIR|COMSPEC|TEMP|TMP|USERPROFILE|HOME|HOMEDRIVE|HOMEPATH|APPDATA|LOCALAPPDATA|ALLUSERSPROFILE|PUBLIC|USERNAME|USERDOMAIN|USERDNSDOMAIN|ProgramData|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432|CommonProgramFiles|CommonProgramFiles\(x86\)|CommonProgramW6432|OS|PROCESSOR_.+|NUMBER_OF_PROCESSORS|PSModulePath|DOCKER_HOST|DOCKER_CONTEXT|DOCKER_CONFIG|DOCKER_TLS_VERIFY|DOCKER_CERT_PATH|DOCKER_API_VERSION)$'
    foreach ($name in @($inherited.Keys)) {
        if ($name -notmatch $systemVariables) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    }
    $env:EDUAGENT_ENV_FILE = $selectedEnvFile
    $composeArgs = @('compose', '--project-name', $projectName, '--file', $baseCompose, '--env-file', $selectedEnvFile)
    $environmentId = $null
    $creationId = $null
    if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
        $receipt = Read-DemoJson (Get-Content -LiteralPath $receiptPath -Raw) 'DEMO_RECEIPT_INVALID'
        if ($receipt.state -cne 'identity_verified' -or $receipt.active_step) { Stop-Demo 'DEMO_RECEIPT_INCOMPLETE' }
        if ($receipt.schema_version -ne 2 -or $receipt.environment -cne 'demo' -or $receipt.project -cne $projectName -or
            $receipt.data_root -cne '/app/storage' -or $receipt.storage_root -cne '/app/storage/storage' -or
            $receipt.config_root -cne $configRoot -or $receipt.compose_file -cne $baseCompose -or
            $receipt.environment_id -cnotmatch '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$' -or
            $receipt.creation_id -cnotmatch '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$' -or
            [guid]$receipt.environment_id -eq [guid]::Empty -or [guid]$receipt.creation_id -eq [guid]::Empty -or
            -not $receipt.resources -or -not $receipt.identity -or -not $receipt.created_at -or -not $receipt.images) { Stop-Demo 'DEMO_RECEIPT_INVALID' }
        foreach ($service in @('postgres', 'redis', 'backend')) {
            if ($receipt.images[$service] -cnotmatch '^sha256:[a-f0-9]{64}$' -or
                @($receipt.resources.containers | Where-Object { $_.service -ceq $service -and $_.image -ceq $receipt.images[$service] }).Count -ne 1) {
                Stop-Demo 'DEMO_RECEIPT_INVALID'
            }
        }
        $environmentId = [string]$receipt.environment_id
        $creationId = [string]$receipt.creation_id
        $recordedIdentity = $receipt.identity
        if ($recordedIdentity.database.database -cne 'eduagent_demo' -or $recordedIdentity.database.user -cne 'eduagent_demo' -or
            $recordedIdentity.database.schema -cne 'public' -or $recordedIdentity.database.system_identifier -cnotmatch '^[0-9]+$' -or
            $recordedIdentity.redis_run_id -cnotmatch '^[a-f0-9]{40}$' -or
            $recordedIdentity.backend.environment_id -cne $environmentId -or $recordedIdentity.backend.creation_id -cne $creationId -or
            $recordedIdentity.backend.data_root -cne '/app/storage' -or $recordedIdentity.backend.storage_root -cne '/app/storage/storage' -or
            $recordedIdentity.backend.device -cnotmatch '^[0-9]+$' -or $recordedIdentity.backend.inode -cnotmatch '^[1-9][0-9]*$' -or
            @($receipt.schema_heads).Count -ne 1 -or [string]$receipt.schema_heads[0] -cnotmatch '^[A-Za-z0-9_]{1,128}$') {
            Stop-Demo 'DEMO_RECEIPT_INVALID'
        }
    } elseif (Test-Path -LiteralPath $overridePath) { Stop-Demo 'DEMO_RECEIPT_INVALID' }

    $config = Read-DemoJson (Invoke-DemoCompose @('config', '--format', 'json') 'DEMO_CONFIG_INVALID') 'DEMO_CONFIG_INVALID'
    $expectedPorts = Assert-DemoConfig $config $false
    $stage = 'preview'
    $snapshot = Get-DemoInventory
    Assert-DemoResources $snapshot $(if ($receipt) { $receipt.resources }) @('backend', 'postgres', 'redis') $true $true
    Assert-DemoPorts $snapshot
    Write-Host "DEMO_PREVIEW: environment=demo; project=$projectName; data=/app/storage; storage=/app/storage/storage; ports=$($expectedPorts.published -join ','); unknown resources are never adopted."

    if (-not $receipt) {
        $environmentId = [guid]::NewGuid().ToString()
        $creationId = [guid]::NewGuid().ToString()
    }
    $labels = [ordered]@{ $labelOwner = 'run_demo.ps1'; $labelEnvironment = 'demo'; $labelId = $environmentId;
        $labelCreation = $creationId; $labelRoot = '/app/storage' }
    $override = [ordered]@{ services = [ordered]@{
        postgres = @{ labels = $labels; environment = @{ POSTGRES_DB = 'eduagent_demo'; POSTGRES_USER = 'eduagent_demo' } };
        redis = @{ labels = $labels };
        backend = @{ labels = $labels; environment = @{ EDUAGENT_ENVIRONMENT = 'demo'; EDUAGENT_RUNTIME_KIND = 'docker';
            EDUAGENT_ENVIRONMENT_ID = $environmentId; EDUAGENT_CREATION_ID = $creationId; EDUAGENT_DATA_ROOT = '/app/storage';
            STORAGE_ROOT = '/app/storage/storage'; DATABASE_URL = 'postgresql+psycopg://eduagent_demo@postgres:5432/eduagent_demo' } }
        }; volumes = [ordered]@{ postgres_data = @{ labels = $labels }; redis_data = @{ labels = $labels }; storage_data = @{ labels = $labels } };
        networks = @{ default = @{ labels = $labels } } }
    New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
    Assert-DemoLocalPath $overridePath
    $override | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $overridePath -Encoding utf8
    $composeArgs += @('--file', $overridePath)
    $stage = 'resolved-preview'
    $resolved = Read-DemoJson (Invoke-DemoCompose @('config', '--format', 'json') 'DEMO_CONFIG_INVALID') 'DEMO_CONFIG_INVALID'
    $resolvedPorts = Assert-DemoConfig $resolved $true
    if (-not (Test-DemoEqual $expectedPorts $resolvedPorts)) { Stop-Demo 'DEMO_CONFIG_INVALID' }
    # Repeat observations before any engine mutation; unknown resources are not adopted.
    $snapshot = Get-DemoInventory
    Assert-DemoResources $snapshot $(if ($receipt) { $receipt.resources }) @('backend', 'postgres', 'redis') $true $true
    Assert-DemoPorts $snapshot
    $reusing = $null -ne $receipt
    $existingBackend = if ($reusing) { @($snapshot.resources.containers | Where-Object { $_.service -ceq 'backend' })[0] } else { $null }
    if (-not $reusing) {
        $receipt = [ordered]@{ schema_version = 2; environment = 'demo'; project = $projectName; environment_id = $environmentId;
            creation_id = $creationId; created_at = [DateTimeOffset]::UtcNow.ToString('o'); config_root = $configRoot;
            compose_file = $baseCompose; data_root = '/app/storage'; storage_root = '/app/storage/storage'; state = 'creating' }
        Save-DemoReceipt
    }
    $canRecordFailure = $true
    $frozenConfig = Read-DemoJson ($resolved | ConvertTo-Json -Depth 60 -Compress)
    $stage = 'images'
    Set-DemoImages
    $receipt.planned_images = [ordered]@{ postgres = $imageDetails.postgres.Id; redis = $imageDetails.redis.Id; backend = $imageDetails.backend.Id }
    Save-DemoReceipt
    $upArgs = @('up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--no-recreate',
        '--wait', '--wait-timeout', [string]$WaitTimeoutSeconds)
    if ($reusing) {
        $stage = 'existing-configuration'
        foreach ($service in @('postgres', 'redis')) {
            $id = @($snapshot.resources.containers | Where-Object { $_.service -ceq $service })[0].id
            try { Assert-DemoContainerConfig (Get-DemoContainer $id) $service $frozenConfig.services[$service] $snapshot }
            catch { if ($_.Exception.Message -ceq 'DEMO_CONTAINER_CONFIG_MISMATCH') { Stop-Demo 'DEMO_CONTROLLED_STOP_REQUIRED' }; throw }
        }
        if ($existingBackend.running) {
            try { Assert-DemoContainerConfig (Get-DemoContainer $existingBackend.id) 'backend' $frozenConfig.services.backend $snapshot }
            catch { if ($_.Exception.Message -ceq 'DEMO_CONTAINER_CONFIG_MISMATCH') { Stop-Demo 'DEMO_CONTROLLED_STOP_REQUIRED' }; throw }
        }
        $permanentServices = @('backend', 'postgres', 'redis')
    } else {
        $stage = 'foundation-health'
        $null = Invoke-DemoCompose ($upArgs + @('postgres', 'redis'))
        $snapshot = Get-DemoInventory
        Assert-DemoResources $snapshot $null @('postgres', 'redis') $false
        Assert-DemoPorts $snapshot
        foreach ($service in @('postgres', 'redis')) {
            $id = @($snapshot.resources.containers | Where-Object { $_.service -ceq $service })[0].id
            Assert-DemoContainerConfig (Get-DemoContainer $id) $service $frozenConfig.services[$service] $snapshot
        }
        $permanentServices = @('postgres', 'redis')
    }
    $stage = 'identity-probe'
    $observation = Invoke-DemoOneOff 'identity' (Get-DemoProgram 'identity') $permanentServices
    $identity = Get-DemoIdentity $observation
    if ($reusing -and -not (Test-DemoEqual $identity $receipt.identity)) { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    if ($reusing -and $existingBackend.running -and
        -not (Test-DemoEqual @($observation.current_heads) @($observation.target_heads))) {
        Stop-Demo 'DEMO_CONTROLLED_STOP_REQUIRED'
    }
    $receipt.identity = $identity
    if (-not $reusing -or -not $existingBackend.running) {
        $stage = 'migration'
        $snapshot = Get-DemoInventory
        Assert-DemoResources $snapshot $null $permanentServices $true $true
        if (@($snapshot.resources.containers | Where-Object { $_.service -ceq 'backend' -and $_.running }).Count) {
            Stop-Demo 'DEMO_CONTROLLED_STOP_REQUIRED'
        }
        $receipt.state = 'creating'
        Save-DemoReceipt
        $expected = [ordered]@{ identity = $identity; target_heads = @($observation.target_heads) }
        $migrated = Invoke-DemoOneOff 'migration' (Get-DemoProgram 'migration' $expected) $permanentServices
        Assert-DemoHeads $migrated $true
        if ($existingBackend) {
            # Only an explicitly stopped, previously recorded container may be replaced; never stop it here.
            $stopped = Get-DemoContainer $existingBackend.id
            if ($stopped.Id -cne $existingBackend.id -or $stopped.State.Running) { Stop-Demo 'DEMO_CONTROLLED_STOP_REQUIRED' }
            Assert-DemoLabels $stopped.Config.Labels
            $null = Invoke-DemoDocker @('container', 'rm', [string]$stopped.Id) 'DEMO_STEP_FAILED'
            $receipt.previous_backend_id = [string]$stopped.Id
        }
        $stage = 'backend-create'
        $snapshot = Get-DemoInventory
        Assert-DemoResources $snapshot $null @('postgres', 'redis') $true
        Assert-DemoPorts $snapshot
        $null = Invoke-DemoCompose @('create', '--no-build', '--pull', 'never', 'backend')
        $created = Get-DemoInventory
        Assert-DemoResources $created $null @('backend', 'postgres', 'redis') $true $true
        $backendId = @($created.resources.containers | Where-Object { $_.service -ceq 'backend' })[0].id
        Assert-DemoContainerConfig (Get-DemoContainer $backendId) 'backend' $frozenConfig.services.backend $created
        Assert-DemoPorts $created
        $stage = 'backend-health'
        $null = Invoke-DemoCompose (@('up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--no-recreate', 'backend'))
        $snapshot = Get-DemoInventory
        Assert-DemoResources $snapshot $created.resources
        Assert-DemoContainerConfig (Get-DemoContainer $backendId) 'backend' $frozenConfig.services.backend $snapshot
        $bootstrapStatus = Wait-DemoBootstrap
    } else {
        # A running, unchanged, already migrated instance is not restarted or migrated again.
        Assert-DemoHeads $observation $true
        $snapshot = Get-DemoInventory
        Assert-DemoResources $snapshot $receipt.resources
        $bootstrapStatus = Wait-DemoBootstrap
    }
    $stage = 'backend-identity'
    if (-not (Test-DemoEqual (Get-DemoIdentity) $identity)) { Stop-Demo 'DEMO_IDENTITY_MISMATCH' }
    $receipt.resources = $snapshot.resources
    $receipt.images = $receipt.planned_images
    $null = $receipt.Remove('planned_images')
    $receipt.schema_heads = @($observation.target_heads)
    $receipt.state = 'identity_verified'
    $receipt.last_stage = if ($bootstrapStatus.business_ready) { 'backend_healthy' } else { 'setup_ready' }
    $receipt.last_error_code = $null
    Save-DemoReceipt
    if (-not $bootstrapStatus.business_ready) {
        $port = @($expectedPorts | Where-Object { $_.service -eq 'backend' })[0].published
        Write-Host "Demo Setup running: http://127.0.0.1:$port/settings; business readiness remains unavailable; Seed was not run."
        Write-Host 'Use the local container management command to authorize Settings; configure external Secrets, then restart the owned Backend after activation.'
        return
    }
    $stage = 'seed'
    $null = Invoke-DemoCompose @('exec', '-T', 'backend', 'python', '-m', 'scripts.demo_seed')
    $receipt.last_stage = 'seed_completed'
    Save-DemoReceipt

    $port = @($expectedPorts | Where-Object { $_.service -eq 'backend' })[0].published
    Write-Host "Demo UI running: http://127.0.0.1:$port/gradio"
    Write-Host 'Accounts: dev_teacher / dev_student / dev_admin (use DEV_MODE quick-login buttons; no password).'
    Write-Host 'Exam availability follows seed JSON: awaiting_teacher_review requires teacher review, validation and approval, then rerun; awaiting_exam_scoring requires preparing and confirming each exam rubric, then rerun; ready means the demo exam is published.'
    Write-Host 'Local demonstration only. Disable DEV_MODE and remove demo accounts before production.'
} catch {
    if ($_.Exception.Message -cmatch '^DEMO_[A-Z_]+$') { $failureCode = $_.Exception.Message }
    $errorType = $_.Exception.GetType().Name
    if ($canRecordFailure) {
        try { $receipt.last_stage = $stage; $receipt.last_error_code = $failureCode; $receipt.last_error_type = $errorType; Save-DemoReceipt }
        catch { } # Never replace the original controlled failure with a receipt-write error.
    }
    Write-Host "Demo startup failed: $failureCode; stage=$stage; type=$errorType. Residual resources are retained; no later steps were run." -ForegroundColor Red
    if ($failureCode -eq 'DEMO_CONTROLLED_STOP_REQUIRED') {
        Write-Host '现有运行实例需要升级或配置不同；请先由操作者受控停止已核验的业务 Backend，再重新运行。脚本不会自动停机或在运行中迁移。'
    }
    if ($failureCode -eq 'DEMO_CONFIG_MISSING') {
        Write-Host '请显式提供 -EnvFile，或自行填写 %LOCALAPPDATA%/EduAgent/demo/config/demo.env；不会读取仓库/CWD配置或复制任何Key。'
    }
    exit 1
} finally {
    if ($launchLock) { $launchLock.Dispose() }
    foreach ($item in @(Get-ChildItem Env:)) {
        if (-not $inherited.ContainsKey($item.Name)) { [Environment]::SetEnvironmentVariable($item.Name, $null, 'Process') }
    }
    foreach ($entry in $inherited.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
}
