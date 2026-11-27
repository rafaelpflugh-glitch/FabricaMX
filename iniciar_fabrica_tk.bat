@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto :run

echo [ERRO] Ambiente virtual .venv nao encontrado em: !CD!
echo Execute: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
pause
exit /b 1

:run
start "" ".venv\Scripts\pythonw.exe" "app.py"
