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
from collections import Counter
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
CAT_DIR = ROOT / "data" / "catalog"          # 商品目录：pages.json（要读的列表页）、state.json、products.json、prices/日期.json
CAT_FRESH_HOURS = 20                         # 列表页多久没读就算该更新了
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


# ---------------- 商品目录（什么值得买百科各分类的热门商品 + 每日全网最低价） ----------------

def catalog_plan(limit: int) -> list[dict]:
    pages = load_json(CAT_DIR / "pages.json", {}).get("pages", [])
    fetched = load_json(CAT_DIR / "state.json", {}).get("fetched", {})
    fresh = (now() - timedelta(hours=CAT_FRESH_HOURS)).isoformat()
    due = [p for p in pages if fetched.get(p["key"], "") < fresh]
    due.sort(key=lambda p: fetched.get(p["key"], ""))  # 从没读过的排最前，其次是最久没读的
    return [{"key": p["key"], "name": p["name"], "url": p["url"]} for p in due[:limit]]


def parse_listing(doc) -> list[dict]:
    """校验 Claude 从列表页读出的商品：{"items":[{"id","name","price"}]} 或紧凑格式 {"items":[[id, price, name]]}。"""
    if not isinstance(doc, dict):
        raise RuntimeError("列表格式不对")
    if doc.get("error"):
        raise RuntimeError(clean(str(doc["error"]))[:80])
    out = []
    for o in doc.get("items") or []:
        if isinstance(o, list) and len(o) >= 3:
            o = {"id": o[0], "price": o[1], "name": o[2]}
        if not isinstance(o, dict):
            continue
        pid, name = str(o.get("id", "")).strip(), clean(str(o.get("name", "")))[:120]
        if not WIKI_ID.match(pid) or not name or (pid.isdigit() and len(pid) >= 8):
            continue  # 纯数字长编号是页面底部的好价爆料，不是百科商品
        try:
            p = float(str(o.get("price") if o.get("price") is not None else "").replace(",", "").replace("¥", "").replace("元起", "").strip())
            p = round(p, 2) if 0 < p < 10_000_000 else None
        except ValueError:
            p = None
        out.append({"id": pid, "name": name, "price": p})
    if not out:
        raise RuntimeError("没有读到商品")
    return out


