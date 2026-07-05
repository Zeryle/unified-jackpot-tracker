@echo off
REM start_hidden_wrapper.bat — invoked by start_hidden.vbs.
REM Always captures stdout/stderr to data\tracker_stdout.log even
REM when the launcher uses pythonw (which detaches from console).
REM This is the "leave it running 8h unattended" entry point.

setlocal
set "TRACKER_DIR=%~dp0"
set "LOG_FILE=%TRACKER_DIR%data\tracker_stdout.log"

if not exist "%TRACKER_DIR%data" mkdir "%TRACKER_DIR%data"

REM Pick the first pythonw.exe we can find on PATH.
where pythonw.exe >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_EXE=pythonw.exe"
) else (
    where python.exe >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_EXE=python.exe"
    ) else (
        echo ERROR: python.exe not found in PATH > "%LOG_FILE%"
        exit /b 1
    )
)

REM Run the tracker. pythonw detaches cleanly; python.exe (console
REM variant) keeps a log file via redirection.
echo [%date% %time%] Starting tracker with %PY_EXE% > "%LOG_FILE%"
echo [%date% %time%] Working dir: %TRACKER_DIR% >> "%LOG_FILE%"

REM Start /B = no new window. pythonw naturally has no console;
REM python.exe (console variant) — for that case we still get a log
REM because the redirect happens BEFORE the start.
if /I "%PY_EXE%"=="python.exe" (
    start /B "" python.exe "%TRACKER_DIR%app.py" >> "%LOG_FILE%" 2>&1
) else (
    start /B "" pythonw.exe "%TRACKER_DIR%app.py"
)

REM Give Flask time to bind. pythonw + Playwright + werkzeug import
REM is slow on cold start; 6s is enough on a typical machine. If
REM port 5050 is still not listening, fall through — the tracker
REM might still be loading. The user will see "page didn't load" if
REM something is really broken and check the log.
timeout /t 6 /nobreak >nul 2>&1
netstat -ano | findstr ":5050.*LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] WARNING: port 5050 not listening after 6s. >> "%LOG_FILE%"
    echo [%date% %time%] Tracker may still be loading. Open the URL in a few seconds. >> "%LOG_FILE%"
) else (
    echo [%date% %time%] OK: port 5050 is listening. Tracker ready. >> "%LOG_FILE%"
)
endlocal