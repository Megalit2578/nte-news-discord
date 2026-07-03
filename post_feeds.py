#!/usr/bin/env python3
"""
NTE News → Discord.

Polls every source in feeds.yml (RSS feeds + the official Perfect World news
page) and posts new items to a Discord channel through a webhook.

Why this is free & unlimited: you own the webhook and the runner (GitHub
Actions), so there is no 3-feed cap like the public MonitoRSS bot.

Environment variables:
  DISCORD_WEBHOOK   (required unless DRY_RUN)  the Discord webhook URL
  DRY_RUN=1         parse feeds and print what WOULD be posted; touches neither
                    Discord nor the saved state (great for local testing)
  MAX_PER_RUN=8     safety cap on posts per source per run (default 8)
"""

import os
import re
import sys
import json
import time
import html
import pathlib
import datetime as dt
from urllib.parse import urljoin

import requests
import feedparser
import yaml

# Titles contain CJK / emoji; make console output UTF-8 safe on every OS.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).parent
STATE_PATH = ROOT / "state" / "seen.json"
FEEDS_PATH = ROOT / "feeds.yml"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "*/*"}

DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "8"))
KEEP_IDS = 300  # how many recent item-ids to remember per source


def log(*a):
    print(*a, flush=True)


def strip_html(text, limit=350):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


YT_SHORTS_RE = re.compile(r"/shorts/([A-Za-z0-9_-]+)")


def normalize_youtube(link):
    """/shorts/ID → /watch?v=ID so Discord always shows a playable player."""
    m = YT_SHORTS_RE.search(link or "")
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else link


