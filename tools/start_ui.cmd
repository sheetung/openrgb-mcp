@echo off
REM 启动状态灯网页测试台（不弹出黑色控制台窗口）
REM 用法：双击运行，或 start "" tools\start_ui.cmd
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
start "" /b ".venv\Scripts\pythonw.exe" "%ROOT%\tools\ui.py" --host 127.0.0.1 --port 8760
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8760/"
endlocal
