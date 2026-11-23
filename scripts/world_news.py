"""해외 경제 뉴스 줄띠용 헤드라인 수집·게시 (CNN·BBC·CNBC 등 RSS). 제목·출처·링크만 다룬다(본문 복사 없음).

사용법
  python world_news.py fetch                  # 후보를 골라 world_pending.json 에 저장하고 목록을 출력
  python world_news.py publish [ko.json]      # world_pending.json 을 genitrader-news/world.json 으로 게시
        ko.json : {"<link>": "한국어 번역 제목", ...}  (없으면 영어 제목만 게시)
  python world_news.py auto                   # fetch + 영어 제목 그대로 publish (AI 없이 동작)
옵션 --dry-run : 저장소를 건드리지 않고 결과만 출력
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CI = bool(os.environ.get("WN_CI"))  # GitHub Actions 에서는 WN_CI=1, WN_REPO=저장소 폴더
PENDING = os.path.join(os.environ.get("RUNNER_TEMP") or HERE, "world_pending.json")
LOG = os.path.join(os.environ.get("RUNNER_TEMP") or HERE, "publish.log")
REPO = os.environ.get("WN_REPO") or r"C:\Users\User\Downloads\Video Pro\genitrader-news"
KST = timezone(timedelta(hours=9))
GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
FEEDS = [  # (출처, URL, 일반뉴스 피드면 True → 경제 키워드가 있는 기사만 채택)
    # CNN 공식 RSS 는 2017년에 멈춰 있어 구글 뉴스 검색 피드로 CNN 기사만 가져온다(링크는 구글을 거쳐 CNN 기사로 이동)
    ("CNN", GN + "site:cnn.com%20business%20when:2d", False),
    ("CNN", GN + "site:cnn.com%20(economy%20OR%20markets%20OR%20stocks%20OR%20Fed%20OR%20tariffs%20OR%20gold)%20when:2d", False),
    ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml", False),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml", True),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html", False),
    ("CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html", False),
    ("Guardian", "https://www.theguardian.com/business/rss", False),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", False),
    # 러시아
    ("Moscow Times", "https://www.themoscowtimes.com/rss/news", True),
    ("TASS", "https://tass.com/rss/v2.xml", True),
    # 중동
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", True),
    ("Arab News", "https://www.arabnews.com/rss.xml", True),
    # 동남아
    ("CNA", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", True),
    ("Straits Times", "https://www.straitstimes.com/news/business/rss.xml", False),
    ("Bangkok Post", "https://www.bangkokpost.com/rss/data/business.xml", False),
    # 유럽
    ("DW", "https://rss.dw.com/rdf/rss-en-bus", False),
    ("Euronews", "https://www.euronews.com/rss?level=theme&name=business", False),
    # 남미
    ("MercoPress", "https://en.mercopress.com/rss", True),
    ("Buenos Aires Times", "https://www.batimes.com.ar/feed", True),
    # 그 밖의 아시아
    ("SCMP", "https://www.scmp.com/rss/92/feed", True),
    ("Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms", True),
]
ECON = re.compile(r"\b(econom\w*|market\w*|stocks?|shares|oil|gas|gold|trade|tariffs?|bank\w*|inflation|rates?|currenc\w*|ruble|rouble|dollar|euro|yuan|energy|invest\w*|gdp|budget|sanctions?|export\w*|import\w*|tech\w*|ai|chips?|prices?|business\w*|compan\w*|funds?|debt|bonds?|crypto\w*|bitcoin|growth|jobs|wages?|opec|fed|ecb|imf|supply|factory|industr\w*|deals?|merger|profit\w*|loss\w*|revenue|tax\w*|crisis|recession|billion|million|trillion)\b", re.I)
BAD = re.compile(r"\b(horoscope|recipe|celebrity|royal|kardashian|taylor swift|nfl|nba|mlb|soccer|premier league|oscars?|grammy|sponsored|deals?|coupon|black friday|parenting|should (i|we)|my (wife|husband)|retirement|401\(k\)|annuity|social security|medicare|tennis|travel nightmare|take-home pay|marries|dies aged|grand theft auto|columnist)\b", re.I)
MAX_ITEMS = 30
MAX_AGE_H = 48
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} [world] {msg}\n")
    except Exception:
        pass


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")


def tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", block, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def clean(s: str, n: int) -> str:
    return CTRL.sub("", re.sub(r"\s+", " ", s.replace("]]>", ""))).strip()[:n]


def collect() -> tuple[list[dict], list[str]]:
    now = datetime.now(timezone.utc)
    per, errors = [], []
    for outlet, url, general in FEEDS:
        rows = []
        try:
            xml = get(url)
        except Exception as e:  # 한 곳이 죽어도 나머지로 진행
            errors.append(f"{outlet}: {e}")
            continue
        for b in re.findall(r"<item>.*?</item>", xml, re.S)[:25]:
            title, link = clean(tag(b, "title"), 160), tag(b, "link")
            if not title or not link.startswith("https://") and not link.startswith("http://") or BAD.search(title) or (general and not ECON.search(title)):
                continue
            link = link.replace("http://", "https://", 1).split("?at_medium")[0]
            if "/make-it/" in link: continue
            title = re.sub(r"\s+[-|]\s+(CNN( Business)?|CNN)$", "", title)
            title = re.sub(r"\s*\|\s*CNN( Business)?$", "", title)
            try:
                ts = parsedate_to_datetime(tag(b, "pubDate"))
            except Exception:
                ts = None
            if ts and (now - ts.astimezone(timezone.utc)).total_seconds() > MAX_AGE_H * 3600:
                continue
            rows.append({"s": outlet, "t": title, "u": link, "ts": ts.timestamp() if ts else 0})
        per.append(sorted(rows, key=lambda r: r["ts"], reverse=True))
    # 출처가 골고루 섞이도록 돌아가며 뽑고, 같은 링크·거의 같은 제목은 건너뛴다
    out, seen_u, seen_t = [], set(), set()
    i = 0
    while len(out) < MAX_ITEMS and any(i < len(p) for p in per):
        for p in per:
            if i < len(p) and len(out) < MAX_ITEMS:
                r = p[i]
                k = re.sub(r"\W+", "", r["t"].lower())[:40]
                if r["u"] in seen_u or k in seen_t:
                    continue
                seen_u.add(r["u"]); seen_t.add(k); out.append(r)
        i += 1
    return out, errors


CACHE = os.path.join(REPO if CI else HERE, "world_ko_cache.json")


def translate(text: str) -> str:
    import urllib.parse
    url = "https://api.mymemory.translated.net/get?langpair=en%7Cko&q=" + urllib.parse.quote(text[:200])
    try:
        d = json.loads(get(url))
        if d.get("responseStatus") != 200 or d.get("quotaFinished"):
            return ""
        t = clean(html.unescape(d["responseData"]["translatedText"]), 120)
        # 한글이 거의 없으면(미번역·오류 문구) 버린다
        return t if len(re.findall(r"[가-힣]", t)) >= 4 and "MYMEMORY" not in t.upper() else ""
    except Exception:
        return ""


def auto_translate(items: list[dict]) -> dict:
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        cache = {}
    live = {r["u"] for r in items}
    fails = 0
    for r in items:
        if r["u"] in cache:
            continue
        k = translate(r["t"])
        if k:
            cache[r["u"]] = k
        else:
            fails += 1
            if fails >= 3:  # 한도 초과 등 — 나머지는 영어로 둔다
                break
    keep = {u: cache[u] for u in cache if u in live}
    old = {u: k for u, k in cache.items() if u not in live}
    # 오래된 캐시는 최근 300개만 유지
    merged = dict(list(old.items())[-300:]); merged.update(keep)
    json.dump(merged, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return keep


def git(*a: str) -> str:
    pre = [] if CI else ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    r = subprocess.run(["git", "-C", REPO, *pre, *a],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    if r.returncode:
        raise RuntimeError(f"git {a[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def publish(ko: dict, dry: bool) -> int:
    pend = json.load(open(PENDING, encoding="utf-8"))
    items = []
    for r in pend["items"]:
        e = {"s": r["s"], "t": r["t"], "u": r["u"]}
        k = clean(str(ko.get(r["u"], "")), 120)
        if k:
            e["k"] = k
        items.append(e)
    if len(items) < 4:
        log(f"FAIL 기사 {len(items)}건(<6) — 게시 안 함")
        print(f"FAIL 기사가 너무 적음({len(items)})")
        return 1
    now = datetime.now(KST)
    data = {"updated": now.isoformat(timespec="minutes"), "label": "WORLD NEWS", "items": items}
    if dry:
        print(json.dumps(data, ensure_ascii=False, indent=1)); return 0
    try:
        git("pull", "--ff-only", "origin", "main")
        with open(os.path.join(REPO, "world.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        files = ["world.json"] + (["world_ko_cache.json"] if CI and os.path.exists(os.path.join(REPO, "world_ko_cache.json")) else [])
        git("add", *files)
        if not git("status", "--porcelain", *files).strip():
            print("OK 변경 없음"); return 0
        git("commit", "-m", f"해외 뉴스 갱신 {now:%Y-%m-%d %H:%M}")
        git("push", "origin", "HEAD:main")
    except Exception as e:
        log(f"FAIL {e}"); print(f"FAIL {e}"); return 1
    log(f"OK {len(items)}건 게시 (한국어 {sum('k' in i for i in items)}건)")
    print(f"OK {len(items)}건 게시")
    return 0


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    mode = argv[1] if len(argv) > 1 else ""
    dry = "--dry-run" in argv
    if mode in ("fetch", "auto"):
        items, errs = collect()
        json.dump({"items": items}, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if mode == "fetch":
            for r in items:
                print(f'{r["s"]}\t{r["u"]}\t{r["t"]}')
            if errs: print("ERRORS:", errs)
            return 0 if items else 1
        if errs: log("수집 오류: " + "; ".join(errs))
        return publish(auto_translate(items), dry)
    if mode == "publish":
        paths = [a for a in argv[2:] if not a.startswith("--")]
        ko = json.load(open(paths[0], encoding="utf-8")) if paths else {}
        return publish(ko, dry)
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
