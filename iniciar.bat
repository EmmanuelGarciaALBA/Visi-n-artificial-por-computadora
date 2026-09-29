@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Creando entorno virtual...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe run.py
pause
