# Stops the supervisor and the collector it runs. The database keeps everything; start.ps1 resumes.
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run-forever.cmd*' -or $_.CommandLine -like '*-m sniper collect*' }
if (-not $procs) { Write-Output "not running"; exit 0 }
foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Output "stopped pid $($p.ProcessId)" } catch {} }
