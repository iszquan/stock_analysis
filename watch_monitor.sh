#!/bin/bash
# 外部 supervisor: 防 monitor.py 卡死/网络阻塞导致仪表盘挂掉
# 用法: nohup bash watch_monitor.sh >> watch_monitor.log 2>&1 &

BASE=/Users/29/Projects/deepseek/Stock
LOG=$BASE/monitor.log
PORT=8899
INTERVAL=30

while true; do
    # 1) 端口是否响应
    RESP=$(python3 -c "
import urllib.request, sys
try:
    r=urllib.request.urlopen('http://127.0.0.1:$PORT/', timeout=2)
    sys.exit(0 if r.status==200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null)
    OK=$?

    # 2) 进程是否存在
    PID=$(pgrep -f 'monitor\.py' | grep -v grep | head -n1 || true)

    if [ "$OK" -ne 0 ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️ 端口 $PORT 无响应 (OK=$OK PID=${PID:-无}) → 重新启动"
        [ -n "$PID" ] && kill -9 "$PID" 2>/dev/null || true
        sleep 1
        nohup python3 $BASE/monitor.py >> $LOG 2>&1 &
        echo "[$(date '+%H:%M:%S')] ✅ 已重启 (PID=$(pgrep -f 'monitor\.py' | grep -v grep | head -n1))"
    else
        # 正常时静默，只有每 5 分钟输出一次心跳，避免日志膨胀
        if [ $(( $(date +%s) % 300 )) -lt 30 ]; then
            echo "[$(date '+%H:%M:%S')] 心跳 OK PID=$PID"
        fi
    fi

    sleep $INTERVAL
done
