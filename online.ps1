# Puts the attendance system on the internet through Cloudflare Tunnel (real HTTPS, no router setup).
# The server keeps running on this PC; Cloudflare forwards https://your-address to http://127.0.0.1:8000.
#
#   Try it now (temporary random address *.trycloudflare.com, changes every time):
#       powershell -ExecutionPolicy Bypass -File online.ps1
#
#   Permanent address on your own domain (e.g. https://attendance.example.com), starts with Windows:
#     1. Add your domain to a free Cloudflare account.
#     2. Cloudflare dashboard > Zero Trust > Networks > Tunnels > Create tunnel (Cloudflared),
#        add a Public Hostname:  attendance.example.com  ->  HTTP  ->  localhost:8000
#     3. Copy the tunnel token and run in an *Administrator* PowerShell:
#       powershell -ExecutionPolicy Bypass -File online.ps1 -Token <token>
#
# In both cases the server must run with --behind-proxy (see autostart.ps1 -BehindProxy).
param([string]$Token, [int]$Port = 8000)

$ErrorActionPreference = 'Stop'

function Find-Cloudflared {
    $c = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe") {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$cf = Find-Cloudflared
if (-not $cf) {
    Write-Host 'Installing cloudflared ...'
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements | Out-Host
    $cf = Find-Cloudflared
    if (-not $cf) { throw 'cloudflared was installed but not found. Open a new PowerShell window and run this script again.' }
}

if ($Token) {
    & $cf service install $Token
    Write-Host 'Cloudflare Tunnel installed as a Windows service. Your public hostname is now live.'
    return
}

try { Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 3 | Out-Null }
catch { Write-Warning "The server is not answering on http://127.0.0.1:$Port. Start it first:  py server.py --behind-proxy" }

Write-Host 'Starting a temporary tunnel. Look for the https://....trycloudflare.com address below. Ctrl+C to stop.'
& $cf tunnel --no-autoupdate --url "http://127.0.0.1:$Port"
