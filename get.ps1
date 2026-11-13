# Claude Bridge one-line installer. In PowerShell opened from the Start menu (not inside the Claude app):
#
#   irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1 | iex
#
# Downloads the latest release, checks its SHA-256 and runs its installer (install.ps1). Safe to re-run: an
# existing install keeps its folders, token and settings. To pass installer options such as -Reconfigure:
#
#   & ([scriptblock]::Create((irm https://github.com/raphlau/ClaudeBridge/releases/latest/download/get.ps1))) -Reconfigure
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $repo = 'raphlau/ClaudeBridge'
    $installerArgs = @($args)
    $tmp = Join-Path $env:TEMP ('ClaudeBridge-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Write-Host 'Downloading Claude Bridge...' -ForegroundColor Cyan
        Invoke-WebRequest "https://github.com/$repo/releases/latest/download/version.txt" -OutFile "$tmp\version.txt" -UseBasicParsing
        $ver = (Get-Content -LiteralPath "$tmp\version.txt" -Raw).Trim()
        if ($ver -notmatch '^\d+(\.\d+){1,3}$') { throw "Unexpected version '$ver' in the latest release." }
        $rel = "https://github.com/$repo/releases/download/v$ver"
        Invoke-WebRequest "$rel/ClaudeBridge.zip" -OutFile "$tmp\ClaudeBridge.zip" -UseBasicParsing
        Invoke-WebRequest "$rel/ClaudeBridge.zip.sha256" -OutFile "$tmp\sha256.txt" -UseBasicParsing
        $want = ((Get-Content -LiteralPath "$tmp\sha256.txt" -Raw).Trim() -split '\s+')[0]
        $got = (Get-FileHash -LiteralPath "$tmp\ClaudeBridge.zip" -Algorithm SHA256).Hash
        if ($want -ne $got) { throw 'The download is damaged (SHA-256 mismatch). Try again.' }
        Expand-Archive -LiteralPath "$tmp\ClaudeBridge.zip" -DestinationPath "$tmp\src"
        Write-Host "Claude Bridge $ver downloaded; starting the installer.`n" -ForegroundColor Cyan
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$tmp\src\install.ps1" @installerArgs
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
} @args