def extract_image(entry):
    """Best representative image URL from an RSS entry, or None."""
    best, best_w = None, -1
    for mc in (entry.get("media_content") or []):
        u, w = mc.get("url"), int(mc.get("width") or 0)
        if u and w >= best_w:
            best, best_w = u, w
    for mt in (entry.get("media_thumbnail") or []):
        u, w = mt.get("url"), int(mt.get("width") or 0)
        if u and w >= best_w:
            best, best_w = u, w
    if best:
        return best
    for l in (entry.get("links") or []):
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    blob = entry["content"][0].get("value", "") if entry.get("content") else ""
    blob = blob or entry.get("summary", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', blob)
    return m.group(1) if m else None


# A topical emoji so each post reads at a glance and feels lively.
TOPIC_EMOJI = [
    (r"build|guide|team|comp|kit", "🔥"),
    (r"banner|gacha|pull|wish|rate", "🎰"),
    (r"code|redeem|reward|gift|voucher", "🎁"),
    (r"maintenance|server|hotfix|patch|update|version|v?\d+\.\d+", "🛠️"),
    (r"event|festival|celebrat", "🎉"),
    (r"notice|account|action|ban|penal", "📢"),
    (r"showcase|character|trailer|preview|teaser|reveal|impression", "✨"),
    (r"tier|meta|ranking|best", "📊"),
    (r"launch|release|out now|steam|epic", "🚀"),
]


def topic_emoji(title):
    t = (title or "").lower()
    for pat, emo in TOPIC_EMOJI:
        if re.search(pat, t):
            return emo
    return "📰"


def clean_summary(text, limit=480):
    """Readable description: drop tags + WordPress 'The post … appeared first' junk."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"(?is)\bthe post\b.*?\bappeared first on\b.*$", "", text)
    text = text.replace("[…]", " ").replace("[&hellip;]", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") + " …"
    return text


def fetch_og_image(url):
    """The article's og:image — usually a WIDE banner, so the embed renders
    at full width instead of tall-and-thin. Returns None on any failure."""
    try:
        text = requests.get(url, headers=HEADERS, timeout=20).text
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', text, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', text, re.I))
        return m.group(1).strip() if m and m.group(1).strip() else None
    except Exception:
        return None


def load_state():
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ── source fetchers ──────────────────────────────────────────────────────────
def fetch_rss(url):
    """Return newest-first list of dict(id,title,link,summary,thumb,ts)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items = []
    for e in parsed.entries:
        link = e.get("link") or ""
        eid = e.get("id") or e.get("guid") or link or e.get("title")
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                ts = dt.datetime(*e[key][:6], tzinfo=dt.timezone.utc)
                break
        items.append({
            "id": str(eid),
            "title": (e.get("title") or "(no title)").strip(),
            "link": link,
            "summary": e.get("summary") or e.get("description") or "",
            "image": extract_image(e),
            "ts": ts,
        })
    return items


# Matches each article card on the Perfect World news index.
PW_ITEM_RE = re.compile(
    r'<a\s+href="(?P<href>/en/article/news/[^"]+\.html)">\s*'
    r'<div\s+class="listItem">.*?'
    r'<h2\s+class="title">(?P<title>.*?)</h2>.*?'
    r'<p\s+class="date">(?P<date>[^<]*)</p>',
    re.S,
)


def fetch_pw(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    items = []
    for m in PW_ITEM_RE.finditer(resp.text):
        link = urljoin(url, m.group("href"))
        ts = None
        date = (m.group("date") or "").strip()
        try:
            ts = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        except Exception:
            pass
        items.append({
            "id": link,
            "title": strip_html(m.group("title"), 250),
            "link": link,
            "summary": "",
            "thumb": None,
            "ts": ts,
        })
    return items


def fetch(src):
    if src.get("type") == "pw_scrape":
        return fetch_pw(src["url"])
    return fetch_rss(src["url"])


# ── discord ──────────────────────────────────────────────────────────────────
def _send(payload):
    payload.setdefault("username", "NTE News")
    payload.setdefault("allowed_mentions", {"parse": []})
    for _ in range(5):
        r = requests.post(WEBHOOK, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate-limiting")


def post_discord(item, source):
    emoji = source.get("emoji", "")

    # Video sources: post the link as message content so Discord renders a
    # PLAYABLE inline player (rich embeds never play video). Shorts → watch.
    if source.get("video"):
        link = normalize_youtube(item["link"])
        header = f"{emoji} **{item['title']}**".strip()
        _send({"content": f"{header}\n{link}"[:2000]})
        return

    # Everything else: a clean, wide rich embed with a big image.
    short_src = source["name"].split("—")[-1].strip() or source["name"]
    embed = {
        "title": f'{topic_emoji(item["title"])} {item["title"]}'[:256],
        "url": item["link"] or None,
        "color": int(source.get("color", 0x5865F2)),
        "author": {"name": f'{emoji} {source["name"]}'.strip()[:256]},
        "footer": {"text": "Neverness to Everness • auto-news"},
    }
    if source.get("icon"):
        embed["author"]["icon_url"] = source["icon"]

    desc = clean_summary(item.get("summary"))
    if desc:
        embed["description"] = desc

    # A row of inline fields — informative AND it forces the embed to widen,
    # filling that empty horizontal space instead of staying thin.
    fields = []
    if item.get("ts"):
        fields.append({"name": "📅 Đăng lúc",
                       "value": item["ts"].strftime("%d/%m/%Y"), "inline": True})
    fields.append({"name": "📡 Nguồn", "value": short_src, "inline": True})
    if item.get("link"):
        fields.append({"name": "🔗 Chi tiết",
                       "value": f"[Mở bài viết ›]({item['link']})", "inline": True})
    embed["fields"] = fields

    # Prefer a wide og:image banner (fills the embed width) over a portrait
    # inline image that leaves the embed tall and narrow.
    image = None
    if source.get("og_image") and item.get("link"):
        image = fetch_og_image(item["link"])
    image = image or item.get("image") or source.get("default_image")
    if image:
        embed["image"] = {"url": image}

    _send({"embeds": [embed]})


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    if not DRY_RUN and not WEBHOOK:
        log("ERROR: DISCORD_WEBHOOK is not set (and DRY_RUN is off).")
        sys.exit(1)

    sources = yaml.safe_load(FEEDS_PATH.read_text("utf-8")) or []
    state = load_state()
    posted_total = 0
    changed = False

    for src in sources:
        name = src["name"]
        if src.get("enabled") is False:
            log(f"— skip (disabled): {name}")
            continue

        try:
            items = fetch(src)
        except Exception as ex:
            level = "·" if src.get("optional") else "!"
            log(f"{level} {name}: fetch failed: {ex}")
            continue  # one bad source never kills the run

        if not items:
            log(f"· {name}: 0 items")
            continue

        if DRY_RUN:
            log(f"\n=== {name}: {len(items)} items (latest 3) ===")
            for it in items[:3]:
                when = it["ts"].date().isoformat() if it["ts"] else "—"
                log(f"   • [{when}] {it['title']}  <{it['link']}>")
            continue

        ids_now = [it["id"] for it in items]
        seen = state.get(name)
        if seen is None:
            # First time we see this source: remember everything so we don't
            # dump old articles into the channel. Optionally post the newest
            # SEED_POST items (default 0) as a visible "it works" proof.
            seed_post = int(os.environ.get("SEED_POST", "0"))
            for it in reversed(items[:seed_post]):   # oldest of the batch first
                post_discord(it, src)
                posted_total += 1
                time.sleep(1.0)
                log(f"→ posted [seed] [{name}] {it['title']}")
            state[name] = ids_now[:KEEP_IDS]
            changed = True
            log(f"✓ {name}: seeded {len(ids_now)} items "
                f"(posted {min(seed_post, len(items))})")
            continue

        seen_set = set(seen)
        new_items = [it for it in items if it["id"] not in seen_set]
        new_items.reverse()  # oldest first → channel reads chronologically
        if len(new_items) > MAX_PER_RUN:
            new_items = new_items[-MAX_PER_RUN:]

        for it in new_items:
            post_discord(it, src)
            posted_total += 1
            time.sleep(1.0)  # be gentle with the webhook
            log(f"→ posted [{name}] {it['title']}")

        merged = ids_now + [i for i in seen if i not in set(ids_now)]
        state[name] = merged[:KEEP_IDS]
        changed = True

    if not DRY_RUN and changed:
        save_state(state)
    log(f"\nDone. Posted {posted_total} new item(s).")


if __name__ == "__main__":
    main()
