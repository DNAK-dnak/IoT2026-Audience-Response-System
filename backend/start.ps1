<#
  start.ps1 - start everything for a test or demo, in one command.

  ON THE MAIN LAPTOP (runs the backend), from the backend folder:
      .\start.ps1                       # backend + teacher page + 50 fake devices
      .\start.ps1 -Pico                 # + BLE bridge for the real PicoRemote clickers
      .\start.ps1 -Phone                # + phone bridge on THIS laptop (needs Bluetooth that can advertise)
      .\start.ps1 -Pico -Fake 0         # only real clickers, no fake devices
      .\start.ps1 -Test all             # fake devices run a test scenario (report at session end)
      .\start.ps1 -Reset                # DELETE all data, reload tables + demo data first
      .\start.ps1 -Watch                # + windows with the backend log and all MQTT messages
      .\start.ps1 -OpenFirewall         # once, as administrator: let phones / a 2nd laptop reach this one

  ON A SECOND LAPTOP (only Bluetooth there; the backend stays on the main laptop):
      .\start.ps1 -Server 192.168.1.23 -Phone     # phone bridge here, connected to the main laptop
      .\start.ps1 -Server 192.168.1.23 -Pico      # Pico bridge here
    Use this when the main laptop's Bluetooth cannot advertise (phone mode: "ABORTED").
    No Docker needed on the second laptop.

  Stop everything:  .\stop.ps1
#>
param(
    [switch]$Pico,                 # real clickers over Bluetooth (ble_bridge.py)
    [switch]$Phone,                # phones over Bluetooth (ble_bridge.py --phone)
    [string]$Server = "",          # IP of the main laptop -> this laptop only runs bridges
    [int]$Fake = 50,               # number of fake devices (0 = none)
    [string]$Test = "normal",      # fake devices scenario: normal burst lossy wrong_mssv reboot late dropout spam random all
    [string]$Room = "room1",
    [int]$ClassId = 1,             # class list used by the fake devices
    [switch]$Reset,                # reload schema.sql + seed.sql (DELETES all data)
    [switch]$Watch,                # extra windows: backend log + MQTT traffic
    [switch]$OpenFirewall,         # allow ports 8000, 9001, 1884 from the local network (run as administrator)
    [switch]$NoBrowser
)

# "Continue": in Windows PowerShell 5.1 "Stop" would abort on any warning a program prints;
# every step checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$pidFile = Join-Path $PSScriptRoot ".run_pids.txt"
$remote = $Server -ne ""
$hostAddr = if ($remote) { $Server } else { "127.0.0.1" }
$ports = "--broker $hostAddr --port 1884 --api http://${hostAddr}:8000"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "   OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   !!  $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "   XX  $text" -ForegroundColor Red; exit 1 }

# laptop IP for phones / a second laptop: prefer Wi-Fi, skip loopback and Docker/WSL virtual adapters
function Get-LanIp {
    (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
     Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and
                    $_.InterfaceAlias -notlike "vEthernet*" -and $_.InterfaceAlias -notlike "*Loopback*" } |
     Sort-Object { if ($_.InterfaceAlias -like "*Wi-Fi*" -or $_.InterfaceAlias -like "*WLAN*") { 0 } else { 1 } } |
     Select-Object -First 1).IPAddress
}

