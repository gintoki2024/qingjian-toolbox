@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
set "PYTHONHOME="
set "PYTHONPATH="
set "QT_PLUGIN_PATH="
set "QT_QPA_PLATFORM_PLUGIN_PATH="
if not exist "%~dp0程序文件\Python\python.exe" goto missing
if /i "%~1"=="--no-ui" goto wait
if /i "%~1"=="--check-only" goto wait
if /i "%~1"=="--prepare-only" goto wait
start "" /b "%~dp0程序文件\Python\pythonw.exe" -I -B -X utf8 "%~dp0程序文件\launcher\offline_cmd.py" %*
exit /b %errorlevel%
:wait
"%~dp0程序文件\Python\python.exe" -I -B -X utf8 "%~dp0程序文件\launcher\offline_cmd.py" %*
exit /b %errorlevel%
:missing
echo 未找到内置Python。请完整解压压缩包，保留“程序文件”文件夹后重试。
pause
exit /b 2
