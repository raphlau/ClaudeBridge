<#
.SYNOPSIS
  Manage Claude Bridge.
    .\bridge.cmd status        what's running, local + public health, Claude app
    .\bridge.cmd off           kill switch: stop the server and take it off the internet
    .\bridge.cmd on            start the server and publish it again
    .\bridge.cmd restart       restart the server (after editing config.json)
    .\bridge.cmd logs          last 60 log lines (every tool call is logged)
    .\bridge.cmd test          run the server in this window to see startup errors
    .\bridge.cmd rotate-token  make a new secret (update the connector in Claude afterwards)
    .\bridge.cmd uninstall     remove the task and the Funnel (leaves this folder for you to delete)
#>
param([Parameter(Position = 0)][ValidateSet('status', 'on', 'off', 'restart', 'logs', 'test', 'rotate-token', 'uninstall')][string]$Command = 'status')

$ErrorActionPreference = 'Stop'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'ClaudeBridge'
$cfgPath = Join-Path $Dir 'config.json'
$cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
$Port = [int]$cfg.listen_port
$ts = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = "$env:ProgramFiles\Tailscale\tailscale.exe" }
$target = "http://127.0.0.1:$Port"

function Health([string]$url) {
    try { return (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 8).Content -eq 'ok' } catch { return $false }
}
function Stop-Server {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*$Dir*server.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

switch ($Command) {
    'status' {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "Task:          $(if ($t) { $t.State } else { 'not installed' })"
        Write-Host "Local server:  $(if (Health "$target/healthz") { 'OK' } else { 'NOT responding' })"
        Write-Host "Public URL:    $($cfg.public_url)  ->  $(if (Health "$($cfg.public_url)/healthz") { 'OK' } else { 'NOT reachable' })"
        Write-Host "Claude app:    $(if (Get-Process -Name claude -ErrorAction SilentlyContinue) { 'running' } else { 'not running' })"
        Write-Host "`nFunnel:"; & $ts funnel status
    }
    'off' {
        & $ts funnel --https=443 $target off
        Stop-Server
        Write-Host "Bridge is OFF (not running, not published). Turn back on with: .\bridge.cmd on" -ForegroundColor Yellow
    }
    'on' {
        Start-ScheduledTask -TaskName $TaskName
        & $ts funnel --bg --https=443 $target
        Start-Sleep -Seconds 3
        Write-Host "Local server: $(if (Health "$target/healthz") { 'OK' } else { 'not responding yet' })" -ForegroundColor Green
    }
    'restart' {
        Stop-Server; Start-Sleep -Seconds 1; Start-ScheduledTask -TaskName $TaskName; Start-Sleep -Seconds 3
        Write-Host "Local server: $(if (Health "$target/healthz") { 'OK' } else { 'not responding; try .\bridge.cmd test' })"
    }
    'logs' { Get-Content -LiteralPath (Join-Path $Dir 'logs\bridge.log') -Tail 60 }
    'test' {
        Stop-Server
        Write-Host "Running in the foreground; press Ctrl+C to stop, then run .\bridge.cmd on" -ForegroundColor Yellow
        $env:CLAUDE_BRIDGE_VERBOSE = '1'
        & (Join-Path $Dir '.venv\Scripts\python.exe') (Join-Path $Dir 'server.py')
    }
    'rotate-token' {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $cfg.token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
        Stop-Server; Start-ScheduledTask -TaskName $TaskName
        Write-Host "New token (update the connector in Claude):`n  Authorization: Bearer $($cfg.token)`n  or URL: $($cfg.public_url)/k/$($cfg.token)/mcp" -ForegroundColor Green
    }
    'uninstall' {
        & $ts funnel --https=443 $target off
        Stop-Server
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed the task and the Funnel. Delete $Dir to remove everything, and remove the connector in Claude." -ForegroundColor Yellow
    }
}
