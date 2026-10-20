# 捡漏雷达 · GitHub 版

自动汇总什么值得买上的全网好价，按价格历史给出好价评分。网页是公开的，任何人打开链接就能看，不需要登录。

- **更新**：GitHub Actions 每小时自动抓取一次（免费），抓完自动发布到 GitHub Pages。
- **网页**：`https://<你的用户名>.github.io/<仓库名>/`
- **历史**：价格记录按天保存在 `data/days/`，保留 60 天，网页显示最近 30 天。

## 目录
- `scripts/update.py`：抓取、去重、合并历史、生成网页数据、目标价推送（只用 Python 标准库）
- `site/index.html`：网页（手机优先，支持深色模式）
- `.github/workflows/update.yml`：每小时运行的自动任务
- `data/days/*.json`：每天的好价记录（自动提交）

## 关键词目标价推送（可选）
在仓库 **Settings → Secrets and variables → Actions → New repository secret** 里添加。Secrets 不会公开，别人看不到你的关键词。

| 名称 | 内容 |
|---|---|
| `RADAR_TARGETS` | JSON 数组，例如 `[{"kw":"固态 2TB","max":600},{"kw":"漫步者","max":200}]`。标题同时包含 kw 里所有空格分隔的词，且价格不高于 max，就推送 |
| `BARK_URL` | iPhone 上的 Bark App 里复制的推送地址，形如 `https://api.day.app/你的key` |
| `SERVERCHAN_SENDKEY` | （可选）Server酱 SendKey，推送到微信 |

同一条爆料只推送一次。

## 手动更新
进入仓库的 **Actions → 更新捡漏雷达 → Run workflow**，可以立即运行一次。

## 说明
- 价格是爆料发布时的标题价，没有在商城页面核实。下单前请点“查看爆料”，去商城确认价格。
- “已观测最低价”只代表本雷达记录到的最低价，不等于商品的真实历史最低价。
- GitHub 的定时任务在高峰期可能会延迟几分钟到几十分钟。
- 如果仓库连续 60 天没有任何提交，GitHub 会自动停用定时任务。本项目每小时都会提交数据，正常情况下不会被停用。
