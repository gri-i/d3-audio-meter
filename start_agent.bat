@echo off
rem Starts the Audio Meter agent in the background (no console window).
rem Log: agent\agent.log
cd /d "%~dp0"
netstat -ano | findstr /r /c:":8765 .*LISTENING" >nul && (
  echo Audio Meter agent is already running on port 8765.
  timeout /t 3 /nobreak >nul 2>&1 <nul
  exit /b 0
)
start "" /b pyw -3.11 -u agent\audio_meter.py > agent\agent.log 2>&1
echo Audio Meter agent started: http://localhost:8765/
timeout /t 3 /nobreak >nul 2>&1 <nul
