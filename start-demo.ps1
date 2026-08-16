# Start the Financial Operation Core response-loss demo.
#
#   .\start-demo.ps1
#
# Touches only demo-owned resources: the fincore-demo-pg container on port
# 55433, the demo venv, and the demo schema. The flagship checkout is read-only.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$Flagship = if ($env:FINCORE_FLAGSHIP_PATH) { $env:FINCORE_FLAGSHIP_PATH }
            else { Join-Path (Split-Path -Parent $root) 'financial-operation-core-public' }

$Container = 'fincore-demo-pg'
$Port = 55433

# --- 1. flagship checkout ----------------------------------------------------
Write-Host '[1/5] Financial Operation Core checkout' -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $Flagship 'src\fincore\engine.py'))) {
    Write-Host "  NOT FOUND: $Flagship" -ForegroundColor Red
    Write-Host '  Set FINCORE_FLAGSHIP_PATH to the read-only flagship checkout and retry.'
    exit 1
}
Write-Host "  ok  $Flagship (read-only)" -ForegroundColor Green

# --- 2. demo database --------------------------------------------------------
Write-Host '[2/5] demo database' -ForegroundColor Cyan
$existing = docker ps -a --filter "name=^/$Container$" --format '{{.Names}} {{.State}}'
if (-not $existing) {
    Write-Host "  creating $Container on 127.0.0.1:$Port"
    docker run -d --name $Container `
        -e POSTGRES_USER=fincore -e POSTGRES_PASSWORD=fincore -e POSTGRES_DB=fincore_demo `
        -p "127.0.0.1:${Port}:5432" postgres:16-alpine | Out-Null
} elseif ($existing -notmatch 'running') {
    Write-Host "  starting $Container"
    docker start $Container | Out-Null
} else {
    Write-Host "  $Container already running"
}

Write-Host '  waiting for postgres'
$ready = $false
foreach ($i in 1..30) {
    docker exec $Container pg_isready -U fincore -d fincore_demo *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Milliseconds 700
}
if (-not $ready) { Write-Host '  postgres did not become ready' -ForegroundColor Red; exit 1 }
Write-Host '  ok' -ForegroundColor Green

# --- 3. python environment ---------------------------------------------------
Write-Host '[3/5] python environment' -ForegroundColor Cyan
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host '  creating .venv'
    python -m venv .venv
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -r requirements.txt
} else {
    Write-Host '  ok  .venv' -ForegroundColor Green
}

# --- 4. migrate (flagship migrations, demo schema) ---------------------------
Write-Host '[4/5] schema (flagship alembic migrations)' -ForegroundColor Cyan
$env:PYTHONDONTWRITEBYTECODE = '1'
& $py -c "import sys; sys.path.insert(0,'.'); from app.db import migrate; migrate(); print('  ok  schema demo at head')"
if ($LASTEXITCODE -ne 0) { exit 1 }

# --- 5. serve ----------------------------------------------------------------
Write-Host '[5/5] starting FastAPI' -ForegroundColor Cyan
Write-Host ''
Write-Host 'Demo:' -ForegroundColor Green
Write-Host '  http://127.0.0.1:8000/?recording=1' -ForegroundColor Green
Write-Host ''
Write-Host 'Stop with Ctrl+C, then .\stop-demo.ps1 to stop the demo database.'
Write-Host ''

& $py -m app.server
