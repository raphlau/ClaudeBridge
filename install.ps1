<#
.SYNOPSIS
  Installs Claude Bridge: a small background server that lets Claude read and edit your MEGA folder
  and Obsidian vault on this PC (and start/quit the Claude desktop app), published through Tailscale Funnel.

.USAGE
  Easiest, in PowerShell from the Start menu (no need for admin):
    irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1 | iex
  Or from an unzipped release folder:
    powershell -ExecutionPolicy Bypass -File .\install.ps1

  Re-run any time to update the code. Add -Reconfigure to choose folders again,
  and -ChromeProfile 'Profile 1' if Claude in Chrome is in a non-default Chrome profile.
  -Unattended is used by update.ps1: no questions, and it needs an existing config.json.
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$InstallDir = "$env:LOCALAPPDATA\ClaudeBridge",
    [switch]$Reconfigure,
    [string]$ChromeProfile = '',   # e.g. 'Profile 1' if Claude in Chrome lives in a non-default Chrome profile
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ClaudeBridge'
$UpdateTaskName = 'ClaudeBridge Update'
function Say([string]$msg, [string]$color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
function Fail([string]$msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }
function Write-Utf8NoBom([string]$path, [string]$text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}
function Lock-File([string]$path) {
    # Only you, SYSTEM and Administrators can read it (it holds the secret token).
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    icacls $path /inheritance:r /grant:r "${me}:(M)" "*S-1-5-18:(F)" "*S-1-5-32-544:(F)" | Out-Null
}

function Test-AppDataRedirected {
    # Apps from the Microsoft Store (such as the Claude desktop app) can run the programs they start with AppData
    # writes redirected into the app's own storage. Installed there, the bridge would be invisible to its
    # background task. Check by writing a probe folder and looking for it in the apps' storage.
    $probe = 'cb-probe-' + [guid]::NewGuid().ToString('N')
    $p = Join-Path $env:LOCALAPPDATA $probe
    try {
        New-Item -ItemType Directory -Path $p -ErrorAction Stop | Out-Null
        $pk = Join-Path $env:LOCALAPPDATA 'Packages'
        $hit = @(Get-ChildItem -LiteralPath $pk -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "LocalCache\Local\$probe") })
        return ($hit.Count -gt 0)
    } catch {
        return $false
    } finally {
        Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    }
}

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Say "Claude Bridge installer$(if ($Unattended) { ' (unattended)' })`n"
if ($Unattended -and $Reconfigure) { Fail "-Unattended can't be combined with -Reconfigure." }
if (-not $Unattended -and (Test-AppDataRedirected)) {
    Fail ("This window runs inside another app (probably the Claude desktop app), which redirects AppData. " +
          "Open PowerShell from the Start menu and run the install command there.")
}

# ---------------------------------------------------------------- Tailscale
$ts = (Get-Command tailscale.exe -ErrorAction SilentlyContinue).Source
if (-not $ts -and (Test-Path "$env:ProgramFiles\Tailscale\tailscale.exe")) { $ts = "$env:ProgramFiles\Tailscale\tailscale.exe" }
if (-not $ts) { Fail "Tailscale isn't installed (https://tailscale.com/download/windows)." }
try { $tsStatus = (& $ts status --json) | Out-String | ConvertFrom-Json } catch { Fail "Couldn't read Tailscale status. Is Tailscale running and logged in?" }
if ($tsStatus.BackendState -ne 'Running') { Fail "Tailscale isn't connected (state: $($tsStatus.BackendState)). Log in, then re-run." }
$dns = ("" + $tsStatus.Self.DNSName).TrimEnd('.')
if (-not $dns) { Fail "This PC has no Tailscale DNS name. Turn on MagicDNS in the Tailscale admin console (DNS page), then re-run." }
Say "Tailscale OK: this PC is $dns"

# ---------------------------------------------------------------- uv (Python manager)
$uv = (Get-Command uv.exe -ErrorAction SilentlyContinue).Source
if (-not $uv -and (Test-Path "$env:USERPROFILE\.local\bin\uv.exe")) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
if (-not $uv) {
    Say "Installing uv (Astral's Python manager)..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uv = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uv)) { Fail "uv didn't install. Install it from https://docs.astral.sh/uv/ and re-run." }
}

