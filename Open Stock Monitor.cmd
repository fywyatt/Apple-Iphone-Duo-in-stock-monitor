@echo off
setlocal
cd /d "%~dp0"
set "MONITOR_PYTHON="
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 set "MONITOR_PYTHON=py -3"
if defined MONITOR_PYTHON goto timezone
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 set "MONITOR_PYTHON=python"
if defined MONITOR_PYTHON goto timezone
echo Install Python 3.11 or later from https://www.python.org/downloads/ and open this file again.
pause
exit /b 1

:timezone
%MONITOR_PYTHON% -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Los_Angeles')" >nul 2>&1
if not errorlevel 1 goto start
echo Installing Python's time zone data for the dashboard...
%MONITOR_PYTHON% -m pip install --user tzdata
if not errorlevel 1 goto start
echo Time zone setup failed. Check your connection and open this file again.
pause
exit /b 1

:start
%MONITOR_PYTHON% -B apple_stock_web.py
if errorlevel 1 pause
