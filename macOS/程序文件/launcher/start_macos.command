#!/bin/bash
# Standard macOS tools suffice until a fixed-hash portable Python is ready.
set -euo pipefail
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
unset PYTHONHOME PYTHONPATH QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH

fail() { printf '\n准备未完成：%s\n' "$*" >&2; exit 2; }
[ "$(/usr/bin/uname -s)" = Darwin ] || fail '此启动器用于 macOS。'
script_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
if [ "$(basename -- "$script_dir")" = launcher ]; then
    project="$(cd -- "$script_dir/.." && pwd -P)"
else
    project="$script_dir/程序文件"
fi
[ -f "$project/bootstrap_macos.py" ] || fail '请先完整解压，保留“程序文件”文件夹。'
manifest="$project/macos-runtime-sources.json"
[ -f "$manifest" ] || fail 'Mac 环境清单缺失，请重新完整解压。'
machine="$(/usr/bin/uname -m)"
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then machine=arm64; fi
case "$machine" in arm64|x86_64) ;; *) fail "不支持的处理器：$machine" ;; esac
release="$(/usr/bin/sw_vers -productVersion)"
major="${release%%.*}"
minor="${release#*.}"; minor="${minor%%.*}"
if [ "$major" -lt 14 ] || { [ "$major" -eq 14 ] && [ "$minor" -lt 4 ]; }; then
    fail '此版本需要 macOS 14.4 或更新系统。'
fi
json_get() { /usr/bin/plutil -extract "$2" raw -o - "$1" 2>/dev/null; }
choice_file="${QINGJIAN_MAC_SELECTION:-${QINGJIAN_USER_DATA:-$HOME/Library/Application Support/QingjianLocal}/macos-startup-selection.json}"
mode=''; selected_python=''; runtime_home="${QINGJIAN_RUNTIME_HOME:-}"
choose=0
offline="${QINGJIAN_OFFLINE:-0}"
explicit_mode=''; explicit_python=''; explicit_home=''
original_arguments=("$@")
while [ "$#" -gt 0 ]; do
    case "$1" in
        --choose-environment) choose=1; shift ;;
        --offline|--check-only|--check-environment) offline=1; shift ;;
        --home|--runtime-home)
            [ "$#" -ge 2 ] || fail '组件位置参数缺少值。'
            explicit_home="$2"; shift 2 ;;
        --python)
            [ "$#" -ge 2 ] || fail 'Python 位置参数缺少值。'
            explicit_python="$2"; explicit_mode=reuse; shift 2 ;;
        --managed-python) explicit_mode=managed; shift ;;
        --selection-mode)
            [ "$#" -ge 2 ] || fail '部署模式缺少值。'
            explicit_mode="$2"; shift 2 ;;
        --) break ;;
        *) shift ;;
    esac
done
set -- "${original_arguments[@]}"
if [ "$choose" -eq 0 ] && [ -f "$choice_file" ] && [ ! -L "$choice_file" ] &&
   [ "$(json_get "$choice_file" schema_version || true)" = 1 ] &&
   [ "$(json_get "$choice_file" architecture || true)" = "$machine" ]; then
    mode="$(json_get "$choice_file" mode || true)"
    selected_python="$(json_get "$choice_file" python || true)"
    runtime_home="${runtime_home:-$(json_get "$choice_file" runtime_home || true)}"
    case "$mode" in managed|reuse) ;; *) mode='';; esac
fi
[ -z "$explicit_home" ] || runtime_home="$explicit_home"
[ -z "$explicit_python" ] || selected_python="$explicit_python"
if [ -n "$explicit_mode" ]; then
    mode="$explicit_mode"
    case "$mode" in managed|reuse) ;; *) fail '部署模式只能是 managed 或 reuse。';; esac
    [ -n "$runtime_home" ] || fail '指定 Python 或独立部署时，请同时通过 --home 指定组件位置。'
fi

compatible_python() {
    [ -f "$1" ] && [ -x "$1" ] || return 1
    # /usr/bin/python3 can prompt to install developer tools; never invoke it.
    [ "$1" != /usr/bin/python3 ] || return 1
    "$1" -E -c 'import platform,sys;raise SystemExit(0 if sys.platform=="darwin" and (3,10)<=sys.version_info[:2]<(3,15) and platform.machine()==sys.argv[1] else 1)' "$machine" >/dev/null 2>&1
}