# ---------------------------------------------------------------- copy files
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$InstallDir = (Resolve-Path $InstallDir).Path
if ((Resolve-Path $Src).Path -ne $InstallDir) {
    foreach ($f in 'server.py', 'pyproject.toml', 'uv.lock', 'bridge.ps1', 'bridge.cmd', 'update.ps1', 'README.md', 'config.example.json', 'selftest.py', 'CLAUDE.md') {
        Copy-Item -LiteralPath (Join-Path $Src $f) -Destination $InstallDir -Force
    }
}
Get-ChildItem -LiteralPath $InstallDir -File | Unblock-File -ErrorAction SilentlyContinue
# Stop a running copy first (an update can't replace files Python has open)
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" | Where-Object { $_.CommandLine -like "*$InstallDir*server.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Say "Installing Python dependencies (first run downloads Python; this can take a minute)..."
Push-Location $InstallDir
try {
    & $uv sync --frozen --python 3.13
    if ($LASTEXITCODE -ne 0) { Fail "uv sync failed (see messages above)." }
} finally { Pop-Location }
$pyw = Join-Path $InstallDir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) { Fail "Python environment missing: $pyw" }

# ---------------------------------------------------------------- config
$cfgPath = Join-Path $InstallDir 'config.json'
function Get-FileCandidates {
    # Synced document folders, best guesses first: subfolders that look like important papers, then the folder itself
    $bases = @("$env:USERPROFILE\Documents\MEGA", "$env:USERPROFILE\MEGA", "$env:USERPROFILE\MEGAsync", "$env:USERPROFILE\Dropbox")
    if ($env:OneDrive) { $bases += "$env:OneDrive\Documents" }
    $bases += @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' -ErrorAction SilentlyContinue | ForEach-Object { "$($_.DeviceID)\MEGA" })
    $out = @()
    foreach ($b in $bases) {
        if (-not (Test-Path -LiteralPath $b -PathType Container)) { continue }
        $out += @(Get-ChildItem -LiteralPath $b -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'document|important|paper|admin' } | ForEach-Object { $_.FullName })
        $out += $b
    }
    $out += "$env:USERPROFILE\Documents"
    return @($out | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -Unique)
}
function Get-NoteCandidates {
    # Obsidian lists its vaults in %APPDATA%\obsidian\obsidian.json; most recently opened first
    $out = @()
    $f = Join-Path $env:APPDATA 'obsidian\obsidian.json'
    if (Test-Path -LiteralPath $f) {
        try {
            $j = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json
            $out += @($j.vaults.PSObject.Properties | ForEach-Object { $_.Value } | Sort-Object { [double]$_.ts } -Descending | ForEach-Object { $_.path })
        } catch {}
    }
    $out += "$env:USERPROFILE\Documents\Notes"
    return @($out | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) } | Select-Object -Unique)
}
function Choose-Folder([string]$label, [string]$hint, [string[]]$candidates) {
    Write-Host "`n$label" -ForegroundColor White
    Write-Host "  $hint" -ForegroundColor DarkGray
    $shown = @($candidates | Select-Object -First 6)
    for ($i = 0; $i -lt $shown.Count; $i++) { Write-Host ("  {0}) {1}" -f ($i + 1), $shown[$i]) }
    $prompt = if ($shown.Count) { "  Enter for 1, another number, a folder path, or 'skip'" } else { "  Folder path, or 'skip'" }
    while ($true) {
        $a = ("" + (Read-Host $prompt)).Trim().Trim('"')
        if ($a -eq 'skip') { return $null }
        if (-not $a -and $shown.Count) { return $shown[0] }
        if ($a -match '^\d+$' -and [int]$a -ge 1 -and [int]$a -le $shown.Count) { return $shown[[int]$a - 1] }
        if ($a -and (Test-Path -LiteralPath $a -PathType Container)) { return (Resolve-Path -LiteralPath $a).Path }
        Write-Host "  That's not one of the options or an existing folder; try again." -ForegroundColor Yellow
    }
}
function Find-Exe([string]$procName, [string[]]$candidates) {
    $p = Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1
    if ($p) { return $p.Path }
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
    return $null
}

