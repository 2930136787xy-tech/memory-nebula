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
echo 记忆星云 · 只读：http://127.0.0.1:8766/
echo 局域网地址（发给同一 Wi-Fi 下的人）:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { Write-Host ('      http://' + $_.IPAddress + ':8766/   [' + $_.InterfaceAlias + ']') }"
echo 访客只能看到球体概况，看不到消息/记忆正文，也不能改。
echo 注意：这是明文 HTTP，只在你信任的网络里开。
%PY% -X utf8 viewer_server.py 8766 --host 0.0.0.0 --guest
pause