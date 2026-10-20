#!/bin/bash
# 捡漏雷达 · Mac 每小时运行一次（由 launchd 调用）：抓取 → 更新数据 → 推送到 GitHub
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p .local
LOG=.local/run.log
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S')"
  [ -f .local/radar.env ] && { set -a; . .local/radar.env; set +a; }
  git pull -q --rebase --autostash origin main || echo "git pull 失败"
  FEED=.local/feed.xml
  rm -f "$FEED"
  if curl -sS -L -m 30 -A "BargainRadar/1.0 (personal RSS reader)" -o "$FEED" "https://faxian.smzdm.com/feed"; then
    export RADAR_FEED_FILE="$FEED"
  else
    echo "curl 下载失败"
    export RADAR_FEED_FILE=".local/feed-missing.xml"
  fi
  /usr/bin/python3 scripts/update.py
  git add data
  if ! git diff --cached --quiet; then
    git commit -q -m "数据更新 $(date '+%Y-%m-%d %H:%M')" && git push -q origin main && echo "已推送" || echo "git push 失败"
  else
    echo "没有变化"
  fi
} >> "$LOG" 2>&1
# 日志只保留最近 3000 行
tail -n 3000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
