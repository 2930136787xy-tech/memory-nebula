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
echo 记忆星云 · 账号模式：http://127.0.0.1:8767/
echo 局域网地址（发给同一 Wi-Fi 下的人）:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { Write-Host ('      http://' + $_.IPAddress + ':8767/   [' + $_.InterfaceAlias + ']') }"
echo 账号文件: .\accounts.json（web 目录之外，静态服务读不到）
echo 建账号:  account.bat add 你的QQ号
echo 注意：明文 HTTP，密码在链路上不加密，只在信任的局域网开。
%PY% -X utf8 viewer_server.py 8767 --host 0.0.0.0 --accounts
pause