def prune_catalog() -> None:
    cut = (now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for f in (CAT_DIR / "prices").glob("*.json"):
        if f.stem < cut:
            f.unlink()
    prod = load_json(CAT_DIR / "products.json", {})
    keep = {k: v for k, v in prod.items() if v.get("s", "") >= cut}
    if len(keep) != len(prod):
        save_json(CAT_DIR / "products.json", keep, compact=True)


def merge_catalog(folder: str) -> dict:
    pages = {p["key"]: p for p in load_json(CAT_DIR / "pages.json", {}).get("pages", [])}
    state = load_json(CAT_DIR / "state.json", {})
    state.setdefault("fetched", {})
    state.setdefault("errors", {})
    prod = load_json(CAT_DIR / "products.json", {})
    day = now().strftime("%Y-%m-%d")
    pf = CAT_DIR / "prices" / f"{day}.json"
    prices = load_json(pf, {})
    ok, failed, seen = [], [], 0
    for src in sorted(Path(folder).glob("*.json")):
        key = src.stem
        if key not in pages:
            continue
        try:
            items = parse_listing(json.loads(src.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001 —— 单页失败不影响其他页
            state["errors"][key] = {"t": now().isoformat(), "error": str(e)[:80]}
            failed.append(f"{pages[key]['name']}（{str(e)[:30]}）")
            continue
        for it in items:
            prod[it["id"]] = {"n": it["name"], "c": pages[key]["name"], "s": day}
            if it["price"] is not None:
                prices[it["id"]] = min(prices.get(it["id"], it["price"]), it["price"])
            seen += 1
        state["fetched"][key] = now().isoformat()
        state["errors"].pop(key, None)
        ok.append(key)
    state["lastRun"] = now().isoformat()
    save_json(CAT_DIR / "products.json", prod, compact=True)
    save_json(pf, prices, compact=True)
    save_json(CAT_DIR / "state.json", state)
    prune_catalog()
    return {"pages": len(ok), "failed": failed, "items": seen, "products": len(load_json(CAT_DIR / "products.json", {}))}


CARRY_DAYS = 2  # 某天没读到这个商品所在的列表页时，沿用最近一次价格的最长天数


def daily_series(daily: list[dict], pid: str) -> list:
    """每天的价格；没读到的日子沿用最近一次（最多 CARRY_DAYS 天），再往后记 None。"""
    out, last, gap = [], None, 0
    for d in daily:
        p = d.get(pid)
        if p is not None:
            last, gap = p, 0
        else:
            gap += 1
            if gap > CARRY_DAYS:
                last = None
        out.append(p if p is not None else last)
    return out


def build_catalog() -> int:
    """site/catalog.json：最近 30 天出现过的商品；价格只记变化点 [第几天, 价格]，网页搜索时才下载。"""
    files = sorted((CAT_DIR / "prices").glob("*.json"))[-PUBLISH_DAYS:]
    days = [f.stem for f in files]
    daily = [load_json(f, {}) for f in files]
    prod = load_json(CAT_DIR / "products.json", {})
    cut = days[0] if days else now().strftime("%Y-%m-%d")
    cats: list[str] = []
    items = []
    for pid, v in prod.items():
        if v.get("s", "") < cut:
            continue
        if v.get("c") not in cats:
            cats.append(v.get("c"))
        h, last = [], "x"
        for i, p in enumerate(daily_series(daily, pid)):
            if p != last:
                h.append([i, p])
                last = p
        items.append([pid, v["n"], cats.index(v.get("c")), h])
    state = load_json(CAT_DIR / "state.json", {})
    save_json(SITE_DIR / "catalog.json", {"updatedAt": state.get("lastRun"), "days": days, "cats": cats, "items": items}, compact=True)
    build_models(days, daily, prod, cut)
    return len(items)


# ---------------- 型号汇总：显卡按芯片、手机按机型 ----------------

GPU_SPLIT_MEM = {"RTX 5060 Ti", "RTX 4060 Ti", "RX 9060 XT", "RTX 3060", "RTX 3050", "RX 6750 GRE", "Arc A770"}


def gpu_chip(name: str) -> tuple[str, str] | None:
    """从显卡名称里认出芯片型号，返回 (芯片, 系列)；认不出返回 None。"""
    t = unicodedata.normalize("NFKC", name or "").upper()
    t = re.sub(r"(?<=[A-Z])(?=\d{3,4})", " ", t)          # RTX5060 → RTX 5060
    t = re.sub(r"(?<=\d)(?=TI\b|TI\s|SUPER)", " ", t)       # 5060TI → 5060 TI
    m = re.search(r"RTX\s*PRO\s*(\d{4})", t)
    if m:
        return f"RTX PRO {m.group(1)}", "专业卡"
    m = re.search(r"RTX\s*A\s*(\d{3,4})\b", t) or re.search(r"RTX\s*(\d{4})\s*ADA", t)
    if m:
        return (f"RTX A{m.group(1)}" if "ADA" not in m.group(0) else f"RTX {m.group(1)} Ada"), "专业卡"
    if "QUADRO" in t:
        return None
    m = re.search(r"\b(RTX|GTX|GT)\s*(\d{3,4})\s*(TI\s*SUPER|TI|SUPER|S(?![A-Z])|D\s*V2|D(?![A-Z]))?", t)
    if m:
        num = m.group(2)
        suf = re.sub(r"\s+", "", m.group(3) or "")
        suf = {"TISUPER": " Ti Super", "TI": " Ti", "SUPER": " Super", "S": " Super", "DV2": " D v2", "D": " D"}.get(suf, "")
        chip = f"{m.group(1)} {num}{suf}"
        gen = num[0] if len(num) == 4 else ""
        series = {"5": "RTX 50 系列", "4": "RTX 40 系列"}.get(gen, "更早型号") if m.group(1) == "RTX" else "更早型号"
    else:
        m = re.search(r"\bRX\s*(\d{3,4})\s*(XTX|XT|GRE)?(?![A-Z])", t)
        if m:
            chip = f"RX {m.group(1)}" + (f" {m.group(2)}" if m.group(2) else "")
            series = {"9": "RX 9000 系列", "7": "RX 7000 系列"}.get(m.group(1)[0] if len(m.group(1)) == 4 else "", "更早型号")
        else:
            m = re.search(r"ARC\s*(PRO\s*)?([AB])\s*(\d{2,3})\b", t)
            if m:
                return (f"Arc Pro {m.group(2)}{m.group(3)}", "专业卡") if m.group(1) else (f"Arc {m.group(2)}{m.group(3)}", "Intel Arc")
            m = re.search(r"MTT\s*S\s*(\d{2,4})", t)
            if m:
                return f"摩尔线程 MTT S{m.group(1)}", "国产显卡"
            return None
    if chip in GPU_SPLIT_MEM:
        rest = t[m.end():] + " " + t[:m.start()]
        mm = re.search(r"(?<!\d)(6|8|10|12|16)\s*G(?:B|D6|D7)?(?![A-Z0-9])", rest)
        if mm:
            chip += f" {mm.group(1)}G"
    return chip, series


PHONE_BRANDS = [("苹果", ("apple", "苹果", "iphone")), ("华为", ("huawei", "华为")), ("荣耀", ("honor", "荣耀")), ("REDMI", ("redmi", "红米")),
                ("小米", ("xiaomi", "小米")), ("一加", ("oneplus", "一加")), ("iQOO", ("iqoo",)), ("vivo", ("vivo",)), ("OPPO", ("oppo",)),
                ("真我", ("realme", "真我")), ("三星", ("samsung", "三星")), ("红魔", ("redmagic", "红魔")), ("努比亚", ("nubia", "努比亚")),
                ("魅族", ("meizu", "魅族")), ("摩托罗拉", ("motorola", "摩托罗拉")), ("联想", ("lenovo", "联想")), ("中兴", ("zte", "中兴")),
                ("索尼", ("sony", "索尼")), ("谷歌", ("google",)), ("诺基亚", ("nokia", "诺基亚"))]
BRAND_WORDS = {a for _, als in PHONE_BRANDS for a in als if a != "iphone"} | {"moto", "mi"}
PHONE_KEEP_CN = {"畅享", "畅玩", "至尊版", "至尊纪念版", "竞速版", "活力版", "非凡大师", "典藏版", "超能版", "超级版", "元气版", "轻享版",
                 "标准版", "青春版", "冠军版", "探索版", "焕", "卫星版", "卫星通信版", "影像"}
PHONE_STOP = re.compile(r"^(?:[2345][gk].*|\d+(?:gb|tb|g\+|mah|hz|w)\b.*|\d+gb\+.*|手机.*|智能手机|全网通.*|双卡.*|折叠.*|三折叠.*|第.代.*|骁龙.*|天玑.*|麒麟.*|ai|nfc)$", re.I)


def phone_model(name: str) -> tuple[str, str, str] | None:
    """从手机名称里认出 (品牌, 机型键, 显示名)；认不出返回 None。"""
    t = unicodedata.normalize("NFKC", name or "")
    t = re.sub(r"[()（）/\[\]【】,，]", " ", t)
    t = re.sub(r"(?<=[一-鿿])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9+])(?=[一-鿿])", " ", t)
    t = re.sub(r"(?<=\d)(?=[A-Za-z]{3,})", " ", t)  # 17Ultra → 17 Ultra，Y600Pro → Y600 Pro
    toks = t.split()
    low = [x.lower() for x in toks]
    brand = None
    for b, als in PHONE_BRANDS:
        if any(x in als or x.startswith("iqoo") and b == "iQOO" for x in low):
            brand = b
            break
    if brand == "vivo" and any(x.startswith("iqoo") for x in low):
        brand = "iQOO"
    if brand in ("小米", "华为") and any(x in ("redmi", "红米") for x in low):
        brand = "REDMI"
    if brand == "努比亚" and any(x in ("红魔", "redmagic") for x in low):
        brand = "红魔"
    if not brand:
        return None
    out: list[str] = []
    for x in toks:
        xl = x.lower()
        if xl in BRAND_WORDS or xl == "iqoo":
            if out:
                break
            continue
        if PHONE_STOP.match(xl):
            break
        if re.fullmatch(r"[a-z0-9+\-]+", xl):
            if re.fullmatch(r"[a-z]{3}-[a-z]{2}\d{2}[a-z]?", xl) or re.fullmatch(r"a\d{4}", xl):
                break  # 型号代码 EMA-AL00U、Apple A2634
            if xl.isdigit() and (len(xl) >= 4 or any(any(c.isdigit() for c in o) for o in out)):
                break  # 「5000万像素」「2亿像素」里的数字
            out.append(x)
            continue
        if x in PHONE_KEEP_CN:
            out.append(x)
            continue
        if x.endswith("系列") and len(x) > 2 and x[:-2] in PHONE_KEEP_CN:
            out.append(x[:-2])
        break
    if not out or not any(any(c.isdigit() for c in o) or o.lower() in ("x", "duo", "power", "xt", "xts", "flip") for o in out):
        return None
    key_toks = [o.lower() for o in out if o.lower() != "galaxy"]
    disp = " ".join(out)
    if brand == "三星" and not disp.lower().startswith("galaxy"):
        disp = "Galaxy " + disp
    if brand != "苹果":
        disp = f"{brand} {disp}"
    return brand, brand + "|" + "".join(key_toks), disp


SUPP_BRANDS = [("ON", ("optimum", "奥普帝蒙", "on")), ("康比特", ("康比特", "cpt")), ("MYPROTEIN", ("myprotein",)), ("肌肉科技", ("muscletech", "肌肉科技")),
               ("MUSCLEPHARM", ("musclepharm",)), ("汤臣倍健", ("汤臣倍健", "by-health")), ("诺特兰德", ("诺特兰德", "nutrend")),
               ("BSN", ("bsn",)), ("Dymatize", ("dymatize", "狂神")), ("NutreX", ("nutrex",)), ("ISOPURE", ("isopure",)), ("USN", ("usn",)),
               ("乐体", ("乐体",)), ("迈胜", ("迈胜", "maxler")), ("西王", ("西王",))]
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(磅|lbs?|kg|千克|公斤|g|克)(?![a-z])", re.I)


def supplement_group(name: str) -> tuple[str, str, str, str, float | None] | None:
    """蛋白粉、肌酸：返回 (类别, 品牌, 分组键, 显示名, 重量kg)。口味不同的同款同规格算一组。"""
    t = unicodedata.normalize("NFKC", name or "")
    low = t.lower()
    if "肌酸" in t and not re.search(r"乳清|蛋白|增肌|增重", t):
        cat = "肌酸"
    elif re.search(r"乳清|蛋白粉|蛋白质粉|whey", low) and not re.search(r"胶原|增肌|增重|氮泵|蛋白棒|蛋白饮|代餐|蛋白肽", t):
        cat = "蛋白粉"
    else:
        return None
    toks = set(re.findall(r"[a-z][a-z\-]+|[一-鿿]+", low))
    brand = next((b for b, als in SUPP_BRANDS if any(a in toks or (len(a) > 2 and a in low) for a in als)), None)
    if not brand:
        return None
    kg = None
    m = WEIGHT_RE.search(t)
    if m:
        v, u = float(m.group(1)), m.group(2).lower()
        kg = v * 0.4536 if u in ("磅", "lb", "lbs") else v if u in ("kg", "千克", "公斤") else v / 1000
        mm = re.search(r"[*×x]\s*(\d+)\s*(?:罐|桶|袋|瓶|盒)", t[m.end():m.end() + 8])
        if mm:
            kg *= int(mm.group(1))
        kg = round(kg, 3)
    if cat == "蛋白粉":
        series = next((s for k, s in (("金标", "金标"), ("黄金标准", "金标"), ("白金", "白金"), ("正氮", "正氮"), ("炽金", "炽金"),
                                       ("impact", "Impact"), ("熊猫", "熊猫"), ("combat", "Combat")) if k in low), "")
        kind = "水解乳清" if "水解" in t else "分离乳清" if "分离" in t else "乳清蛋白粉"
        line = f"{series} {kind}".strip()
    else:
        series = next((s for k, s in (("炽金", "炽金"), ("绿标", "绿标"), ("白金", "白金")) if k in t), "")
        kind = "锌镁肌酸" if "锌镁" in t else "盐酸肌酸" if "盐酸" in t else "一水肌酸" if "一水" in t else "肌酸"
        line = f"{series} {kind}".strip()
    size = (f"{kg:g}kg" if kg >= 1 else f"{kg * 1000:g}g") if kg else "规格未注明"
    return cat, brand, f"{cat}|{brand}|{line}|{kg or '?'}", f"{brand} {line} {size}", kg


LAPTOP_BRANDS = [("苹果", ("apple", "苹果", "macbook")), ("华为", ("huawei", "华为", "matebook")), ("荣耀", ("honor", "荣耀", "magicbook")),
                 ("小米", ("xiaomi", "小米", "redmi", "红米")), ("来酷", ("来酷", "lecoo")),
                 ("联想", ("lenovo", "联想", "legion", "拯救者", "小新", "thinkpad", "thinkbook", "yoga")),
                 ("华硕", ("asus", "华硕", "rog", "玩家国度", "天选", "无畏", "灵耀", "adou", "枪神", "魔霸", "破晓")),
                 ("戴尔", ("dell", "戴尔", "xps", "alienware", "外星人", "灵越", "游匣")),
                 ("惠普", ("hp", "惠普", "暗影精灵", "光影精灵", "星book", "战66", "战99", "elitebook")), ("微星", ("msi", "微星")),
                 ("宏碁", ("acer", "宏碁", "掠夺者", "暗影骑士")), ("机械革命", ("mechrevo", "机械革命")), ("雷神", ("thunderobot", "雷神")),
                 ("神舟", ("hasee", "神舟")), ("七彩虹", ("colorful", "七彩虹")), ("机械师", ("机械师", "machenike")), ("火影", ("火影",)),
                 ("微软", ("microsoft", "微软", "surface")), ("三星", ("samsung", "三星")), ("雷蛇", ("razer", "雷蛇"))]
LAPTOP_DROP = {"apple", "苹果", "huawei", "华为", "honor", "荣耀", "xiaomi", "小米", "lenovo", "联想", "legion", "asus", "华硕", "玩家国度",
               "dell", "戴尔", "hp", "惠普", "msi", "微星", "acer", "宏碁", "mechrevo", "机械革命", "thunderobot", "雷神", "hasee", "神舟",
               "colorful", "七彩虹", "machenike", "microsoft", "微软", "samsung", "三星", "razer", "雷蛇", "lecoo", "来酷", "系列"}
LAPTOP_SERIES = {"拯救者", "小新", "天选", "无畏", "灵耀", "枪神", "魔霸", "幻", "破晓", "暗影精灵", "光影精灵", "灵越", "游匣", "外星人",
                 "掠夺者", "暗影骑士", "非凡", "无界", "极光", "蛟龙", "旷世", "翼龙", "耀世", "苍龙", "星影", "泰坦", "雷影", "冲锋坦克", "侠影",
                 "战66", "战99", "战神", "非凡大师", "新锐", "星Book"}
LAPTOP_MOD = {"pro", "air", "max", "plus", "se", "x", "s", "fold", "neo", "carbon", "duo", "art", "book", "flip"}
LAPTOP_STOP = re.compile(r"^(?:\d+(?:\.\d+)?英寸.*|\d+(?:\.\d+)?寸|i[3579].*|ultra\d?|u\d.*|core.*|hx?|酷睿.*|锐龙.*|r[3579].*|rtx.*|gtx.*|"
                         r"\d+gb.*|\d+g\+.*|\d+g|\d+tb|20[12]\d.*|gen|ai(?![a-z]).*|oled|\d+hz|[23]\.?\d?k.*|\d+k|ill\d+|iax\d+|[a-z]{2}\d{4}[a-z]{2})$", re.I)


def laptop_model(name: str) -> tuple[str, str, str] | None:
    """笔记本：返回 (品牌, 机型键, 显示名)。同系列同尺寸算一个机型，不同配置在里面分别列出。"""
    t = unicodedata.normalize("NFKC", name or "")
    t = re.sub(r"a\s*豆", " Adou ", t, flags=re.I)
    t = re.sub(r"【[^】]*】|\[[^\]]*\]", " ", t)
    t = re.sub(r"[()（）/,，、]", " ", t)
    series_re = "|".join(re.escape(s) for s in sorted(LAPTOP_SERIES, key=len, reverse=True))
    t = re.sub(f"({series_re})", r" \1 ", t, flags=re.I)  # 先把系列词隔开：拯救者Y7000P → 拯救者 Y7000P
    t = re.sub(r"(?<=[一-鿿])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9+])(?=[一-鿿])", " ", t)
    t = re.sub(r"星\s+book", "星Book", t, flags=re.I)
    t = re.sub(r"\b(Pro|Air|Max|PRO|AIR|MAX|Adou)(?=\d)", r"\1 ", t)
    t = re.sub(r"(?<=\d)(?=(?:Pro|Air|Max|Plus|PRO|AIR|MAX)\b)", " ", t)
    low = t.lower()
    toks = t.split()
    ltoks = [x.lower() for x in toks]
    brand = next((b for b, als in LAPTOP_BRANDS if any(a in ltoks for a in als) or any(len(a) > 3 and a in low for a in als)), None)
    if not brand:
        return None
    if brand == "苹果":
        m = re.search(r"macbook\s*(air|pro|neo)?", low)
        if not m:
            return None
        line = "MacBook" + (" " + m.group(1).title() if m.group(1) else "")
        size = re.search(r"(1[3-6])(?:\.\d)?\s*英寸", t)
        chip = re.search(r"\b(m[1-6])(?:\s*(pro|max))?\b|\b(a1\d\s*pro)\b", low)
        parts = [line] + ([size.group(1)] if size else []) + ([(chip.group(1).upper() + (" " + chip.group(2).title() if chip.group(2) else "")) if chip.group(1) else "A18 Pro"] if chip else [])
        disp = " ".join(parts)
        return brand, "苹果|" + disp.lower().replace(" ", ""), disp
    toks = re.sub(r"\d+(?:\.\d+)?\s*英寸", " ", t).split()  # 尺寸不算型号：MateBook Fold 18英寸
    ltoks = [x.lower() for x in toks]
    out: list[str] = []
    for x, xl in zip(toks, ltoks):
        if xl in LAPTOP_DROP:
            continue
        if LAPTOP_STOP.match(xl):
            break
        if x in LAPTOP_SERIES or xl == "星book" or re.fullmatch(r"[a-z0-9+\-]+", xl):
            if xl.isdigit() and 13 <= int(xl) <= 18 and any(any(c.isdigit() for c in o) for o in out):
                break  # 型号后面的尺寸：Y7000P 16
            if out and out[-1].lower() == xl:
                continue
            out.append("a豆" if xl == "adou" else x)
            if len(out) >= 4:
                break
            continue
        if out:
            break
    out = [o for o in out if o.lower() not in ("笔记本", "游戏本")]
    if not out or not (any(o in LAPTOP_SERIES or any(c.isdigit() for c in o) for o in out) or any(o.lower() in ("matebook", "magicbook", "thinkpad", "thinkbook", "xps") for o in out)):
        return None
    if all(o.lower() in LAPTOP_MOD for o in out):
        return None
    if brand == "联想" and re.fullmatch(r"[yr]\d{4}[a-z]*", out[0].lower()):
        out.insert(0, "拯救者")
    disp = f"{brand} " + " ".join(out)
    return brand, brand + "|" + "".join(o.lower() for o in out), disp


UNIT_RANGE = {"蛋白粉": (60, 1500), "肌酸": (100, 3000)}  # 合理的每公斤价格范围（元）
CAT_ORDER = ["显卡", "手机", "笔记本", "蛋白粉", "肌酸"]
LAPTOP_BRAND_ORDER = [b for b, _ in LAPTOP_BRANDS]
GPU_SERIES_ORDER = ["RTX 50 系列", "RX 9000 系列", "Intel Arc", "RTX 40 系列", "RX 7000 系列", "国产显卡", "更早型号", "专业卡"]
PHONE_BRAND_ORDER = [b for b, _ in PHONE_BRANDS]


def build_models(days: list[str], daily: list[dict], prod: dict, cut: str) -> None:
    """site/models.json：显卡按芯片、手机按机型汇总，每个型号每天的全网最低价（所有规格里最便宜的）。"""
    groups: dict[str, dict] = {}
    for pid, v in prod.items():
        if v.get("s", "") < cut:
            continue
        if v.get("c") == "显卡":
            r = gpu_chip(v["n"])
            if not r:
                continue
            key, disp, series = "显卡|" + r[0], r[0], r[1]
        elif v.get("c") == "手机":
            r = phone_model(v["n"])
            if not r:
                continue
            series, key, disp = r[0], "手机|" + r[1], r[2]
        elif v.get("c") == "笔记本电脑":
            r = laptop_model(v["n"])
            if not r:
                continue
            series, key, disp = r[0], "笔记本|" + r[1], r[2]
            g = groups.setdefault(key, {"cat": "笔记本", "key": key, "series": series, "names": Counter(), "skus": []})
            g["names"][disp] += 1
            g["skus"].append((pid, v["n"], daily_series(daily, pid)))
            continue
        elif v.get("c") == "运动营养":
            r = supplement_group(v["n"])
            if not r:
                continue
            cat, series, key, disp, kg = r
            series_p = daily_series(daily, pid)
            lo, hi = UNIT_RANGE[cat]
            if kg and series_p[-1] is not None and not lo <= series_p[-1] / kg <= hi:
                continue  # 每公斤价格离谱的多半是标错规格或试用装
            g = groups.setdefault(key, {"cat": cat, "key": key, "series": series, "names": Counter(), "skus": [], "kg": kg})
            g["names"][disp] += 1
            g["skus"].append((pid, v["n"], series_p))
            continue
        else:
            continue
        g = groups.setdefault(key, {"cat": v["c"], "key": key, "series": series, "names": Counter(), "skus": []})
        g["names"][disp] += 1
        g["skus"].append((pid, v["n"], daily_series(daily, pid)))
    out = []
    for g in groups.values():
        skus = g["skus"]
        cur = [s[2][-1] for s in skus if s[2] and s[2][-1] is not None]
        if len(cur) >= 3:  # 价格远离中位数的多半是配件、套装或标错，不参与最低价
            med = statistics.median(cur)
            skus = [s for s in skus if s[2][-1] is None or med * 0.4 <= s[2][-1] <= med * 3]
        hist = []
        for i in range(len(days)):
            vals = [s[2][i] for s in skus if s[2][i] is not None]
            hist.append(min(vals) if vals else None)
        priced = sorted((s for s in skus if s[2] and s[2][-1] is not None), key=lambda s: s[2][-1])
        if not priced:
            continue
        name = max(g["names"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        m = {"cat": g["cat"], "key": g["key"], "name": name, "series": g["series"], "hist": hist,
             "skus": [[s[0], s[1], s[2][-1]] for s in priced]}
        if g.get("kg"):
            m["kg"] = g["kg"]
            m["unit"] = round(priced[0][2][-1] / g["kg"], 1)  # 每公斤价格
        out.append(m)
    def order(m):
        if m["cat"] in UNIT_RANGE:
            return (CAT_ORDER.index(m["cat"]), 0, m.get("unit") or 1e9)
        if m["cat"] == "显卡":
            s = GPU_SERIES_ORDER.index(m["series"]) if m["series"] in GPU_SERIES_ORDER else 99
        elif m["cat"] == "笔记本":
            s = LAPTOP_BRAND_ORDER.index(m["series"]) if m["series"] in LAPTOP_BRAND_ORDER else 99
        else:
            s = PHONE_BRAND_ORDER.index(m["series"]) if m["series"] in PHONE_BRAND_ORDER else 99
        return (CAT_ORDER.index(m["cat"]) if m["cat"] in CAT_ORDER else 9, s, -(m["skus"][0][2] or 0))
    out.sort(key=order)
    state = load_json(CAT_DIR / "state.json", {})
    save_json(SITE_DIR / "models.json", {"updatedAt": state.get("lastRun"), "days": days, "models": out}, compact=True)


def build_site(status: dict) -> int:
    build_catalog()
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


SEARCH_URL = {"京东": "https://so.m.jd.com/ware/search.action?keyword=", "天猫": "https://list.tmall.com/search_product.htm?q=",
              "淘宝": "https://s.taobao.com/search?q=", "拼多多": "https://mobile.yangkeduo.com/search_result.html?search_key="}


def buy_link(mall: str, title: str) -> str:
    """命中提醒用的商城搜索链接（订阅源里没有商品直链）。"""
    from urllib.parse import quote
    name = re.sub(r"\s+\d+(?:\.\d{1,2})?元\S{0,40}\s*$", "", title).strip()
    name = re.sub(r"^[^：:\s]{1,16}[：:]\s*", "", name)[:50]
    plat = next((k for k in SEARCH_URL if k in (mall or "")), "淘宝")
    return SEARCH_URL[plat] + quote(name)


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
            HITS.append({"kw": " ".join(words), "max": cap, "price": price, "title": it["t"], "link": buy_link(it.get("m", ""), it["t"])})
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
    if "--catalog-plan" in args:
        n = arg("--catalog-plan")
        print(json.dumps(catalog_plan(int(n) if n and n.isdigit() else 30), ensure_ascii=False))
        return 0
    if "--catalog-dir" in args:  # 商品目录单独运行：不动好价数据和 status.json
        res = merge_catalog(arg("--catalog-dir") or "")
        res["published"] = build_catalog()
        print(json.dumps(res, ensure_ascii=False))
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
