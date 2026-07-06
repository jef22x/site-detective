@echo off
rem SiteDetective launcher: starts the app (if not already running) and opens the UI.
setlocal
cd /d "%~dp0"
set PORT=8321

rem Already running? Just open the browser.
netstat -an | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if %errorlevel%==0 goto open

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: creating virtual environment...
    python -m venv .venv || goto err
)

rem Keep dependencies and browsers in sync on every launch (fast no-op when current).
echo Checking dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto err
.venv\Scripts\python.exe -m playwright install chromium || goto err

rem Fail fast with a readable error if the app can't even be imported.
.venv\Scripts\python.exe -c "import app.main" || goto err

echo Starting SiteDetective on port %PORT%...
start "SiteDetective server" /min .venv\Scripts\python.exe -m uvicorn app.main:app --port %PORT%

rem Wait up to ~30s for the server to come up.
set /a tries=0
:wait
timeout /t 1 /nobreak >nul
netstat -an | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if %errorlevel%==0 goto open
set /a tries+=1
if %tries% lss 30 goto wait
echo Server did not start within 30 seconds. Check the "SiteDetective server" window for errors.
pause
exit /b 1

:open
start "" http://127.0.0.1:%PORT%
exit /b 0

:err
echo Setup failed. See the error above. Ensure Python 3.11+ is installed and on PATH.
pause
exit /b 1

