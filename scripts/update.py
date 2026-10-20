"""捡漏雷达 · GitHub 版数据更新脚本（只用 Python 标准库）。

由 Claude 定时任务每 6 小时运行一次（什么值得买的订阅源会拦截普通程序，Claude 的网页读取工具可以正常读取）：
1. 读入 Claude 抓到的好价列表（--items-json），或直接抓取 RSS（本地调试用）。
2. 新条目按爆料日期合并进 data/days/YYYY-MM-DD.json（提交到仓库，作为价格历史）。
3. 生成 site/data.json（最近 30 天），随网页一起发布到 GitHub Pages。
4. 命中关键词目标价（RADAR_TARGETS 环境变量）时在输出里列出 hits；配置了 Bark / Server酱 时也会推送。
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAYS_DIR = ROOT / "data" / "days"
SITE_DIR = ROOT / "site"
ALERTED = ROOT / ".local" / "alerted.json"   # 本机私有，不提交
CN = timezone(timedelta(hours=8))
FEED = os.environ.get("RADAR_FEED", "https://faxian.smzdm.com/feed")
UA = "Mozilla/5.0 (compatible; BargainRadar/1.0; +https://github.com) personal RSS reader"
KEEP_DAYS = 60       # 仓库里保留的历史天数
PUBLISH_DAYS = 30    # 网页显示的天数
SCHEDULE = "每 6 小时自动更新"
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
MALLS = ["京东", "天猫超市", "天猫国际", "天猫精选", "天猫", "淘宝", "拼多多", "抖音", "苏宁", "唯品会", "亚马逊", "得物",
         "小米商城", "小米有品", "华为商城", "Apple Store", "当当", "网易严选", "盒马", "山姆", "Costco", "快手", "国美"]
TITLE_PRICE = re.compile(r"\s(\d+(?:\.\d{1,2})?)元([^\s]{0,40})\s*$")


def now() -> datetime:
    return datetime.now(CN).replace(microsecond=0)


def clean(s: str | None) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def first_mall(text: str) -> str:
    best = None
    for m in MALLS:
        i = text.find(m)
        if i >= 0 and (best is None or i < best[0] or (i == best[0] and len(m) > len(best[1]))):
            best = (i, m)
    return best[1] if best else ""


def fetch_feed() -> bytes:
    local = os.environ.get("RADAR_FEED_FILE")
    if local:  # Mac 上由 curl 先下载好订阅源
        body = Path(local).read_bytes()
        head = body[:2000].decode("utf-8", "replace")
        if "<rss" not in head and "<feed" not in head:
            snippet = re.sub(r"\s+", " ", clean(body[:4000].decode("utf-8", "replace")))[:120]
            raise RuntimeError(f"返回的不是 RSS：{snippet}")
        return body
    req = urllib.request.Request(FEED, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body, code, ctype = r.read(5_000_000), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        snippet = re.sub(r"\s+", " ", clean((e.read(4000) or b"").decode("utf-8", "replace")))[:120]
        raise RuntimeError(f"什么值得买拒绝访问（HTTP {e.code}）：{snippet}") from e
    head = body[:2000].decode("utf-8", "replace")
    if "<rss" not in head and "<feed" not in head:
        snippet = re.sub(r"\s+", " ", clean(body[:4000].decode("utf-8", "replace")))[:120]
        raise RuntimeError(f"返回的不是 RSS（HTTP {code}，{ctype}）：{snippet}")
    return body


def parse(xml_bytes: bytes, seen_at: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        title = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        m = re.search(r"/p/(\d+)", link)
        if not m or not TITLE_PRICE.search(title):
            continue  # 没有明确价格的条目不收录，不猜价
        pub = item.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(pub) if pub else now()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN)
        except (TypeError, ValueError):
            dt = now()
        desc = clean(item.findtext("description")) or clean(item.findtext("content:encoded", namespaces=NS))
        out.append({
            "i": m.group(1), "t": title, "d": dt.astimezone(CN).isoformat(), "m": first_mall(desc),
            "p": (item.findtext("focus_pic") or "").strip(), "c": desc[:60], "s": seen_at,
        })
    return out


def parse_items_json(path: str, seen_at: str) -> list[dict]:
    """读入 Claude 用 WebFetch 抓到的列表：[{title, link, pubDate, pic, mall, desc}, ...]"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("好价列表格式不对")
    out = []
    for o in data:
        if not isinstance(o, dict):
            continue
        title = clean(str(o.get("title", "")))
        link = str(o.get("link", "")).strip()
        m = re.search(r"/p/(\d+)", link)
        if not m or not TITLE_PRICE.search(title):
            continue  # 没有明确价格的不收录，不猜价
        pub = str(o.get("pubDate", "")).strip()
        dt = None
        try:
            dt = parsedate_to_datetime(pub)
        except (TypeError, ValueError, IndexError):
            try:
                dt = datetime.fromisoformat(pub)
            except ValueError:
                dt = None
        if dt is None:
            dt = now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN)
        desc = clean(str(o.get("desc", "")))
        pic = str(o.get("pic", "")).strip()
        out.append({
            "i": m.group(1), "t": title, "d": dt.astimezone(CN).isoformat(),
            "m": clean(str(o.get("mall", ""))) or first_mall(desc),
            "p": pic if pic.startswith("https://") else "", "c": desc[:60], "s": seen_at,
        })
    return out


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p: Path, data, compact=False) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(data, ensure_ascii=False, indent=0)
    p.write_text(txt + "\n", encoding="utf-8")


