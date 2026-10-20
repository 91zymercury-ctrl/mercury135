# 捡漏雷达 · GitHub 版

自动汇总什么值得买上的全网好价，按价格历史给出好价评分。网页是公开的，任何人打开链接就能看，不需要登录。

- **更新**：你的 Mac 每小时抓取一次，把数据推送到本仓库；GitHub Actions 收到推送后自动发布到 GitHub Pages。之所以不直接用 GitHub 抓，是因为什么值得买会拦截 GitHub 服务器的访问。Mac 合盖休眠时更新会暂停，醒来后自动补上。
- **网页**：`https://<你的用户名>.github.io/<仓库名>/`
- **历史**：价格记录按天保存在 `data/days/`，保留 60 天，网页显示最近 30 天。

## 目录
- `scripts/update.py`：抓取、去重、合并历史、生成网页数据、目标价推送（只用 Python 标准库）
- `scripts/mac_run.sh` / `scripts/mac_install.sh`：Mac 上每小时运行的抓取脚本和一键安装脚本
- `site/index.html`：网页（手机优先，支持深色模式）
- `.github/workflows/update.yml`：收到推送后发布网页
- `data/days/*.json`：每天的好价记录（自动提交）

## 在 Mac 上安装抓取器
1. 在 GitHub 创建令牌：Settings → Developer settings → Fine-grained tokens → Generate new token。Repository access 选 Only select repositories → mercury135；Permissions → Contents 选 Read and write。
2. 终端运行：`bash mac_install.sh`，按提示粘贴令牌。令牌只保存在 Mac 的钥匙串里。
3. 卸载：`bash ~/jianlou-radar/scripts/mac_install.sh --uninstall`

## 关键词目标价推送（可选）
编辑 Mac 上的 `~/jianlou-radar/.local/radar.env`（只在本机，不会上传）：
- `RADAR_TARGETS`：例如 `'[{"kw":"固态 2TB","max":600}]'`。标题同时包含所有关键词、且价格不高于 max 时推送。
- `BARK_URL`：iPhone 上的 Bark App 里复制的推送地址。
- `SERVERCHAN_SENDKEY`：（可选）Server酱 SendKey，推送到微信。

同一条爆料只推送一次。

## 手动更新
Mac 上运行 `bash ~/jianlou-radar/scripts/mac_run.sh`，可以立即抓取一次。运行记录在 `~/jianlou-radar/.local/run.log`。

## 说明
- 价格是爆料发布时的标题价，没有在商城页面核实。下单前请点“查看爆料”，去商城确认价格。
- “已观测最低价”只代表本雷达记录到的最低价，不等于商品的真实历史最低价。
