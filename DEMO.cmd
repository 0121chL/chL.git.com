@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Injecting ch4_rise anomaly...
python -X utf8 sensor_simulator.py --inject ch4_rise
echo.
echo Watch the dashboard: http://localhost:3000
pause