def merge(items: list[dict]) -> list[dict]:
    """把新条目并入按日期分的文件，返回真正新增的条目。"""
    added = []
    by_day: dict[str, list[dict]] = {}
    for it in items:
        by_day.setdefault(it["d"][:10], []).append(it)
    for day, its in by_day.items():
        f = DAYS_DIR / f"{day}.json"
        doc = load_json(f, {"date": day, "items": []})
        have = {x["i"] for x in doc["items"]}
        new = [x for x in its if x["i"] not in have]
        if new:
            doc["items"].extend(new)
            doc["items"].sort(key=lambda x: x["d"], reverse=True)
            doc["items"] = doc["items"][:2000]
            doc["updatedAt"] = now().isoformat()
            save_json(f, doc)
            added.extend(new)
    return added


def prune() -> None:
    cutoff = (now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for f in DAYS_DIR.glob("*.json"):
        if f.stem < cutoff:
            f.unlink()


def build_site(status: dict) -> int:
    cutoff = (now() - timedelta(days=PUBLISH_DAYS)).strftime("%Y-%m-%d")
    items, seen = [], set()
    for f in sorted(DAYS_DIR.glob("*.json"), reverse=True):
        if f.stem < cutoff:
            continue
        for x in load_json(f, {}).get("items", []):
            if x["i"] not in seen:
                seen.add(x["i"])
                items.append(x)
    items.sort(key=lambda x: x["d"], reverse=True)
    save_json(SITE_DIR / "data.json", {"generatedAt": now().isoformat(), "status": status, "items": items}, compact=True)
    return len(items)


def title_price(t: str) -> float | None:
    m = TITLE_PRICE.search(t)
    return float(m.group(1)) if m else None


def _post(url: str, data: bytes, ctype: str) -> None:
    """优先用 curl（macOS 自带证书最可靠），没有 curl 再用 urllib。"""
    if shutil.which("curl"):
        r = subprocess.run(["curl", "-sS", "-f", "-m", "15", "-X", "POST", "-H", f"Content-Type: {ctype}", "--data-binary", "@-", url],
                           input=data, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200])
        return
    urllib.request.urlopen(urllib.request.Request(url, data=data, headers={"Content-Type": ctype}), timeout=15).read()


