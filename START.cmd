@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting safe-monitor demo...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_demo.ps1" %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" (
  echo [FAILED] exit code %CODE%. Check the messages above or data\logs\*.err
) else (
  echo [OK] Dashboard: http://localhost:3000
  echo      Trigger:   DEMO.cmd
  echo      Stop:      STOP.cmd
)
pause
