@echo off
REM Brief Live — the morning brief as a live dashboard.
REM Double-click this, then the browser opens on its own.
cd /d "%~dp0"
start "" http://localhost:8800
.venv\Scripts\python.exe dashboard\server.py
pause
