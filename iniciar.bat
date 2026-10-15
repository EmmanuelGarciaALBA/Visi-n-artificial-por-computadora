@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Creando entorno virtual con Python 3.14...
  py -3.14 -m venv .venv || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe run.py
pause
