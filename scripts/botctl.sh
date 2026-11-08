#!/usr/bin/env bash
# Linux 单机管理：持有 flock 的进程直接 exec Python，PID 与锁的生命周期一致。
set -eu
umask 0077

BOT_PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
BOT_CONTROL="$BOT_PROJECT_DIR/scripts/botctl.sh"
BOT_RUN_DIR="$BOT_PROJECT_DIR/run"
BOT_PID_FILE="$BOT_RUN_DIR/bot.pid"
BOT_PYTHON="$BOT_PROJECT_DIR/.venv/bin/python"

read_pid() {
    test -f "$BOT_PID_FILE" || return 1
    BOT_PID="$(cat -- "$BOT_PID_FILE")"
    case "$BOT_PID" in ''|*[!0-9]*) return 1 ;; esac
    test "$BOT_PID" -gt 1
}

is_running() {
    read_pid || return 1
    kill -0 "$BOT_PID" 2>/dev/null || return 1
    BOT_PROCESS_STATE="$(ps -p "$BOT_PID" -o stat= 2>/dev/null)" || return 1
    case "$BOT_PROCESS_STATE" in *Z*) return 1 ;; esac
    BOT_PROCESS_COMMAND="$(ps -ww -p "$BOT_PID" -o args= 2>/dev/null)" || return 1
    # 编辑器也可能打开 main.py；必须匹配本脚本的完整启动命令才可发停止信号。
    test "$BOT_PROCESS_COMMAND" = "$BOT_PYTHON -u $BOT_PROJECT_DIR/main.py"
}

start_bot() {
    if is_running; then
        printf '机器人已在运行，PID=%s\n' "$BOT_PID"
        return 0
    fi
    command -v flock >/dev/null 2>&1 || {
        printf '缺少 flock，请安装 util-linux。\n' >&2
        return 1
    }
    test -x "$BOT_PYTHON" || {
        printf '请先创建项目的 .venv 环境（支持 Conda）并安装 requirements.txt，详见 DEPLOYMENT.md。\n' >&2
        return 1
    }
    mkdir -p -- "$BOT_RUN_DIR"
    # 预检前就取锁，后台子进程继承同一描述符，避免并发启动和轮转冲突。
    exec 9>"$BOT_RUN_DIR/bot.lock"
    flock -n 9 || {
        printf '另一实例已持有运行锁，请先确认旧实例状态。\n' >&2
        return 1
    }
    # 前台预检显示配置、依赖或日志权限错误，后台输出才能安全重定向到 /dev/null。
    "$BOT_PYTHON" "$BOT_PROJECT_DIR/main.py" --check-logging || return 1
    nohup bash "$BOT_CONTROL" _run </dev/null >/dev/null 2>&1 &
    BOT_STARTED_PID=$!
    BOT_START_CHECK=0
    while test "$BOT_START_CHECK" -lt 30; do
        if is_running && test "$BOT_PID" = "$BOT_STARTED_PID"; then
            printf '机器人进程已启动，PID=%s；请查看日志并在群内验证回复。\n' "$BOT_PID"
            return 0
        fi
        if ! kill -0 "$BOT_STARTED_PID" 2>/dev/null; then
            wait "$BOT_STARTED_PID" 2>/dev/null || true
            break
        fi
        sleep 0.1
        BOT_START_CHECK=$((BOT_START_CHECK + 1))
    done
    printf '未能确认启动；请检查滚动日志，或前台运行 .venv/bin/python main.py 查看原因。\n' >&2
    return 1
}

stop_bot() {
    if ! is_running; then
        printf '机器人未运行。\n'
        return 0
    fi
    BOT_STOP_PID="$BOT_PID"
    kill -TERM "$BOT_STOP_PID"
    printf '正在停止，等待在途任务完成，PID=%s……\n' "$BOT_STOP_PID"
    BOT_STOP_CHECK=0
    while test "$BOT_STOP_CHECK" -lt 300; do
        if ! is_running; then
            # 已运行另一实例时保留它的 PID 文件，避免并发管理误删。
            if read_pid && test "$BOT_PID" = "$BOT_STOP_PID"; then
                rm -f -- "$BOT_PID_FILE"
            fi
            printf '机器人已停止。\n'
            return 0
        fi
        if test "$BOT_PID" != "$BOT_STOP_PID"; then
            printf '检测到进程已更换，请重新检查状态。\n' >&2
            return 1
        fi
        sleep 1
        BOT_STOP_CHECK=$((BOT_STOP_CHECK + 1))
    done
    printf '等待超过 300 秒，进程仍在运行；请检查日志，本脚本不会强杀或启动第二实例。\n' >&2
    return 1
}

case "${1:-}" in
    start) start_bot ;;
    stop) stop_bot ;;
    restart) stop_bot && start_bot ;;
    status)
        if is_running; then
            printf '机器人进程正在运行，PID=%s；此状态不代表飞书或模型服务可用。\n' "$BOT_PID"
        else
            printf '机器人未运行。\n'
            exit 1
        fi
        ;;
    _run)
        # fd 9 由 start 打开并持锁，nohup/bash/exec Python 全程继承。
        flock -n 9 || exit 1
        printf '%s\n' "$$" >"$BOT_PID_FILE"
        cd -- "$BOT_PROJECT_DIR"
        exec "$BOT_PYTHON" -u "$BOT_PROJECT_DIR/main.py"
        ;;
    *) printf '用法：bash scripts/botctl.sh start|stop|status|restart\n' >&2; exit 2 ;;
esac
