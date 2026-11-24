@echo off
REM ============================================================
REM  Tracciato - build dell'eseguibile Windows
REM  Requisiti: Python 3.12 (64 bit) installato da python.org
REM  Risultato: dist\Tracciato\Tracciato.exe
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist .venv (
  echo [1/4] Creo l'ambiente virtuale .venv
  py -3.12 -m venv .venv 2>nul || python -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat || goto :err

echo [2/4] Installo le dipendenze
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :err

if not exist assets\tracciato.ico python tools\make_icon.py || goto :err

echo [3/4] Verifica del motore (selftest)
python run_tracciato.py --selftest || goto :err

echo [4/4] Creo l'eseguibile
pyinstaller --noconfirm --clean tracciato.spec || goto :err

echo.
echo  Fatto: dist\Tracciato\Tracciato.exe
echo  Per distribuirlo copia l'intera cartella dist\Tracciato (anche su NAS).
echo  Verifica: dist\Tracciato\TracciatoCLI.exe --selftest
exit /b 0

:err
echo.
echo  ERRORE durante la build. Controlla i messaggi sopra.
exit /b 1