# ------------------------------------------------------------------ firewall (main laptop)
if ($OpenFirewall) {
    Step "Opening ports 8000 (web/API), 9001 (phones Wi-Fi), 1884 (MQTT) for the local network"
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
             [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { Fail "run PowerShell as administrator for -OpenFirewall (right-click > Run as administrator)" }
    Get-NetFirewallRule -DisplayName "ARS*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "ARS 8000 9001 1884" -Direction Inbound -Protocol TCP `
        -LocalPort 8000,9001,1884 -Action Allow -Profile Private | Out-Null
    Ok "firewall rule 'ARS 8000 9001 1884' created (Wi-Fi must be set to Private in Windows settings)"
    if (-not ($Pico -or $Phone -or $Fake -gt 0)) { exit 0 }
}

# ------------------------------------------------------------------ 0. files
Step "Checking the backend folder"
$need = if ($remote) { @("scripts", "requirements-bridge.txt") } else { @("docker-compose.yml", "Dockerfile", "app", "sql", "scripts", "web") }
foreach ($f in $need) {
    if (-not (Test-Path $f)) { Fail "$f is missing in $PSScriptRoot - copy the full backend folder" }
}
if (-not $remote -and -not (Test-Path ".env")) { Fail ".env is missing (copy .env.example to .env)" }
if (-not $remote) {
    foreach ($f in "app\auth.py", "sql\migrate_v4.sql", "web\projector.html") {
        if (-not (Test-Path $f)) { Fail "$f is missing - unzip ars_round4.zip over this folder" }
    }
}
Ok "files present"

# ------------------------------------------------------------------ 1. Docker / remote server
if ($remote) {
    Step "Checking the main laptop $Server"
    try {
        $h = Invoke-RestMethod -Uri "http://${Server}:8000/health" -TimeoutSec 4
        if ($h.db -ne "ok" -or $h.mqtt -ne "connected") { Fail "backend on $Server is not healthy: $($h | ConvertTo-Json -Compress)" }
        Ok "backend reachable: http://${Server}:8000"
    } catch {
        Fail "cannot reach http://${Server}:8000 - is start.ps1 running there, same Wi-Fi, and did you run '.\start.ps1 -OpenFirewall' (as administrator) on it?"
    }
    $mqttOk = (Test-NetConnection -ComputerName $Server -Port 1884 -WarningAction SilentlyContinue).TcpTestSucceeded
    if (-not $mqttOk) { Fail "MQTT port 1884 on $Server is blocked - run '.\start.ps1 -OpenFirewall' as administrator on the main laptop" }
    Ok "MQTT reachable: ${Server}:1884"
} else {
    Step "Checking Docker"
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dd) {
            Warn "Docker is not running - starting Docker Desktop (can take a minute)"
            Start-Process $dd
            $ready = $false
            for ($i = 0; $i -lt 60; $i++) {
                Start-Sleep -Seconds 3
                docker info *> $null
                if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            }
            if (-not $ready) { Fail "Docker did not start - open Docker Desktop and wait for 'Engine running'" }
        } else {
            Fail "Docker is not running - open Docker Desktop and wait for 'Engine running'"
        }
    }
    Ok "Docker running"
}

# ------------------------------------------------------------------ 2. Python venv
Step "Checking the Python environment"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Warn "no .venv - creating it and installing packages"
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create .venv (is Python installed?)" }
    if ($remote) { & $py -m pip install -q aiomqtt httpx } else { & $py -m pip install -q -r requirements.txt }
}
if ($Pico -or $Phone) {
    & $py -c "import bleak, paho.mqtt, winrt.windows.devices.bluetooth.genericattributeprofile" 2> $null
    if ($LASTEXITCODE -ne 0) {
        Warn "installing bridge packages"
        & $py -m pip install -q -r requirements-bridge.txt
        if ($LASTEXITCODE -ne 0) { Fail "pip could not install requirements-bridge.txt - read the error above" }
    }
    Write-Host "   Bluetooth on this laptop:"
    & $py scripts\ble_bridge.py --check
}
Ok "venv ready"

