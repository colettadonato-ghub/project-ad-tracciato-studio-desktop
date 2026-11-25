@echo off
REM Avvia AD - Tracciato Studio Pro da sorgente (dopo aver eseguito build.bat almeno una volta)
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python run_tracciato.py --debug
