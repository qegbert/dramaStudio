#!/usr/bin/env zsh
# 一键启动 DramaForge Studio (自主版权 AI 短剧工业化工作台)
cd "$(dirname "$0")"
export DRAMA_STUDIO_PORT="${DRAMA_STUDIO_PORT:-8688}"

# 自动清理同端口残留的旧服务进程，避免 Address already in use
OLD_PIDS=$(lsof -ti tcp:${DRAMA_STUDIO_PORT} 2>/dev/null)
if [ -n "$OLD_PIDS" ]; then
  echo "🔄 正在停止端口 ${DRAMA_STUDIO_PORT} 上的旧进程 (${OLD_PIDS})..."
  kill -9 ${=OLD_PIDS} 2>/dev/null || true
  sleep 0.4
fi

echo "🚀 正在启动 DramaForge Studio -> http://127.0.0.1:${DRAMA_STUDIO_PORT}"
exec python3 serverApp.py