$cfg = $null
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json } catch { Say "Existing config.json couldn't be read; starting fresh." 'Yellow' }
}
if ($Unattended -and -not $cfg) { Fail "No usable config.json in $InstallDir; run the installer by hand once first." }
if ($cfg -and -not $Reconfigure) {
    Say "Keeping your folders and token (re-run with -Reconfigure to choose folders again)."
    $token = $cfg.token
    $roots = @($cfg.roots)
    if ($cfg.listen_port) { $Port = [int]$cfg.listen_port }
    foreach ($r in $roots) {
        # Before 1.6 the documents folder was called 'Backups'; old paths keep working through the alias
        if ($r.name -eq 'Backups') {
            $r.name = 'Files'
            $r | Add-Member -NotePropertyName aliases -NotePropertyValue @('Backups') -Force
            $h = Join-Path $InstallDir 'history\Backups'
            if ((Test-Path -LiteralPath $h) -and -not (Test-Path -LiteralPath (Join-Path $InstallDir 'history\Files'))) {
                Rename-Item -LiteralPath $h -NewName 'Files'
            }
            Say "Renamed the shared folder 'Backups' to 'Files' ('Backups/...' paths still work)."
        }
    }
} else {
    Say "`nPick the two folders to share. Claude can read and add or change files there, but never delete or move anything."
    $roots = @()
    $f = Choose-Folder 'Files: your documents' 'Papers, scans, letters, office files... e.g. a synced "Important Documents" folder.' (Get-FileCandidates)
    if ($f) {
        $r = [ordered]@{ name = 'Files'; path = $f; mode = 'write' }
        if ($f -match '\\MEGA(sync)?(\\|$)') { $r.sync_app = 'MEGA' }
        $roots += $r
    }
    $n = Choose-Folder 'Notes: your notes' 'A folder of Markdown notes, e.g. your Obsidian vault.' (Get-NoteCandidates)
    if ($n) {
        $r = [ordered]@{ name = 'Notes'; path = $n; mode = 'write' }
        if (Test-Path -LiteralPath (Join-Path $n '.obsidian')) { $r.sync_app = 'Obsidian' }
        $roots += $r
    }
    if ($roots.Count -eq 0) { Fail "No folders chosen, nothing to share." }
    $token = $null
    if ($cfg) { $token = $cfg.token }   # keep the token so the connector in Claude keeps working
}

