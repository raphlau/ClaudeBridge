<#
.SYNOPSIS
  Manage Claude Bridge.
    .\bridge.cmd status        what's running, local + public health, Claude app
    .\bridge.cmd off           kill switch: stop the server and take it off the internet
    .\bridge.cmd on            start the server and publish it again
    .\bridge.cmd restart       restart the server (after editing config.json)
    .\bridge.cmd logs          last 60 log lines (every tool call is logged)
    .\bridge.cmd update        install the latest release now (it also happens by itself once a day)
    .\bridge.cmd unlock        lift a lockout and clear blocked addresses before they expire
    .\bridge.cmd test          run the server in this window to see startup errors
    .\bridge.cmd rotate-token  make a new secret (update the connector in Claude afterwards)
    .\bridge.cmd uninstall     remove the tasks and the Funnel (leaves this folder for you to delete)
#>
param([Parameter(Position = 0)][ValidateSet('status', 'on', 'off', 'restart', 'logs', 'update', 'unlock', 'test', 'rotate-token', 'uninstall')][string]$Command = 'status')

$ErrorActionPreference = 'Stop'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = 'ClaudeBridge'
$UpdateTaskName = 'ClaudeBridge Update'
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
        $ver = Select-String -LiteralPath (Join-Path $Dir 'server.py') -Pattern '^VERSION = "([0-9.]+)"' -List
        Write-Host "Version:       $(if ($ver) { $ver.Matches[0].Groups[1].Value } else { '?' })"
        Write-Host "Task:          $(if ($t) { $t.State } else { 'not installed' })"
        $lock = Join-Path $Dir 'lockout.json'
        if (Test-Path -LiteralPath $lock) {
            $l = Get-Content -LiteralPath $lock -Raw | ConvertFrom-Json
            $left = [DateTimeOffset]::FromUnixTimeSeconds([long]$l.until).LocalDateTime
            if ($left -gt (Get-Date)) { Write-Host "LOCKED:        until $($l.until_text) ($($l.wrong_tokens) wrong tokens, last from $($l.last_from)); .\bridge.cmd unlock lifts it" -ForegroundColor Red }
        }
        $upd = Join-Path $Dir 'update_state.json'
        if (Test-Path -LiteralPath $upd) {
            $u = Get-Content -LiteralPath $upd -Raw | ConvertFrom-Json
            Write-Host "Last update:   $($u.at): $($u.result)$(if ($u.detail) { " ($($u.detail))" })"
        } elseif (-not (Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue)) {
            Write-Host "Last update:   no update task (re-run the installer to set it up)"
        }
        $blk = Join-Path $Dir 'blocked_ips.json'
        if (Test-Path -LiteralPath $blk) {
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            $n = @((Get-Content -LiteralPath $blk -Raw | ConvertFrom-Json).PSObject.Properties | Where-Object { [double]$_.Value -gt $now }).Count
            if ($n) { Write-Host "Blocked:       $n internet address(es) ignored for a while (scanners or wrong tokens)" }
        }
        Write-Host "Local server:  $(if (Health "$target/healthz") { 'OK' } else { 'NOT responding' })"
        # Check the way Claude's servers connect: public DNS (not Tailscale's), then HTTPS over the internet.
        # On this PC the name resolves inside the tailnet, which would hide a Funnel problem.
        $hostName = ([Uri]$cfg.public_url).Host
        $pub = 'NOT reachable'
        try {
            $dns = Invoke-RestMethod "https://cloudflare-dns.com/dns-query?name=$hostName&type=A" -Headers @{ Accept = 'application/dns-json' } -UseBasicParsing -TimeoutSec 10
            $ip = @($dns.Answer | Where-Object { $_.type -eq 1 } | ForEach-Object { $_.data }) | Select-Object -First 1
            if (-not $ip) { $pub = 'NOT reachable: no public DNS name yet (Funnel off, or still appearing)' }
            else {
                $body = & curl.exe -s -m 15 --resolve "${hostName}:443:$ip" "https://$hostName/k/$($cfg.token)/healthz"
                if ($body -eq 'ok') { $pub = 'OK' } else { $pub = "NOT reachable through Funnel ($ip)" }
            }
        } catch { $pub = "couldn't check ($($_.Exception.Message))" }
        Write-Host "From internet: $pub"
        foreach ($r in @($cfg.roots)) {
            # Same rule as the server: the nearest folder (upwards) with .obsidian, or .megaignore/.debris
            $sync = ''; $p = $r.path
            while ($p -and -not $sync) {
                if (Test-Path -LiteralPath (Join-Path $p '.obsidian')) { $sync = 'Obsidian' }
                elseif ((Test-Path -LiteralPath (Join-Path $p '.megaignore')) -or (Test-Path -LiteralPath (Join-Path $p '.debris'))) { $sync = 'MEGA' }
                $p = Split-Path $p -Parent
            }
            Write-Host ("Folder:        {0} -> {1} ({2})" -f $r.name, $r.path,
                $(if (-not (Test-Path -LiteralPath $r.path)) { 'MISSING' } elseif ($sync) { "synced by $sync" } else { 'no sync app found' }))
        }
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
    'unlock' {
        Remove-Item -LiteralPath (Join-Path $Dir 'lockout.json') -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $Dir 'blocked_ips.json') -Force -ErrorAction SilentlyContinue
        Stop-Server; Start-Sleep -Seconds 1; Start-ScheduledTask -TaskName $TaskName; Start-Sleep -Seconds 3
        Write-Host "Unlocked. Local server: $(if (Health "$target/healthz") { 'OK' } else { 'not responding; try .\bridge.cmd test' })"
        Write-Host "If those wrong tokens weren't yours, also run .\bridge.cmd rotate-token." -ForegroundColor Yellow
    }
    'update' {
        # Runs update.ps1 through its scheduled task, so it works the same from any window
        if (-not (Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue)) {
            Write-Host "The update task isn't set up; run the installer once." -ForegroundColor Yellow
            break
        }
        $log = Join-Path $Dir 'logs\update.log'
        $before = 0
        if (Test-Path -LiteralPath $log) { $before = @(Get-Content -LiteralPath $log).Count }
        Start-ScheduledTask -TaskName $UpdateTaskName
        Write-Host 'Checking for updates...'
        Start-Sleep -Seconds 2
        for ($i = 0; $i -lt 600 -and (Get-ScheduledTask -TaskName $UpdateTaskName).State -eq 'Running'; $i++) { Start-Sleep -Seconds 1 }
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log | Select-Object -Skip $before | Where-Object { $_ -notlike '    *' } }
    }
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
        Unregister-ScheduledTask -TaskName $UpdateTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed the tasks and the Funnel. Delete $Dir to remove everything, and remove the connector in Claude." -ForegroundColor Yellow
    }
}
