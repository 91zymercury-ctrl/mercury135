"""捡漏雷达 · GitHub 版数据更新脚本（只用 Python 标准库）。

由 Claude 定时任务每 6 小时运行一次（什么值得买的订阅源会拦截普通程序，Claude 的网页读取工具可以正常读取）：
1. 读入 Claude 抓到的好价列表（--items-json），或直接抓取 RSS（本地调试用）。
2. 新条目按爆料日期合并进 data/days/YYYY-MM-DD.json（提交到仓库，作为价格历史）。
3. 追踪清单（data/watch.json）：读入 Claude 抓到的什么值得买百科「全网比价」（--watch-dir），
   每个平台的最低价按时间存进 data/prices/<id>.json，用来画 30 天走势。
4. 生成 site/data.json（最近 30 天），随网页一起发布到 GitHub Pages。
5. 命中关键词目标价（RADAR_TARGETS 环境变量）时在输出里列出 hits；配置了 Bark / Server酱 时也会推送。

追踪清单维护：
  --watch-plan                         列出要读取的百科页面
  --add-watch '{"name":..,"kw":..,"not":..,"wiki":..,"issue":N}'   新增或更新一个追踪商品
  --remove-watch <id 或名称>            删除追踪商品（价格记录一并删除）
  --issue-done N                       记录已处理的 GitHub issue
"""
from __future__ import annotations

import hashlib
import html
import json
import statistics
import unicodedata
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
WATCH_FILE = ROOT / "data" / "watch.json"
PRICES_DIR = ROOT / "data" / "prices"
WIKI_ID = re.compile(r"^[0-9a-z]{5,12}$")
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


# ---------------- 追踪清单 ----------------