record=0
if [ -z "$mode" ]; then
    candidate=''
    for name in python3.13 python3.12 python3.14 python3.11 python3.10 python3; do
        path="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$path" ] && compatible_python "$path"; then candidate="$path"; break; fi
    done
    printf '\n青简工具箱 · Mac 首次准备\n'
    printf '1 复用已有 Python（只补缺失组件，同环境的其他程序可共用）\n'
    printf '2 独立部署 Python（可选择位置，不改系统环境）\n'
    [ -z "$candidate" ] || printf '发现可用 Python：%s\n' "$candidate"
    printf '请选择 [1]，或输入 Q 取消：'
    IFS= read -r answer || exit 130
    case "$answer" in
        q|Q) exit 130 ;;
        2) mode=managed ;;
        ''|1)
            mode=reuse
            selected_python="$candidate"
            if [ -z "$selected_python" ]; then
                printf '未发现可用 Python。请输入已有 Python 的绝对路径；回车改用独立部署：'
                IFS= read -r selected_python || exit 130
                [ -n "$selected_python" ] || mode=managed
            fi
            if [ "$mode" = reuse ]; then compatible_python "$selected_python" || fail '所选 Python 不可用，请重新选择。'; fi
            ;;
        *) fail '请选择 1、2 或 Q。' ;;
    esac
    suggested="${runtime_home:-$(cd -- "$project/../.." && pwd -P)/青简共享环境}"
    printf '转换引擎、缓存和日志将放在源码包外，更新源码不会重复下载。\n'
    printf '组件位置（回车采用 %s）：' "$suggested"
    IFS= read -r answer || exit 130
    runtime_home="${answer:-$suggested}"
    case "$runtime_home" in /*) ;; *) fail '部署位置必须是绝对路径。';; esac
    printf '使用位置：%s\n首次补缺需要联网。输入 Y 确认，其他输入取消：' "$runtime_home"
    IFS= read -r answer || exit 130
    case "$answer" in y|Y) ;; *) exit 130 ;; esac
    record=1
fi
case "$runtime_home" in /*) ;; *) fail '保存的组件位置无效，请用 --choose-environment 重新选择。';; esac
[ ! -L "$runtime_home" ] || fail '部署根目录不能是符号链接。'

if [ "$mode" = managed ]; then
    selected_python="$runtime_home/python/$machine/bin/python3"
    if ! compatible_python "$selected_python"; then
        [ "$offline" != 1 ] || fail 'Mac Python 尚未准备完整；离线检查未下载任何组件。'
        artifact="python.$machine"
        filename="$(json_get "$manifest" "$artifact.filename")"
        expected="$(json_get "$manifest" "$artifact.sha256")"
        expected_size="$(json_get "$manifest" "$artifact.bytes")"
        case "$filename" in ''|*[!A-Za-z0-9_.+%-]*|*..*) fail 'Python 文件名无效。';; esac
        [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail 'Python SHA256 无效。'
        [[ "$expected_size" =~ ^[1-9][0-9]*$ ]] || fail 'Python 文件大小无效。'
        target="$runtime_home/python/$machine"
        [ ! -e "$target" ] || fail "Python 目录不完整，已保留原文件。请备份后处理或选择其他位置：$target"
        /bin/mkdir -p -- "$runtime_home/downloads" "$runtime_home/python"
        lock="$runtime_home/.python-shell-lock"
        /bin/mkdir -- "$lock" 2>/dev/null || fail '另一启动器正在准备 Python；若上次异常退出，请确认没有准备任务后移除 .python-shell-lock 目录。'
        stage=''
        cleanup() {
            if [ -n "$stage" ] && [ -d "$stage" ] && [ ! -L "$stage" ]; then
                case "$stage" in "$runtime_home"/python/.stage-*) /bin/rm -rf -- "$stage" ;; esac
            fi
            /bin/rmdir -- "$lock" 2>/dev/null || true
        }
        trap cleanup EXIT
        archive="$runtime_home/downloads/$filename"
        verified() {
            [ -f "$1" ] && [ "$(/usr/bin/stat -f %z "$1")" = "$expected_size" ] &&
                [ "$(/usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}')" = "$expected" ]
        }
        if ! verified "$archive"; then
            ok=0
            for number in 0 1 2; do
                url="$(json_get "$manifest" "$artifact.urls.$number.url" || true)"
                [ -n "$url" ] || continue
                case "$url" in https://*) ;; *) fail '下载清单必须使用 HTTPS。';; esac
                source_name="$(json_get "$manifest" "$artifact.urls.$number.name")"
                printf '正在通过 %s 获取 Mac Python…\n' "$source_name"
                part="$archive.part"
                if /usr/bin/curl --fail --location --proto '=https' --proto-redir '=https' \
                    --connect-timeout 20 --max-time 240 --retry 1 --output "$part" "$url" && verified "$part"; then
                    /bin/mv -- "$part" "$archive"; ok=1; break
                fi
                /bin/rm -f -- "$part"
                printf '%s 下载或校验未通过。\n' "$source_name"
            done
            [ "$ok" -eq 1 ] || fail '所有 Python 下载源均未成功；没有使用未校验文件，请联网后重试。'
        fi
        # The archive is pinned by both size and SHA256 before extraction.
        /usr/bin/tar -tzf "$archive" | /usr/bin/awk '
            /^\// || /(^|\/)\.\.($|\/)/ || !/^python(\/|$)/ {bad=1} END {exit bad}' || fail 'Python 压缩包路径不安全。'
        stage="$(/usr/bin/mktemp -d "$runtime_home/python/.stage-XXXXXX")"
        /usr/bin/tar -xzf "$archive" -C "$stage" --strip-components 1
        compatible_python "$stage/bin/python3" || fail '下载的 Python 无法在当前 Mac 运行。'
        "$stage/bin/python3" -E -c 'import json,sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(json.dumps({"kind":"python-build-standalone","architecture":sys.argv[2],"sha256":sys.argv[3],"size":int(sys.argv[4]),"version":"3.13.18"}),encoding="utf-8");p.chmod(0o600)' \
            "$stage/.qingjian-runtime.json" "$machine" "$expected" "$expected_size"
        /bin/mv -- "$stage" "$target"
        stage=''
        cleanup
        trap - EXIT
    fi
else
    compatible_python "$selected_python" || fail '原来选择的 Python 不可用，请用 --choose-environment 重新选择。'
fi
arguments=(--home "$runtime_home" --python "$selected_python" --selection-mode "$mode")
[ "$mode" != managed ] || arguments+=(--managed-python)
[ "$record" -ne 1 ] || arguments+=(--record-selection)
exec "$selected_python" -E -X utf8 -B "$project/bootstrap_macos.py" "${arguments[@]}" "$@"
