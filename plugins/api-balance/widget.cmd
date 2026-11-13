@echo off
setlocal
set "PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
start "" "%PY%" "%~dp0widget_v2.py" --plugin-config "%USERPROFILE%\.codex\config.toml"
