# Starts the collector supervisor in a hidden window, detached from this terminal.
$root = Split-Path -Parent $PSScriptRoot
$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run-forever.cmd*' }
if ($running) { Write-Output "already running (pid $($running.ProcessId -join ', '))"; exit 0 }
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$root\scripts\run-forever.cmd`"" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 2
$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run-forever.cmd*' }
Write-Output "started supervisor pid $($p.ProcessId -join ', '). log: $root\data\collect.log"
