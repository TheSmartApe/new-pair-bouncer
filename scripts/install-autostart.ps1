# Starts the bouncer automatically when you log in to Windows: drops a tiny hidden launcher in your
# Startup folder that runs scripts\start.ps1. No admin rights needed. start.ps1 does nothing if the
# bot is already running. Undo with scripts\uninstall-autostart.ps1.
$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$launcher = Join-Path $startup 'new-pair-bouncer.vbs'
$cmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\start.ps1`""
$lines = @(
    "' New Pair Bouncer autostart (created by scripts\install-autostart.ps1)",
    'Set sh = CreateObject("WScript.Shell")',
    ('sh.Run "' + $cmd.Replace('"', '""') + '", 0, False')
)
Set-Content -Path $launcher -Value $lines -Encoding ASCII
Write-Output "autostart installed: $launcher"
