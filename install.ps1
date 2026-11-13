<#
.SYNOPSIS
  Installs Claude Bridge: a small background server that lets Claude read and edit your MEGA folder
  and Obsidian vault on this PC (and start/quit the Claude desktop app), published through Tailscale Funnel.

.USAGE
  From the unzipped folder, in a normal PowerShell window (no need for admin):
    powershell -ExecutionPolicy Bypass -File .\install.ps1

  Re-run any time to update the code. Add -Reconfigure to choose folders again,
  and -ChromeProfile 'Profile 1' if Claude in Chrome is in a non-default Chrome profile.
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$InstallDir = "$env:LOCALAPPDATA\ClaudeBridge",
    [switch]$Reconfigure,
    [string]$ChromeProfile = ''   # e.g. 'Profile 1' if Claude in Chrome lives in a non-default Chrome profile
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ClaudeBridge'
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

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Say "Claude Bridge installer`n"

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
    foreach ($f in 'server.py', 'pyproject.toml', 'uv.lock', 'bridge.ps1', 'bridge.cmd', 'README.md', 'config.example.json', 'selftest.py', 'CLAUDE.md') {
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
function Ask-Folder([string]$label, [string]$hint) {
    while ($true) {
        Write-Host "`n$label" -ForegroundColor White
        Write-Host "  $hint" -ForegroundColor DarkGray
        $p = Read-Host "  Folder path (leave blank to skip)"
        if (-not $p) { return $null }
        $p = $p.Trim().Trim('"')
        if (Test-Path -LiteralPath $p -PathType Container) { return (Resolve-Path -LiteralPath $p).Path }
        Write-Host "  That folder doesn't exist, try again." -ForegroundColor Yellow
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
if ($cfg -and -not $Reconfigure) {
    Say "`nKeeping your existing config.json (re-run with -Reconfigure to choose folders again)."
    $token = $cfg.token
    $roots = @($cfg.roots)
    if ($cfg.listen_port) { $Port = [int]$cfg.listen_port }
} else {
    Say "`nChoose the folders. Claude can read them and add or change files, but never delete or move anything."
    Say "Tip: paste paths from Explorer (Shift+right-click a folder > Copy as path)."
    $roots = @()
    $b = Ask-Folder 'Your MEGA folder' 'the folder the MEGA app syncs, or a subfolder such as "Important Documents"'
    if ($b) { $roots += [ordered]@{ name = 'Backups'; path = $b; mode = 'write'; sync_app = 'MEGA' } }
    $n = Ask-Folder 'Your Obsidian vault' 'the top folder of the vault (the one that contains a .obsidian folder)'
    if ($n) {
        if (-not (Test-Path -LiteralPath (Join-Path $n '.obsidian'))) { Say "  Note: there's no .obsidian folder in there. Is this the vault's top folder?" 'Yellow' }
        $roots += [ordered]@{ name = 'Notes'; path = $n; mode = 'write'; sync_app = 'Obsidian' }
    }
    if ($roots.Count -eq 0) { Fail "No folders chosen, nothing to share." }
    $token = $null
    if ($cfg) { $token = $cfg.token }   # keep the token so the connector in Claude keeps working
}

if (-not $token -or $token.Length -lt 32) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# Desktop apps the bridge can check and start: MEGA and Obsidian (they upload changes), Chrome and Claude
$apps = @()
$wanted = @($roots | ForEach-Object { $_.sync_app })
if ($wanted -contains 'MEGA') {
    $mega = Find-Exe 'MEGAsync' @("$env:LOCALAPPDATA\MEGAsync\MEGAsync.exe", "$env:ProgramFiles\MEGAsync\MEGAsync.exe", "${env:ProgramFiles(x86)}\MEGAsync\MEGAsync.exe")
    if ($mega) { $apps += [ordered]@{ name = 'MEGA'; process = 'MEGAsync.exe'; launch = @($mega); can_quit = $false }; Say "Found MEGA: $mega" }
    else { Say "Couldn't find the MEGA desktop app (MEGAsync.exe). Changes in Backups won't upload until it runs; re-run this installer after installing it." 'Yellow' }
}
if ($wanted -contains 'Obsidian') {
    $vault = ($roots | Where-Object { $_.sync_app -eq 'Obsidian' } | Select-Object -First 1).path
    $obs = Find-Exe 'Obsidian' @("$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe", "$env:LOCALAPPDATA\Obsidian\Obsidian.exe", "$env:ProgramFiles\Obsidian\Obsidian.exe")
    if ($obs) {
        # Opening this link starts Obsidian if needed and opens the vault, which is what Obsidian Sync needs
        $uri = 'obsidian://open?vault=' + [Uri]::EscapeDataString((Split-Path -Leaf $vault))
        $apps += [ordered]@{ name = 'Obsidian'; process = 'Obsidian.exe'; launch = $uri; vault = $vault; can_quit = $false }
        Say "Found Obsidian: $obs"
    } else { Say "Couldn't find Obsidian. Note changes won't sync until it runs; re-run this installer after installing it." 'Yellow' }
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
# keep any other settings you added by hand (deny lists, history_days, sync_watchdog_minutes, limits...)
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
  Name: Home PC
  URL:  https://$dns/mcp
  Request header:  Authorization: Bearer $token

If there's no place for a request header, use this URL instead (it contains the secret, treat it like a password):
  https://$dns/k/$token/mcp

For download/upload links to work from Claude's cloud workspace, add this to Claude's allowed domains:
  $dns

Shared folders:
$(($roots | ForEach-Object { "  $($_.name)  ($($_.mode))  $($_.path)" }) -join "`n")
"@
$sumPath = Join-Path $InstallDir 'connector.txt'
Write-Utf8NoBom $sumPath $summary
Lock-File $sumPath
Write-Host ""
Write-Host $summary -ForegroundColor Green
if (-not $public) { Say "`nThe public address isn't answering yet. New Funnel names can take up to 10 minutes; check later with: .\bridge.cmd status" 'Yellow' }
Say "`nThis summary is saved in $sumPath. Manage the bridge with $InstallDir\bridge.cmd (status, on, off, logs, rotate-token, uninstall)."
