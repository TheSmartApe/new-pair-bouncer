# Removes the login autostart created by scripts\install-autostart.ps1. Does not stop a running bot.
$launcher = Join-Path ([Environment]::GetFolderPath('Startup')) 'new-pair-bouncer.vbs'
if (Test-Path $launcher) { Remove-Item $launcher -Force; Write-Output "autostart removed: $launcher" } else { Write-Output "no autostart installed" }
