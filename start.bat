@echo off
REM One-click start on Windows. Starts Nazar (every sector radar + the hub), waits for it, then opens it in your browser.
REM For the single-sector Pebble radar only:  python run.py --radar
cd /d "%~dp0"
python run.py
if errorlevel 1 (
  echo.
  echo Something went wrong. The message above says what. Common fixes:
  echo   - Python not found: install from python.org and tick "Add to PATH"
  echo   - Missing packages: run  python -m pip install -r requirements-full.txt
  pause
)
