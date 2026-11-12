@echo off
REM Start the OpenRGB lamp test console (no black console window).
REM Usage: double-click this file, or: start "" tools\start_ui.cmd
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
start "" /b ".venv\Scripts\pythonw.exe" "%ROOT%\tools\ui.py" --host 127.0.0.1 --port 8760
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8760/"
endlocal
