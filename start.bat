@echo off
:: Agent System restart script
echo [Agent System] Stopping existing processes...
wmic process where "name='python.exe' and commandline like '%%main.py%%'" delete >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
echo [Agent System] Starting...
cd /d %~dp0
start "AgentSystem" /MIN python main.py
echo [Agent System] Started. Check logs\jarvis.log for status.
