#!/bin/bash
# Offline distribution: use only the interpreter stored beside this launcher.
set -u
TASK_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
TASK_PYTHON="$TASK_DIR/程序文件/Python/bin/python3"
if [ ! -x "$TASK_PYTHON" ]; then
  printf '青简工具箱：包内 Python 不完整。请重新完整解压下载包。\n'
  read -r -p '按回车关闭。' _task_answer
  exit 2
fi
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
export PYTHONNOUSERSITE=1 QINGJIAN_OFFLINE=1
"$TASK_PYTHON" -I -B "$TASK_DIR/程序文件/macos_build/offline_start.py" "$@"
TASK_RESULT=$?
if [ "$TASK_RESULT" -ne 0 ]; then
  printf '\n启动未完成（退出码 %s），请查看上方提示。\n' "$TASK_RESULT"
  read -r -p '按回车关闭。' _task_answer
fi
exit "$TASK_RESULT"
