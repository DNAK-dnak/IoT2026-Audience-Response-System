<#
  stop.ps1 - close the helper windows opened by start.ps1 and stop the containers.
      .\stop.ps1            # data is kept
      .\stop.ps1 -KeepDocker  # only close the helper windows
#>
param([switch]$KeepDocker)

Set-Location -Path $PSScriptRoot
$pidFile = Join-Path $PSScriptRoot ".run_pids.txt"

if (Test-Path $pidFile) {
    foreach ($id in Get-Content $pidFile) {
        if ($id) {
            # /T also stops the python / docker process running inside that window
            taskkill /PID $id /T /F *> $null
        }
    }
    Remove-Item $pidFile
    Write-Host "helper windows closed" -ForegroundColor Green
} else {
    Write-Host "no helper windows recorded" -ForegroundColor Yellow
}

if (-not $KeepDocker -and (Get-Command docker -ErrorAction SilentlyContinue) -and (Test-Path "docker-compose.yml")) {
    docker compose stop
    Write-Host "containers stopped (data kept; 'docker compose down -v' would delete it)" -ForegroundColor Green
}
