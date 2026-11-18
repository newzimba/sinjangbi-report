@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Sinjangbi Report] Starting web app at http://127.0.0.1:8000
echo Close this window to stop the app.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
python -m uvicorn --app-dir . webapp.app:app --host 127.0.0.1 --port 8000
pause