# ------------------------------------------------------------------ 3. backend (main laptop only)
if (-not $remote) {
    Step "Starting database, broker and backend (docker compose)"
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { Fail "docker compose failed - read the error above" }

    if ($Reset) {
        Warn "Reset: deleting all data and loading the demo data"
        docker compose exec -T db psql -q -U ars -d ars -f /sql/schema.sql
        docker compose exec -T db psql -q -U ars -d ars -f /sql/seed.sql
        docker compose restart api | Out-Null
    }
    # bring an existing database up to date (each file is safe to run again; data is kept)
    foreach ($m in "migrate_lock.sql", "migrate_v4.sql") {
        if (Test-Path "sql\$m") {
            $out = docker compose exec -T db psql -q -v ON_ERROR_STOP=1 -U ars -d ars -f /sql/$m 2>&1
            if ($LASTEXITCODE -ne 0) { Fail "database update $m failed:`n$($out | Out-String)" }
        }
    }

    $healthy = $false
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
            if ($h.db -eq "ok" -and $h.mqtt -eq "connected") { $healthy = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { Fail "backend not healthy after 40 s - run: docker compose logs api --tail 40" }
    Ok "backend healthy: http://127.0.0.1:8000/health"
}

# ------------------------------------------------------------------ 4. helper windows
Step "Opening helper windows"
$started = @()
function Open-Window($title, $command) {
    $full = "`$host.UI.RawUI.WindowTitle = '$title'; Set-Location '$PSScriptRoot'; $command"
    # -EncodedCommand: the command arrives exactly as written (quotes and spaces in paths included)
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($full))
    $p = Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-EncodedCommand", $enc -PassThru
    $script:started += $p.Id
    Ok "$title (window pid $($p.Id))"
}

if ($Fake -gt 0 -and -not $remote) {
    Open-Window "ARS fake devices" "& '$py' scripts\fake_devices.py -n $Fake --mssv-from-class $ClassId --test $Test --room $Room"
}
if ($Pico) {
    Open-Window "ARS BLE bridge (Pico)" "& '$py' scripts\ble_bridge.py --room $Room $ports -v"
}
if ($Phone) {
    Open-Window "ARS BLE bridge (phones)" "& '$py' scripts\ble_bridge.py --phone --room $Room $ports -v"
}
if ($Watch -and -not $remote) {
    Open-Window "ARS backend log" "docker compose logs -f api"
    Open-Window "ARS MQTT traffic" "docker compose exec mosquitto mosquitto_sub -t 'ars/#' -v"
}
$started | Set-Content -Path $pidFile

if (-not $NoBrowser -and -not $remote) { Start-Process "http://127.0.0.1:8000/ui/" }

# ------------------------------------------------------------------ 5. summary
$ip = if ($remote) { $Server } else { Get-LanIp }
Step "Ready"
if ($remote) {
    Write-Host "   Main laptop  : $Server (backend, teacher page)"
    Write-Host "   This laptop  : Bluetooth bridge(s) only"
} else {
    Write-Host "   Teacher page : http://127.0.0.1:8000/ui/   (GV001 / 123456)"
    Write-Host "   Projector    : button 'Màn hình chiếu' on the teacher page (drag the window to the projector, press F)"
    Write-Host "   API docs     : http://127.0.0.1:8000/docs"
    if ($h -and $h.analyze) { Ok "AI analysis   : on" } else { Write-Host "   AI analysis  : off (add ANTHROPIC_API_KEY to .env, then .\start.ps1 again)" }
    if ($Fake -gt 0) { Write-Host "   Fake devices : $Fake (devices 1..$Fake, scenario '$Test', room $Room)" }
    if ($ip) { Write-Host "   2nd laptop   : .\start.ps1 -Server $ip -Phone    (if this laptop cannot advertise)" }
    if (-not (Get-NetFirewallRule -DisplayName "ARS*" -ErrorAction SilentlyContinue)) {
        Warn "no firewall rule yet: phones / a 2nd laptop cannot reach this one. Once, as administrator: .\start.ps1 -OpenFirewall"
    }
}
if ($ip) {
    Write-Host "   Phone (Wi-Fi): http://${ip}:8000/ui/phone.html?dev=51"
    if ($Phone) { Write-Host "   Phone (BLE)  : http://${ip}:8000/ui/phone_ble.html?dev=51   (Chrome flag: see BLE_BRIDGE.md)" }
}
if ($Pico)  { Write-Host "   Pico clickers: number them 51-60 (or -Fake 0), stop ble_receiver.py first" }
Write-Host "   Stop all     : .\stop.ps1"