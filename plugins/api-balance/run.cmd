@echo off
setlocal
set "PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
if "%~1"=="" (
  "%PY%" "%~dp0server.py" balance
) else (
  "%PY%" "%~dp0server.py" %*
)