if (-not $token -or $token -notmatch '^[A-Za-z0-9_-]{32,}$') {
    # 32 bytes from the OS's cryptographic random generator: 256 bits, 43 characters, made on this PC
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# Desktop apps the bridge can check and start: the apps that sync the shared folders (started only when Claude
# changes something there), Chrome and Claude
$apps = @()
$wanted = @($roots | ForEach-Object { $_.sync_app })
if ($wanted -contains 'MEGA') {
    $mega = Find-Exe 'MEGAsync' @("$env:LOCALAPPDATA\MEGAsync\MEGAsync.exe", "$env:ProgramFiles\MEGAsync\MEGAsync.exe", "${env:ProgramFiles(x86)}\MEGAsync\MEGAsync.exe")
    if ($mega) { $apps += [ordered]@{ name = 'MEGA'; process = 'MEGAsync.exe'; launch = @($mega); can_quit = $false }; Say "Found MEGA: $mega" }
    else { Say "Couldn't find the MEGA app, which syncs Files; re-run this installer after installing it." 'Yellow' }
}
if ($wanted -contains 'Obsidian') {
    $vault = ($roots | Where-Object { $_.sync_app -eq 'Obsidian' } | Select-Object -First 1).path
    $obs = Find-Exe 'Obsidian' @("$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe", "$env:LOCALAPPDATA\Obsidian\Obsidian.exe", "$env:ProgramFiles\Obsidian\Obsidian.exe")
    if ($obs) {
        # Opening this link starts Obsidian if needed and opens the vault, which is what Obsidian Sync needs
        $uri = 'obsidian://open?vault=' + [Uri]::EscapeDataString((Split-Path -Leaf $vault))
        $apps += [ordered]@{ name = 'Obsidian'; process = 'Obsidian.exe'; launch = $uri; vault = $vault; can_quit = $false }
        Say "Found Obsidian: $obs"
    } else { Say "Couldn't find Obsidian, which syncs Notes; re-run this installer after installing it." 'Yellow' }
}
$chrome = $null
foreach ($k in 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe') {
    $v = (Get-ItemProperty -LiteralPath $k -ErrorAction SilentlyContinue).'(default)'
    if ($v -and (Test-Path -LiteralPath $v)) { $chrome = $v; break }
}
if (-not $chrome) { $chrome = Find-Exe 'chrome' @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") }
if ($chrome) {
    $chromeLaunch = @($chrome)
    if ($ChromeProfile) { $chromeLaunch += "--profile-directory=$ChromeProfile" }
    $apps += [ordered]@{ name = 'Chrome'; process = 'chrome.exe'; launch = $chromeLaunch; match = @('\google\chrome\'); can_quit = $true }
    Say "Found Chrome: $chrome$(if ($ChromeProfile) { " (profile: $ChromeProfile)" })"
} else { Say "Couldn't find Google Chrome; the bridge won't be able to start it." 'Yellow' }
$app = Get-StartApps | Where-Object { $_.Name -eq 'Claude' } | Select-Object -First 1
if ($app) {
    $apps += [ordered]@{ name = 'Claude'; process = 'claude.exe'; launch = @('explorer.exe', "shell:AppsFolder\$($app.AppID)"); can_quit = $true }
    Say "Found the Claude app ($($app.AppID))."
} else { Say "Couldn't find the Claude desktop app in the Start menu; re-run this installer after installing it." 'Yellow' }

$cfgOut = [ordered]@{
    listen_port = $Port
    public_url  = "https://$dns"
    token       = $token
    roots       = $roots
    apps        = $apps
    quiet       = $true
}
# keep any other settings you added by hand (deny lists, history_days, limits, chrome, update...)
if ($cfg) {
    foreach ($prop in $cfg.PSObject.Properties) {
        if ($prop.Name -notin @('listen_port', 'public_url', 'token', 'roots', 'apps', 'sync_apps', 'claude_app', 'quiet')) { $cfgOut[$prop.Name] = $prop.Value }
    }
}
Write-Utf8NoBom $cfgPath ($cfgOut | ConvertTo-Json -Depth 6)
Lock-File $cfgPath

# ---------------------------------------------------------------- background task (runs at sign-in, restarts on failure)
Say "`nRegistering the background task '$TaskName'..."
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$InstallDir\server.py`"" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Me
$principal = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Claude Bridge: scoped folder access for Claude via Tailscale Funnel' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# Daily update check (update.ps1), also started on demand by update_bridge. It runs in a headless console host:
# with Windows Terminal as the default terminal, powershell's -WindowStyle Hidden still shows a window.
# An update runs this installer from inside that task, so then it only adjusts the task's action in place.
$uArgs = "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$InstallDir\update.ps1`" -Scheduled"
$uAction = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $uArgs -WorkingDirectory $InstallDir
$existing = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
if ($Unattended -and $existing) {
    if (@($existing.Actions)[0].Execute -ne 'conhost.exe') {
        Set-ScheduledTask -TaskName $UpdateTaskName -Action $uAction | Out-Null
        Say "Updates now run without a window."
    }
} else {
    $uTrigger = New-ScheduledTaskTrigger -Daily -At '04:00' -RandomDelay (New-TimeSpan -Hours 2)
    $uSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $UpdateTaskName -Action $uAction -Trigger $uTrigger -Principal $principal -Settings $uSettings `
        -Description 'Claude Bridge: installs new releases (daily, and when Claude calls update_bridge)' -Force | Out-Null
    Say "Registered the daily update task '$UpdateTaskName'."
}

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2).Content -eq 'ok') { $ok = $true; break } } catch {}
}
if (-not $ok) { Fail "The server didn't start. Run '.\bridge.cmd test' in $InstallDir to see the error." }
Say "Server running on this PC (port $Port, local only)."

# ---------------------------------------------------------------- Tailscale Funnel
Say "`nPublishing it with Tailscale Funnel. If a browser page opens or a link appears, approve Funnel for this PC."
& $ts funnel --bg --https=443 "http://127.0.0.1:$Port"
if ($LASTEXITCODE -ne 0) { Say "Funnel didn't turn on. Approve it in the Tailscale admin console, then run: .\bridge.cmd on" 'Yellow' }

$public = $false
try { $public = (Invoke-WebRequest "https://$dns/healthz" -UseBasicParsing -TimeoutSec 10).Content -eq 'ok' } catch {}

# ---------------------------------------------------------------- summary
$summary = @"
Claude Bridge is installed.

Add it to Claude (Settings > Connectors > Add custom connector):
  Name: Bridge ($env:COMPUTERNAME)
  URL:  https://$dns/k/$token/mcp
This URL contains the secret: treat it like a password. Each PC has its own.

(If a tool lets you set request headers instead: https://$dns/mcp with  Authorization: Bearer $token)

For download/upload links to work from Claude's cloud workspace, add this to Claude's allowed domains:
  $dns

Shared folders:
$(($roots | ForEach-Object { "  $($_.name)  ($($_.mode))  $($_.path)" }) -join "`n")
"@
$sumPath = Join-Path $InstallDir 'connector.txt'
Write-Utf8NoBom $sumPath $summary
Lock-File $sumPath
if ($Unattended) {
    # The update log keeps this output, so don't print the token
    Say "Claude Bridge is installed and running; connector details unchanged (in $sumPath)."
    exit 0
}
Write-Host ""
Write-Host $summary -ForegroundColor Green
if (-not $public) { Say "`nThe public address isn't answering yet. New Funnel names can take up to 10 minutes; check later with: .\bridge.cmd status" 'Yellow' }
Say "`nThis summary is saved in $sumPath. Manage the bridge with $InstallDir\bridge.cmd (status, on, off, logs, update, rotate-token, uninstall)."
Say "Updates install by themselves once a day (or ask Claude to run update_bridge)."
