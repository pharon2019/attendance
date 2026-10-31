# Registers a Windows scheduled task that starts the attendance server (hidden, no console window)
# when this user logs in, and restarts it if it crashes.
#   Install:   powershell -ExecutionPolicy Bypass -File autostart.ps1
#   Remove:    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
#   At boot (before any login, needs an Administrator PowerShell):  ... autostart.ps1 -AtBoot
#   Online via Cloudflare Tunnel (see online.ps1):                     ... autostart.ps1 -BehindProxy
param([switch]$Remove, [switch]$AtBoot, [switch]$BehindProxy)

$name = 'Attendance Server'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task '$name'."
    return
}

$python = & py -c "import sys; print(sys.executable)"
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ("`"$dir\server.py`" --port 8000" + $(if ($BehindProxy) { ' --behind-proxy' } else { '' })) -WorkingDirectory $dir
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

if ($AtBoot) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Attendance system server (server.py) on port 8000' -Force | Out-Null
Write-Host "Task '$name' registered ($(if ($AtBoot) {'at boot'} else {'at logon'}))."
