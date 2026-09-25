@echo off
rem Installs the Audio Meter agent's Python dependencies from the vendored
rem wheels in agent\vendor (no internet access needed) and starts the agent.
rem Requires Python 3.11 64-bit (the "py" launcher must know about it).
cd /d "%~dp0"

py -3.11 -c "" >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 x64 was not found ^(py -3.11^).
  echo Install it from https://www.python.org/downloads/release/python-3119/
  echo ^(Windows installer, 64-bit^), then run this script again.
  pause
  exit /b 1
)

echo Installing dependencies from agent\vendor ...
py -3.11 -m pip install --no-index --find-links agent\vendor -r agent\requirements.txt
if errorlevel 1 (
  echo Dependency install failed - see the messages above.
  pause
  exit /b 1
)

echo Dependencies installed.
call "%~dp0start_agent.bat"
