@echo off
rem Starts the Pigeon Deterrent dashboard server and opens it in your browser.
rem Double-click this file (or the "Pigeon Deterrent" desktop shortcut).
rem The server runs in its own window titled "Pigeon Deterrent server"; close that window to stop it.
rem It listens on 127.0.0.1 only, so nothing outside this computer can reach it.

rem Work from the folder this file is in, wherever it was started from.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo The "uv" tool was not found. Install it or open a new terminal so it is on the PATH.
    pause
    exit /b 1
)

rem Is something already listening on port 8000? Then do not start a second server.
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 (
    echo Port 8000 is already in use, so a second server was NOT started.
    echo The Pigeon Deterrent server may already be running: open http://127.0.0.1:8000/
    echo Otherwise close the program that is using port 8000 and run this file again.
    pause
    exit /b 1
)

start "Pigeon Deterrent server" cmd /k uv run uvicorn deterrent.api.app:create_app --factory --host 127.0.0.1 --port 8000

rem Give the server about 4 seconds to start, then open the dashboard.
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8000/
