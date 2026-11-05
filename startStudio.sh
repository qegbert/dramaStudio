#!/usr/bin/env zsh
# 一键启动 DramaForge Studio (自主版权 AI 短剧工业化工作台)
cd "$(dirname "$0")"
export DRAMA_STUDIO_PORT="${DRAMA_STUDIO_PORT:-8688}"
echo "🚀 正在启动 DramaForge Studio -> http://127.0.0.1:${DRAMA_STUDIO_PORT}"
python3 serverApp.py
