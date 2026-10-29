@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [错误] 没找到 Python 3。请安装 Python 3.9+，安装时勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
%PY% -X utf8 accounts.py %*
pause