def push(title: str, body: str, url: str) -> None:
    bark = os.environ.get("BARK_URL", "").strip()
    sckey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if bark:
        _post(bark.rstrip("/"), json.dumps({"title": title, "body": body, "url": url, "group": "捡漏雷达"}).encode(),
              "application/json; charset=utf-8")
    if sckey:
        m = re.match(r"^sctp(\d+)t", sckey)
        ep = f"https://{m.group(1)}.push.ft07.com/send/{sckey}.send" if m else f"https://sctapi.ftqq.com/{sckey}.send"
        _post(ep, urllib.parse.urlencode({"title": title[:100], "desp": f"{body}\n\n[查看爆料]({url})"}).encode(),
              "application/x-www-form-urlencoded")


HITS: list[dict] = []


def alerts(new_items: list[dict]) -> int:
    raw = os.environ.get("RADAR_TARGETS", "").strip()
    if not raw or not new_items:
        return 0
    try:
        targets = json.loads(raw)
    except ValueError:
        print("RADAR_TARGETS 不是合法 JSON，跳过提醒", file=sys.stderr)
        return 0
    done = set(load_json(ALERTED, []))
    sent = 0
    for it in new_items:
        price = title_price(it["t"])
        for t in targets:
            words = [w.lower() for w in str(t.get("kw", "")).split() if w]
            try:
                cap = float(t.get("max"))
            except (TypeError, ValueError):
                continue
            if not words or price is None or price > cap or not all(w in it["t"].lower() for w in words):
                continue
            if it["i"] in done:
                continue
            HITS.append({"kw": " ".join(words), "max": cap, "price": price, "title": it["t"], "link": f"https://www.smzdm.com/p/{it['i']}/"})
            try:
                if os.environ.get("BARK_URL") or os.environ.get("SERVERCHAN_SENDKEY"):
                    push(f"🎯 {price:g}元 · {' '.join(words)}", it["t"], f"https://www.smzdm.com/p/{it['i']}/")
                sent += 1
            except Exception as e:  # noqa: BLE001 —— 推送失败不影响数据更新
                print(f"推送失败：{e}", file=sys.stderr)
            done.add(it["i"])
            break
    save_json(ALERTED, sorted(done)[-3000:], compact=True)
    return sent


def main() -> int:
    if "--build-only" in sys.argv:  # GitHub Actions：只用仓库里的数据生成网页，不抓取
        status = load_json(ROOT / "data" / "status.json", {"lastRun": None, "ok": False, "error": "还没有数据", "schedule": SCHEDULE})
        print(json.dumps({**status, "published": build_site(status)}, ensure_ascii=False))
        return 0
    seen_at = now().isoformat()
    status = {"lastRun": seen_at, "ok": True, "fetched": 0, "added": 0, "alerts": 0, "error": None, "schedule": SCHEDULE}
    args = sys.argv[1:]
    try:
        if "--fail" in args:  # Claude 抓取失败时，只记录原因并重新发布旧数据
            raise RuntimeError(args[args.index("--fail") + 1] if len(args) > args.index("--fail") + 1 else "抓取失败")
        if "--items-json" in args:
            items = parse_items_json(args[args.index("--items-json") + 1], seen_at)
        else:
            items = parse(fetch_feed(), seen_at)
        added = merge(items)
        status.update(fetched=len(items), added=len(added), alerts=alerts(added))
    except Exception as e:  # noqa: BLE001 —— 失败时保留旧数据，网页显示失败原因
        reason = "连接什么值得买失败" if isinstance(e, (OSError, urllib.error.URLError)) else "解析订阅源失败" if isinstance(e, ET.ParseError) else str(e)
        status.update(ok=False, error=f"{reason}，网页显示的是之前的数据"[:300])
        print("抓取失败：", status["error"], file=sys.stderr)
    prune()
    total = build_site(status)
    save_json(ROOT / "data" / "status.json", status)
    print(json.dumps({**status, "published": total, "hits": HITS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
