# 捡漏雷达 · GitHub 版

自动汇总什么值得买上的全网好价，按价格历史给出好价评分。网页是公开的，任何人打开链接就能看，不需要登录。

- **更新**：Claude 定时任务每 6 小时读取一次什么值得买好价订阅源，把数据推送到本仓库；GitHub Actions 收到推送后自动发布到 GitHub Pages。什么值得买的订阅源会拦截普通程序（GitHub 服务器、curl 都只能拿到验证页），所以用 Claude 的网页读取工具来读。
- **网页**：https://91zymercury-ctrl.github.io/mercury135/
- **历史**：价格记录按天保存在 `data/days/`，保留 60 天，网页显示最近 30 天。

## 目录
- `scripts/update.py`：抓取、去重、合并历史、生成网页数据、目标价推送（只用 Python 标准库）
- `site/index.html`：网页（手机优先，支持深色模式）
- `.github/workflows/update.yml`：收到推送后发布网页
- `data/days/*.json`：每天的好价记录（自动提交）

## 关键词目标价提醒
目标价保存在 Claude 定时任务里，不公开。想增加或修改，直接告诉 Claude，例如“加一个提醒：固态 2TB，600 元以下”。命中时会通过 Claude App 推送到手机。

## 手动更新
在 Claude 里让它运行一次“捡漏雷达 GitHub 更新”这个定时任务。也可以在本地运行：`python3 scripts/update.py --items-json 好价列表.json`，再提交 `data/`。

## 说明
- 价格是爆料发布时的标题价，没有在商城页面核实。下单前请点“查看爆料”，去商城确认价格。
- “已观测最低价”只代表本雷达记录到的最低价，不等于商品的真实历史最低价。
