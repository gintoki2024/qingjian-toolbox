@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
if exist "%~dp0程序文件\launcher\prepare_python.cmd" (
  set "QJ_CMD_PROJECT=%~dp0程序文件"
) else if exist "%~dp0prepare_python.cmd" (
  for %%I in ("%~dp0..") do set "QJ_CMD_PROJECT=%%~fI"
) else (
  echo 未找到程序文件。请完整解压源码包后重试。
  exit /b 2
)
set "QJ_CMD_RESULT=%QJ_CMD_PROJECT%\config\.cmd-python-%RANDOM%-%RANDOM%.txt"
set "QJ_CMD_PYTHON="
set "QJ_CMD_HOME="
set "QJ_CMD_NO_UI="
set "QJ_CMD_NO_DISCOVERY="
set "QJ_CMD_OFFLINE="
set "QJ_CMD_CHOOSE="
set "QJ_CMD_CHECK_ONLY="
:read_options
if "%~1"=="" goto prepare
if "%~1"=="--" goto prepare
if /i "%~1"=="--python" (
  set "QJ_CMD_PYTHON=%~2"
  shift
)
if /i "%~1"=="--runtime-home" (
  set "QJ_CMD_HOME=%~2"
  shift
)
if /i "%~1"=="--no-ui" set "QJ_CMD_NO_UI=1"
if /i "%~1"=="--check-only" (
  set "QJ_CMD_NO_UI=1"
  set "QJ_CMD_CHECK_ONLY=1"
)
if /i "%~1"=="--no-discovery" set "QJ_CMD_NO_DISCOVERY=1"
if /i "%~1"=="--offline" set "QJ_CMD_OFFLINE=1"
if /i "%~1"=="--choose-environment" set "QJ_CMD_CHOOSE=1"
shift
goto read_options
:prepare
call "%QJ_CMD_PROJECT%\launcher\prepare_python.cmd"
if errorlevel 1 exit /b %errorlevel%
if not exist "%QJ_CMD_RESULT%" exit /b 2
set /p "QJ_CMD_PYTHON=" < "%QJ_CMD_RESULT%"
del /q "%QJ_CMD_RESULT%" >nul 2>nul
if not defined QJ_CMD_PYTHON exit /b 2
set "QINGJIAN_CMD_START=1"
set "QINGJIAN_CMD_SELECTION_DONE=1"
"%QJ_CMD_PYTHON%" -E -X utf8 "%QJ_CMD_PROJECT%\launcher\cmd_source.py" %*
exit /b %errorlevel%
