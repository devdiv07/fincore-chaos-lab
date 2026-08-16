# Stop demo-owned resources ONLY.
#
#   .\stop-demo.ps1            stop the demo database container
#   .\stop-demo.ps1 -Remove    stop and delete it (drops the demo data)
#
# This never touches `fincore-pg` (the flagship's test database) or any other
# container on this machine.

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$Container = 'fincore-demo-pg'

$state = docker ps -a --filter "name=^/$Container$" --format '{{.State}}'
if (-not $state) {
    Write-Host "$Container does not exist; nothing to stop."
    exit 0
}

if ($state -match 'running') {
    docker stop $Container | Out-Null
    Write-Host "stopped $Container" -ForegroundColor Green
} else {
    Write-Host "$Container already stopped"
}

if ($Remove) {
    docker rm $Container | Out-Null
    Write-Host "removed $Container" -ForegroundColor Green
}

Write-Host 'The FastAPI process (if running) stops with Ctrl+C in its own window.'
