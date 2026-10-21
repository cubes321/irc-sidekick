@echo off
rem Start the IRC client from its venv. Extra arguments pass through, e.g. start.bat -c other.toml
setlocal
rem config.toml and the log database are found relative to the working directory
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv found. Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e .[dev]
    popd
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m llm_irc %*
set "rc=%errorlevel%"
popd

rem Keep the window open on an error when launched by double-click
if not "%rc%"=="0" pause
exit /b %rc%
