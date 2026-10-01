@echo off
rem New Pair Bouncer supervisor: runs the collector (and the bouncer) and restarts it if it ever exits.
rem Credits are capped per UTC day in the database (max_credits_per_day in sniper.yaml); past the cap the collector pauses until the next day.
rem Start it hidden with scripts\start.ps1, stop it with scripts\stop.ps1. Log: data\collect.log
cd /d "%~dp0.."
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
if not exist data mkdir data
:loop
echo [%date% %time%] supervisor: starting collector>> data\collect.log
.venv\Scripts\python.exe -m sniper collect >> data\collect.log 2>&1
set CODE=%errorlevel%
echo [%date% %time%] supervisor: collector exited with code %CODE%>> data\collect.log
if "%CODE%"=="3" goto end
ping -n 31 127.0.0.1 >nul
goto loop
:end
echo [%date% %time%] supervisor: credit budget reached, not restarting>> data\collect.log
