@echo off
chcp 65001 >nul
title C22S Hotel Camera Hub (Zero-CPU H.265 Engine)
cd /d "%~dp0"

echo ===========================================================================
echo   C22S HOTEL CAMERA HUB - ZERO-CPU H.265 NVR ^& SMART DASHBOARD
echo ===========================================================================
echo   [+] Dashboard Web UI : http://127.0.0.1:8080
echo   [+] RTSP Server      : rtsp://127.0.0.1:8554/camera-1 ...
echo   [+] Recordings Dir   : %~dp0recordings
echo ===========================================================================
echo.

REM Open the browser after 3 seconds in background
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8080"

REM Run the Camera Hub engine
python camera_hub.py %*
pause
