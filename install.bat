@echo off
rem  Install pcbmerge on Windows.  Double-click this, or run it from a prompt.
rem
rem  All it does is find a Python and hand over to install.py, which is where
rem  the checking and the explaining live.  Anything passed here is passed on,
rem  so `install.bat --dev` and `install.bat --uninstall` both work.

setlocal
cd /d "%~dp0"

rem  The py launcher is the right way to ask Windows for a Python, because it
rem  knows about every version installed rather than whichever one happens to
rem  be first on PATH.  Fall back to a bare python for machines without it.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"

if not defined PY (
    echo.
    echo No Python was found on this machine.
    echo.
    echo pcbmerge needs Python 3.10 or newer. Get it from
    echo     https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" while installing.
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0install.py" %*
set "CODE=%ERRORLEVEL%"

echo.
pause
exit /b %CODE%