def squash(s: str) -> str:
    """统一大小写、全半角并去掉空白，方便“iphone17”匹配“iPhone 17”。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").lower())


def platform(mall: str) -> str:
    """把商城名归到平台：天猫超市/天猫精选 → 天猫，京东自营 → 京东。"""
    m = unicodedata.normalize("NFKC", mall or "").strip()
    for key, name in (("京东", "京东"), ("天猫", "天猫"), ("淘宝", "淘宝"), ("拼多多", "拼多多"), ("抖音", "抖音"),
                      ("苏宁", "苏宁"), ("唯品会", "唯品会"), ("得物", "得物"), ("小米", "小米商城"), ("华为", "华为商城"),
                      ("apple", "Apple 官网"), ("苹果官网", "Apple 官网"), ("亚马逊", "亚马逊"), ("快手", "快手")):
        if key in m.lower():
            return name
    return m[:8] or "其他"


def load_watch() -> dict:
    w = load_json(WATCH_FILE, {})
    if not isinstance(w, dict):
        w = {}
    w.setdefault("items", [])
    w.setdefault("issues", [])
    return w


def wiki_id(v) -> str:
    v = str(v or "").strip()
    m = re.search(r"wiki\.smzdm\.com/p/([0-9a-z]+)", v)
    v = m.group(1) if m else v.strip("/")
    if v and not WIKI_ID.match(v):
        raise RuntimeError(f"百科编号格式不对：{v}")
    return v


def add_watch(raw: str) -> dict:
    o = json.loads(raw)
    name = clean(str(o.get("name", "")))[:60]
    kw = " ".join(clean(str(o.get("kw", ""))).split())[:80] or name
    if not name:
        raise RuntimeError("缺少商品名称 name")
    wid = wiki_id(o.get("wiki"))
    w = load_watch()
    item = next((x for x in w["items"] if (wid and x.get("wiki") == wid) or squash(x.get("kw", "")) == squash(kw)), None)
    if item is None:
        item = {"id": wid or "k" + hashlib.sha1(squash(kw).encode()).hexdigest()[:8], "added": now().strftime("%Y-%m-%d")}
        w["items"].append(item)
    item.update(name=name, kw=kw, **{"not": " ".join(clean(str(o.get("not", ""))).split())[:80]}, wiki=wid)
    if o.get("issue"):
        w["issues"] = sorted(set(w["issues"]) | {int(o["issue"])})
    save_json(WATCH_FILE, w)
    return item


def remove_watch(key: str) -> list[str]:
    w = load_watch()
    gone = [x for x in w["items"] if key in (x.get("id"), x.get("name"), x.get("wiki"))]
    w["items"] = [x for x in w["items"] if x not in gone]
    for x in gone:
        (PRICES_DIR / f"{x['id']}.json").unlink(missing_ok=True)
    save_json(WATCH_FILE, w)
    return [x["name"] for x in gone]


def watch_plan() -> list[dict]:
    return [{"id": x["id"], "name": x["name"], "wiki_url": f"https://wiki.smzdm.com/p/{x['wiki']}/" if x.get("wiki") else None}
            for x in load_watch()["items"]]


def parse_offers(doc) -> tuple[str, list[dict]]:
    """校验 Claude 从百科页读出的比价列表：{"title":..,"offers":[{"mall","price","orig","link"}]}。"""
    if not isinstance(doc, dict):
        raise RuntimeError("比价结果格式不对")
    if doc.get("error"):
        raise RuntimeError(clean(str(doc["error"]))[:80])
    offers = []
    for o in doc.get("offers") or []:
        if not isinstance(o, dict):
            continue
        try:
            p = float(str(o.get("price", "")).replace("¥", "").replace(",", "").strip())
        except ValueError:
            continue
        mall = clean(str(o.get("mall", "")))[:20]
        if not mall or not 0 < p < 1_000_000:
            continue
        try:
            orig = float(str(o.get("orig") or "").replace("¥", "").replace(",", "").strip() or 0) or None
        except ValueError:
            orig = None
        link = str(o.get("link") or "").strip()
        offers.append({"m": mall, "p": round(p, 2), "o": orig if orig and orig >= p else None,
                       "u": link if re.match(r"^https://[\w.-]+/", link) else ""})
    if offers:  # 价格远低于其他报价的多半是配件或读错，丢掉
        med = statistics.median(x["p"] for x in offers)
        offers = [x for x in offers if x["p"] >= med * 0.4]
    if not offers:
        raise RuntimeError("页面上没有读到有效价格")
    offers.sort(key=lambda x: x["p"])
    return clean(str(doc.get("title", "")))[:120], offers


def merge_watch(folder: str, seen_at: str) -> dict:
    """把 --watch-dir 里每个 <id>.json 的比价结果并入 data/prices/<id>.json。"""
    w = load_watch()
    done, failed = [], []
    for item in w["items"]:
        if not item.get("wiki"):
            continue
        f = PRICES_DIR / f"{item['id']}.json"
        hist = load_json(f, {"id": item["id"], "samples": []})
        src = Path(folder) / f"{item['id']}.json"
        try:
            if not src.exists():
                raise RuntimeError("本次没有读取")
            title, offers = parse_offers(json.loads(src.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001 —— 单个商品失败不影响其他
            hist["lastError"] = {"t": seen_at, "error": str(e)[:120]}
            save_json(f, hist)
            failed.append(f"{item['name']}（{str(e)[:40]}）")
            continue
        lo: dict[str, float] = {}
        for o in offers:
            k = platform(o["m"])
            lo[k] = min(lo.get(k, o["p"]), o["p"])
        prev = hist["samples"][-1] if hist["samples"] else None
        sample = {"t": seen_at, "lo": lo}
        if prev and (datetime.fromisoformat(seen_at) - datetime.fromisoformat(prev["t"])) < timedelta(hours=1):
            hist["samples"][-1] = sample  # 一小时内重复运行，覆盖上一条
            prev = hist["samples"][-2] if len(hist["samples"]) > 1 else None
        else:
            hist["samples"].append(sample)
        cutoff = (now() - timedelta(days=KEEP_DAYS)).isoformat()
        hist["samples"] = [s for s in hist["samples"] if s["t"] >= cutoff]
        hist["latest"] = {"t": seen_at, "title": title, "offers": offers[:20]}
        hist["lastError"] = None
        save_json(f, hist)
        done.append({"item": item, "low": min(lo.values()), "prevLow": min(prev["lo"].values()) if prev and prev.get("lo") else None,
                     "offer": offers[0]})
    return {"done": done, "failed": failed}


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
    pub_cut = (now() - timedelta(days=PUBLISH_DAYS)).isoformat()
    watch = []
    for x in load_watch()["items"]:
        h = load_json(PRICES_DIR / f"{x['id']}.json", {})
        watch.append({k: x.get(k) for k in ("id", "name", "kw", "not", "wiki", "added")} | {
            "samples": [s for s in h.get("samples", []) if s["t"] >= pub_cut],
            "latest": h.get("latest"), "lastError": h.get("lastError")})
    save_json(SITE_DIR / "data.json", {"generatedAt": now().isoformat(), "status": status, "items": items, "watch": watch}, compact=True)
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


def load_targets() -> list[dict]:
    raw = os.environ.get("RADAR_TARGETS", "").strip()
    if not raw:
        return []
    try:
        targets = json.loads(raw)
    except ValueError:
        print("RADAR_TARGETS 不是合法 JSON，跳过提醒", file=sys.stderr)
        return []
    out = []
    for t in targets if isinstance(targets, list) else []:
        try:
            out.append({"words": [squash(w) for w in str(t.get("kw", "")).split() if w], "max": float(t.get("max"))})
        except (TypeError, ValueError, AttributeError):
            continue
    return [t for t in out if t["words"]]


def watch_alerts(done: list[dict]) -> None:
    """追踪商品的全网最低价第一次降到目标价以下时提醒（上一次还高于目标价，避免每 6 小时重复提醒）。"""
    for d in done:
        text = squash(d["item"]["name"] + " " + d["item"]["kw"])
        for t in load_targets():
            if not all(w in text for w in t["words"]) or d["low"] > t["max"]:
                continue
            if d["prevLow"] is not None and d["prevLow"] <= t["max"]:
                continue
            o = d["offer"]
            HITS.append({"kw": " ".join(t["words"]), "max": t["max"], "price": d["low"],
                         "title": f"{d['item']['name']}（{o['m']}，全网比价）",
                         "link": o["u"] or f"https://wiki.smzdm.com/p/{d['item']['wiki']}/"})
            break


def alerts(new_items: list[dict]) -> int:
    targets = load_targets()
    if not targets or not new_items:
        return 0
    done = set(load_json(ALERTED, []))
    sent = 0
    for it in new_items:
        price = title_price(it["t"])
        for t in targets:
            words, cap = t["words"], t["max"]
            if price is None or price > cap or not all(w in squash(it["t"]) for w in words):
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
    args = sys.argv[1:]
    arg = lambda flag: args[args.index(flag) + 1] if flag in args and len(args) > args.index(flag) + 1 else None  # noqa: E731
    if "--watch-plan" in args:
        print(json.dumps(watch_plan(), ensure_ascii=False))
        return 0
    if "--add-watch" in args:
        print(json.dumps(add_watch(arg("--add-watch") or "{}"), ensure_ascii=False))
        return 0
    if "--remove-watch" in args:
        print(json.dumps({"removed": remove_watch(arg("--remove-watch") or "")}, ensure_ascii=False))
        return 0
    if "--issue-done" in args:
        w = load_watch()
        w["issues"] = sorted(set(w["issues"]) | {int(arg("--issue-done"))})
        save_json(WATCH_FILE, w)
        return 0
    seen_at = now().isoformat()
    status = {"lastRun": seen_at, "ok": True, "fetched": 0, "added": 0, "alerts": 0, "error": None, "schedule": SCHEDULE}
    if "--watch-dir" in args:
        try:
            res = merge_watch(arg("--watch-dir") or "", seen_at)
            watch_alerts(res["done"])
            status.update(watched=len(res["done"]), watchFailed=res["failed"])
        except Exception as e:  # noqa: BLE001
            status.update(watched=0, watchFailed=[f"全部失败：{str(e)[:80]}"])
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
