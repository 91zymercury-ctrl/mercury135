#!/bin/bash
# 捡漏雷达 · Mac 抓取器一键安装。用法：bash mac_install.sh
# 卸载：bash ~/jianlou-radar/scripts/mac_install.sh --uninstall
set -u
REPO_URL="https://github.com/91zymercury-ctrl/mercury135.git"
GH_USER="91zymercury-ctrl"
DIR="$HOME/jianlou-radar"
LABEL="com.jianlou.radar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  rm -f "$PLIST"
  echo "已停止并移除自动运行。数据目录 $DIR 保留，不需要可以手动删除。"
  exit 0
fi

echo "== 捡漏雷达 Mac 抓取器安装 =="
if ! xcode-select -p >/dev/null 2>&1; then
  echo "需要先安装苹果命令行工具（git 和 python3）。在弹窗里点“安装”，装完后重新运行本脚本。"
  xcode-select --install 2>/dev/null
  exit 1
fi

echo "1/5 测试能否访问什么值得买……"
HEAD=$(curl -sS -L -m 30 -A "BargainRadar/1.0 (personal RSS reader)" https://faxian.smzdm.com/feed 2>&1 | head -c 3000)
if ! printf '%s' "$HEAD" | grep -q "<rss"; then
  echo "❌ 拿不到好价订阅源，返回的是："
  printf '%s\n' "$HEAD" | head -c 300; echo
  echo "请把上面这段发给 Claude。"
  exit 1
fi
echo "   ✅ 可以访问"

echo "2/5 GitHub 令牌（只保存在这台 Mac 的钥匙串里）"
read -r -s -p "   粘贴令牌后按回车（输入时屏幕不显示）：" TOKEN; echo
if [ -z "$TOKEN" ]; then echo "❌ 没有输入令牌"; exit 1; fi
printf 'protocol=https\nhost=github.com\n\n' | git credential-osxkeychain erase 2>/dev/null
printf 'protocol=https\nhost=github.com\nusername=%s\npassword=%s\n\n' "$GH_USER" "$TOKEN" | git credential-osxkeychain store
unset TOKEN

echo "3/5 下载仓库到 $DIR ……"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" -c credential.helper=osxkeychain pull -q --rebase origin main || { echo "❌ 更新仓库失败"; exit 1; }
else
  git -c credential.helper=osxkeychain clone -q "$REPO_URL" "$DIR" || { echo "❌ 下载仓库失败，检查网络或令牌"; exit 1; }
fi
cd "$DIR"
git config credential.helper osxkeychain
git config user.name "radar-bot"
git config user.email "radar-bot@users.noreply.github.com"
mkdir -p .local
if [ ! -f .local/radar.env ]; then
  cat > .local/radar.env <<'ENV'
# 捡漏雷达私有设置（只在这台 Mac 上，不会上传）
# 关键词目标价：标题同时包含 kw 里所有空格分隔的词、且价格不高于 max 时推送
# 例：RADAR_TARGETS='[{"kw":"固态 2TB","max":600},{"kw":"漫步者","max":200}]'
RADAR_TARGETS='[]'
# iPhone 安装 Bark App，复制里面的推送地址，例：BARK_URL='https://api.day.app/xxxx'
BARK_URL=''
# （可选）Server酱 SendKey，推送到微信
SERVERCHAN_SENDKEY=''
ENV
fi

echo "4/5 检查推送权限……"
git -c credential.helper=osxkeychain push -q --dry-run origin main 2>/tmp/jianlou_push_err || {
  echo "❌ 令牌没有推送权限："; cat /tmp/jianlou_push_err; echo "请确认令牌对 mercury135 有 Contents 读写权限。"; exit 1; }
echo "   ✅ 可以推送"

echo "5/5 设置每小时自动运行……"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/scripts/mac_run.sh</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" || { echo "❌ 设置自动运行失败"; exit 1; }

echo "   正在运行第一次……"
sleep 20
echo "----- 运行记录 -----"
tail -n 8 .local/run.log 2>/dev/null
echo "--------------------"
echo "✅ 安装完成！Mac 开着时每小时自动更新一次。"
echo "   查看运行记录：tail ~/jianlou-radar/.local/run.log"
echo "   设置关键词提醒：open -e ~/jianlou-radar/.local/radar.env"
echo "   网页：https://91zymercury-ctrl.github.io/mercury135/"
