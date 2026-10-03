<#
.SYNOPSIS
Start the three-container Docker Demo, migrate, and seed through domain services.
.DESCRIPTION
Run from any directory: ./scripts/run_demo.ps1 -EnvFile .env
Requires Docker Compose with --wait support and an explicit DEV_MODE=true config.
P4.1 cloud Embedding configuration is retained. Initial ingestion calls that provider.
Existing data is retained; this script never resets volumes or user submissions.
#>
[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\.env'),
    [ValidateRange(30, 1800)]
    [int]$WaitTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousEnvFile = $env:EDUAGENT_ENV_FILE

function Invoke-DemoCompose {
    param([string[]]$Arguments, [string]$Stage)
    & docker @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "DEMO_STEP_FAILED: $Stage (exit $LASTEXITCODE). No later steps were run."
    }
}

try {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw 'DEMO_CONFIG_MISSING: create an env file using .env.example and fill its values.'
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'DEMO_DOCKER_MISSING: install and start Docker Desktop with Compose.'
    }
    $env:EDUAGENT_ENV_FILE = (Resolve-Path -LiteralPath $EnvFile).Path
    $composeArgs = @('compose', '--file', (Join-Path $projectRoot 'docker-compose.yml'),
                    '--env-file', $env:EDUAGENT_ENV_FILE)
    # Resolve the same environment Compose will use; never print config JSON or credentials.
    $configOutput = & docker @composeArgs config --format json 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'DEMO_CONFIG_INVALID: Docker Compose could not resolve the env/config files.'
    }
    $config = ($configOutput -join "`n") | ConvertFrom-Json
    $environment = $config.services.backend.environment
    foreach ($field in @('JWT_SECRET_KEY', 'LLM_PROVIDER', 'DEEPSEEK_API_KEY',
                         'DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL', 'RERANK_PROVIDER',
                         'CONFIDENCE_THRESHOLD', 'EMBEDDING_MODEL', 'EMBEDDING_API_KEY')) {
        $value = [string]$environment.$field
        if ([string]::IsNullOrWhiteSpace($value) -or $value -match '<[^>]+>' -or
            $value -match '^(change-me|replace-me|placeholder)$') {
            throw "DEMO_CONFIG_MISSING: $field (Embedding uses DEMO_EMBEDDING_* in the env file)."
        }
    }
    if (([string]$environment.JWT_SECRET_KEY).Length -lt 32) {
        throw 'DEMO_CONFIG_INVALID: JWT_SECRET_KEY needs at least 32 characters.'
    }
    if (([string]$environment.DEV_MODE).ToLowerInvariant() -notin @('1', 'true', 't', 'yes', 'y', 'on')) {
        throw 'DEMO_DEV_MODE_REQUIRED: set DEV_MODE=true in your local Demo env file.'
    }

    # /ready checks PostgreSQL/Redis connectivity, so a fresh database can be healthy
    # before migrations. Never report Demo success based on container health alone.
    Invoke-DemoCompose -Arguments @('up', '-d', '--build', '--wait', '--wait-timeout',
                                  [string]$WaitTimeoutSeconds) -Stage 'build/start/health'
    Invoke-DemoCompose -Arguments @('exec', '-T', 'backend', 'alembic', 'upgrade', 'head') -Stage 'migration'
    Invoke-DemoCompose -Arguments @('exec', '-T', 'backend', 'python', '-m', 'scripts.demo_seed') -Stage 'seed'

    $port = ($config.services.backend.ports | Where-Object { $_.target -eq 8000 }).published
    Write-Host "Demo UI running: http://127.0.0.1:$port/gradio"
    Write-Host 'Accounts: dev_teacher / dev_student / dev_admin (use DEV_MODE quick-login buttons; no password).'
    Write-Host 'Exam availability follows seed JSON: awaiting_teacher_review requires teacher review, validation and approval, then rerun; ready means the demo exam is published.'
    Write-Host 'Local demonstration only. Disable DEV_MODE and remove demo accounts before production.'
} catch {
    Write-Host "Demo startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    $env:EDUAGENT_ENV_FILE = $previousEnvFile
}
