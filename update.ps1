<#
.SYNOPSIS
  Claude Bridge updater: installs the latest release in place.
  Run by the 'ClaudeBridge Update' scheduled task once a day (-Scheduled) and when Claude calls update_bridge.
  To update now by hand: .\bridge.cmd update

  It downloads the release from GitHub (raphlau/ClaudeBridge), checks its SHA-256, and runs that release's
  installer unattended (which keeps config.json, the token and your settings). If the new version doesn't
  start, it puts the previous files back. Everything goes to logs\update.log.
#>
[CmdletBinding()]
param(
    [switch]$Scheduled,   # the daily run: skipped when config.json has "update": {"auto": false}
    [switch]$Force        # reinstall even if the latest release is already installed
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest very slow in PowerShell 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'ClaudeBridge'
$logDir = Join-Path $Dir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir 'update.log'
if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
    Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
}
$statePath = Join-Path $Dir 'update_state.json'
$reqPath = Join-Path $Dir 'update_request.json'
# Files in the install folder that belong to this PC, not to a release
$localFiles = @('config.json', 'connector.txt', 'chrome_tools.json', 'chrome_state.json', 'update_state.json', 'update_request.json', 'lockout.json', 'blocked_ips.json')

function Log([string]$msg) {
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" -Encoding UTF8
    Write-Host $msg
}
function Get-Version([string]$serverPy) {
    $m = Select-String -LiteralPath $serverPy -Pattern '^VERSION = "([0-9.]+)"' -List -ErrorAction SilentlyContinue
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return '0.0.0'
}
function Save-State([string]$result, [string]$detail, [string]$latest) {
    $s = [ordered]@{
        at      = (Get-Date -Format 'yyyy-MM-dd HH:mm')
        current = (Get-Version (Join-Path $Dir 'server.py'))
        latest  = $latest
        result  = $result
        detail  = $detail
    }
    [IO.File]::WriteAllText($statePath, ($s | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}
function Stop-Server {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*$Dir*server.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}
function Test-Health([int]$port, [int]$seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try { if ((Invoke-WebRequest "http://127.0.0.1:$port/healthz" -UseBasicParsing -TimeoutSec 2).Content -eq 'ok') { return $true } } catch {}
        Start-Sleep -Seconds 1
    }
    return $false
}
function Find-Uv {
    $uv = (Get-Command uv.exe -ErrorAction SilentlyContinue).Source
    if (-not $uv -and (Test-Path "$env:USERPROFILE\.local\bin\uv.exe")) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
    return $uv
}

function Invoke-Update([string]$tmp) {
    $cfg = Get-Content -LiteralPath (Join-Path $Dir 'config.json') -Raw | ConvertFrom-Json
    $repo = 'raphlau/ClaudeBridge'
    $auto = $true
    if ($cfg.update) {
        if ($cfg.update.repo) { $repo = [string]$cfg.update.repo }
        if ($cfg.update.auto -eq $false) { $auto = $false }
    }
    $port = [int]$cfg.listen_port

    # A request from update_bridge (Claude) runs even when the daily update is off
    $requested = Test-Path -LiteralPath $reqPath
    if ($requested) {
        try { if ((Get-Content -LiteralPath $reqPath -Raw | ConvertFrom-Json).reinstall) { $script:Force = $true } } catch {}
        Remove-Item -LiteralPath $reqPath -Force
    }
    if ($Scheduled -and -not $requested -and -not $auto) { Log 'Daily update is turned off in config.json.'; return 0 }
    # 'bridge.cmd off' is a kill switch: installing would start the server and publish it again
    if (-not (Test-Health $port 5)) { Log "The bridge isn't running (turned off?), so not updating."; return 0 }

    $current = Get-Version (Join-Path $Dir 'server.py')
    Invoke-WebRequest "https://github.com/$repo/releases/latest/download/version.txt" -OutFile "$tmp\version.txt" -UseBasicParsing -TimeoutSec 60
    $latest = (Get-Content -LiteralPath "$tmp\version.txt" -Raw).Trim()
    if ($latest -notmatch '^\d+(\.\d+){1,3}$') { throw "unexpected version.txt in the latest release: '$latest'" }
    if (([version]$latest -le [version]$current) -and -not $script:Force) {
        Log "Up to date ($current)."
        Save-State 'up to date' '' $latest
        return 0
    }

    $rel = "https://github.com/$repo/releases/download/v$latest"
    Invoke-WebRequest "$rel/ClaudeBridge.zip" -OutFile "$tmp\ClaudeBridge.zip" -UseBasicParsing -TimeoutSec 300
    Invoke-WebRequest "$rel/ClaudeBridge.zip.sha256" -OutFile "$tmp\sha256.txt" -UseBasicParsing -TimeoutSec 60
    $want = ((Get-Content -LiteralPath "$tmp\sha256.txt" -Raw).Trim() -split '\s+')[0]
    $got = (Get-FileHash -LiteralPath "$tmp\ClaudeBridge.zip" -Algorithm SHA256).Hash
    if ($want -ne $got) { throw "SHA-256 mismatch for release $latest (expected $want, got $got)" }
    Expand-Archive -LiteralPath "$tmp\ClaudeBridge.zip" -DestinationPath "$tmp\src"
    if ((Get-Version "$tmp\src\server.py") -ne $latest -or -not (Test-Path -LiteralPath "$tmp\src\install.ps1")) {
        throw "release $latest looks incomplete"
    }

    # Keep the current files so a bad release can be undone
    $prev = Join-Path $Dir 'previous'
    if (Test-Path -LiteralPath $prev) { Remove-Item -LiteralPath $prev -Recurse -Force }
    New-Item -ItemType Directory -Path $prev | Out-Null
    Get-ChildItem -LiteralPath $Dir -File | Where-Object { $localFiles -notcontains $_.Name } | Copy-Item -Destination $prev

    Log "Installing $latest (was $current)..."
    $ErrorActionPreference = 'Continue'   # stderr from the child shouldn't count as a failure; the exit code does
    $out = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$tmp\src\install.ps1" -Unattended -InstallDir $Dir *>&1 |
        ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Add-Content -LiteralPath $logPath -Value ($out | ForEach-Object { "    $_" }) -Encoding UTF8

    if ($code -eq 0 -and (Get-Version (Join-Path $Dir 'server.py')) -eq $latest -and (Test-Health $port 45)) {
        Log "Updated to $latest."
        Save-State 'updated' "from $current" $latest
        return 0
    }

    if ((Get-Version (Join-Path $Dir 'server.py')) -eq $current -and (Test-Health $port 5)) {
        Log "The installer stopped before changing anything (exit $code); still on $current."
        Save-State 'failed' "installer exit $code before any change; see logs\update.log" $latest
        return 1
    }
    Log "Version $latest didn't start (installer exit $code); putting $current back."
    Stop-Server
    Get-ChildItem -LiteralPath $prev -File | Copy-Item -Destination $Dir -Force
    $uv = Find-Uv
    if ($uv) {
        Push-Location $Dir
        try { & $uv sync --frozen --python 3.13 *>&1 | ForEach-Object { Add-Content -LiteralPath $logPath -Value "    $_" -Encoding UTF8 } } finally { Pop-Location }
    }
    Start-ScheduledTask -TaskName $TaskName
    $back = Test-Health $port 45
    Log "Rolled back to $current; server $(if ($back) { 'running' } else { 'NOT responding' })."
    Save-State 'failed, rolled back' "version $latest didn't start; see logs\update.log" $latest
    return 1
}

$tmp = Join-Path $env:TEMP ('ClaudeBridge-update-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$exitCode = 1
try {
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $exitCode = @(Invoke-Update $tmp)[-1]
} catch {
    Log "Update failed: $($_.Exception.Message)"
    try { Save-State 'failed' $_.Exception.Message '' } catch {}
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
exit $exitCode
