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
  PING_ROLE_ID=…    optional: Discord role id to @-ping on IMPORTANT news
                    (new codes, maintenance, banners, launch). No ping if unset.
"""

import os
import re
import sys
import json
import time
import html
import pathlib
import datetime as dt
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

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
PING_ROLE_ID = os.environ.get("PING_ROLE_ID", "").strip()
KEEP_IDS = 300     # how many recent item-ids to remember per source
KEEP_DEDUP = 500   # how many recent content-keys to remember globally
DEDUP_KEY = "__dedup__"  # reserved state key (never a source name)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")  # display times in Vietnam time


def log(*a):
    print(*a, flush=True)


def fmt_vn(ts):
    """UTC/aware datetime → 'dd/mm/YYYY HH:MM' in Vietnam time, or '' if none."""
    if not ts:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M")


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
# Order matters — most specific topics first, generic "guide" last.
TOPIC_EMOJI = [
    (r"code|redeem|reward|gift|voucher", "🎁"),
    (r"maintenance|hotfix|patch ?notes?|server", "🛠️"),
    (r"banner|gacha|pull|wish|rate ?up", "🎰"),
    (r"event|festival|celebrat", "🎉"),
    (r"notice|account|action|\bban\b|penal", "📢"),
    (r"launch|release|out now|steam|epic", "🚀"),
    (r"showcase|character|trailer|preview|teaser|reveal|impression", "✨"),
    (r"tier|meta|ranking|nerf|buff", "📊"),
    (r"update|version|v?\d+\.\d+", "🚀"),
    (r"build|guide|team|comp|kit|best", "🔥"),
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


# Topics that are worth an @-role ping (opt-in via PING_ROLE_ID).
IMPORTANT_RE = re.compile(
    r"(?i)\b(codes?|redeem|giftcodes?|maintenance|hotfix|patch ?notes?|"
    r"banners?|launch|release|out now|drop ?rate|compensat)\b")


def is_important(title):
    return bool(IMPORTANT_RE.search(title or ""))


# Words that look like codes but aren't — never surface these as "codes".
CODE_STOP = {
    "NEVERNESS", "EVERNESS", "HETHEREAU", "STEAM", "HTTPS", "HTML", "REDEEM",
    "CODES", "CODE", "GIFT", "GIFTCODE", "PATCH", "NOTES", "UPDATE", "GLOBAL",
    "OFFICIAL", "REWARD", "REWARDS", "SERVER", "GAMES", "TWITCH", "GEFORCE",
}
CODE_RE = re.compile(r"\b([A-Z][A-Z0-9]{4,19})\b")


def extract_codes(title, summary):
    """Best-effort redeem-code extraction. Only runs when the text is clearly
    about codes, so random ALL-CAPS words don't get mistaken for codes."""
    text = f"{title} {summary}"
    if not re.search(r"(?i)\b(code|redeem|giftcode|coupon)\b", text):
        return []
    codes, seen = [], set()
    for tok in CODE_RE.findall(text):
        if tok in CODE_STOP or tok in seen:
            continue
        # a real code usually has a digit, or is a longer coined word
        if re.search(r"\d", tok) or len(tok) >= 7:
            seen.add(tok)
            codes.append(tok)
    return codes[:6]


# ── cross-source de-duplication ──────────────────────────────────────────────
_SITE_SUFFIX = re.compile(r"\s*[|\-–—]\s*[^|\-–—]{1,40}$")  # " - IGN", " | PCGamer"


def canon_url(link):
    """Normalize a URL for dedup: drop scheme/query/fragment, www., trailing /."""
    try:
        p = urlsplit(link or "")
        host = (p.netloc or "").lower().removeprefix("www.")
        path = (p.path or "").rstrip("/")
        return f"{host}{path}" if host else ""
    except Exception:
        return ""


def norm_title(title):
    """Normalize a headline for dedup: strip publisher suffix + punctuation."""
    t = _SITE_SUFFIX.sub("", title or "")
    t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    return t


def content_keys(item):
    """Keys identifying the underlying STORY, so the same news reported by
    several sources posts only once. Google links are opaque, so the title
    key does the heavy lifting there."""
    keys = []
    u = canon_url(item.get("link"))
    # google-news redirect urls are per-aggregator, not per-story → skip them
    if u and "news.google.com" not in u:
        keys.append("u:" + u)
    nt = norm_title(item.get("title"))
    if len(nt) >= 12:  # too-short titles collide by accident; skip them
        keys.append("t:" + nt)
    return keys


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
            # the real publisher (IGN, AUTOMATON…) for aggregators like Google News
            "publisher": (e.get("source") or {}).get("title"),
        })
    return items


# Steam BBCode image token → real CDN url.
STEAM_CLAN = "https://clan.akamai.steamstatic.com/images/"


def fetch_steam(appid):
    """Official Steam 'Community Announcements' via the public ISteamNews API
    (the store RSS feed 404s until a game is released)."""
    api = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
           f"?appid={appid}&count=15&maxlength=600")
    data = requests.get(api, headers=HEADERS, timeout=30).json()
    items = []
    for it in data.get("appnews", {}).get("newsitems", []):
        body = it.get("contents", "") or ""
        m = re.search(r"\[img\]([^\[]+)\[/img\]", body, re.I)
        img = m.group(1).replace("{STEAM_CLAN_IMAGE}", STEAM_CLAN) if m else None
        ts = None
        if it.get("date"):
            ts = dt.datetime.fromtimestamp(int(it["date"]), tz=dt.timezone.utc)
        items.append({
            "id": str(it.get("gid") or it.get("url")),
            "title": (it.get("title") or "(no title)").strip(),
            "link": it.get("url") or "",
            "summary": re.sub(r"\[/?[^\]]+\]", " ", body),  # strip BBCode
            "image": img,
            "ts": ts,
            "publisher": it.get("feedlabel"),
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
    t = src.get("type")
    if t == "pw_scrape":
        return fetch_pw(src["url"])
    if t == "steam_news":
        return fetch_steam(src["appid"])
    return fetch_rss(src["url"])


def apply_filters(items, src):
    """Keep only items whose title passes the source's include/exclude regex
    (used to strip noise, e.g. Reddit memes/megathreads from the NEWS feed)."""
    inc, exc = src.get("include"), src.get("exclude")
    if not inc and not exc:
        return items
    kept = []
    for it in items:
        title = it.get("title") or ""
        if inc and not re.search(inc, title):
            continue
        if exc and re.search(exc, title):
            continue
        kept.append(it)
    return kept


# ── discord ──────────────────────────────────────────────────────────────────
def _send(payload, image_url=None):
    """Send a webhook message. If image_url is given, the image is uploaded as
    an ATTACHMENT (renders bigger/wider than an embed image) below the embed."""
    payload.setdefault("username", "NTE News")
    payload.setdefault("allowed_mentions", {"parse": []})

    attach = None
    if image_url:
        try:
            resp = requests.get(image_url, headers=HEADERS, timeout=30)
            ct = resp.headers.get("content-type", "").split(";")[0]
            if resp.ok and ct.startswith("image"):
                ext = {"image/png": "png", "image/webp": "webp",
                       "image/gif": "gif"}.get(ct, "jpg")
                attach = (f"news.{ext}", resp.content, ct)
        except Exception:
            attach = None

    for _ in range(5):
        if attach:
            r = requests.post(WEBHOOK, data={"payload_json": json.dumps(payload)},
                              files={"file": attach}, timeout=60)
        else:
            r = requests.post(WEBHOOK, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate-limiting")


def ping_content(item, source):
    """A '<@&role>' lead line when the news is important AND pinging is enabled
    (PING_ROLE_ID set + the source didn't opt out). Empty string otherwise."""
    if not (PING_ROLE_ID and source.get("ping", True)):
        return "", None
    codes = extract_codes(item["title"], item.get("summary"))
    if not (codes or is_important(item["title"])):
        return "", None
    lead = "🎁 **Code mới!**" if codes else "📢 **Tin quan trọng!**"
    return f"<@&{PING_ROLE_ID}> {lead}", {"parse": [], "roles": [PING_ROLE_ID]}


def post_discord(item, source):
    emoji = source.get("emoji", "")
    ping, mentions = ping_content(item, source)

    # Video sources: post the link as message content so Discord renders a
    # PLAYABLE inline player (rich embeds never play video). Shorts → watch.
    if source.get("video"):
        link = normalize_youtube(item["link"])
        header = f"{emoji} **{item['title']}**".strip()
        lead = f"{ping}\n" if ping else ""
        payload = {"content": f"{lead}{header}\n{link}"[:2000]}
        if mentions:
            payload["allowed_mentions"] = mentions
        _send(payload)
        return

    # Everything else: a clean, wide rich embed with a big image.
    short_src = source["name"].split("—")[-1].strip() or source["name"]
    outlet = (item.get("publisher") or "").strip()  # real byline for aggregators
    author_name = f'{emoji} {source["name"]}'.strip()
    if outlet and outlet.lower() not in author_name.lower():
        author_name = f"{author_name} · {outlet}"

    now_vn = dt.datetime.now(VN_TZ).strftime("%H:%M")
    embed = {
        "title": f'{topic_emoji(item["title"])} {item["title"]}'[:256],
        "url": item["link"] or None,
        "color": int(source.get("color", 0x5865F2)),
        "author": {"name": author_name[:256]},
        "footer": {"text": f"Neverness to Everness • auto-news • {now_vn}"},
    }
    if source.get("icon"):
        embed["author"]["icon_url"] = source["icon"]

    desc = clean_summary(item.get("summary"))
    if desc:
        embed["description"] = desc

    fields = []
    # Redeem codes, if any, get a prominent full-width field at the top.
    codes = extract_codes(item["title"], item.get("summary"))
    if codes:
        fields.append({"name": "🎁 Code",
                       "value": " ".join(f"`{c}`" for c in codes), "inline": False})

    # A row of inline fields — informative AND it forces the embed to widen,
    # filling that empty horizontal space instead of staying thin.
    when = fmt_vn(item.get("ts"))
    if when:
        fields.append({"name": "📅 Đăng lúc", "value": when, "inline": True})
    fields.append({"name": "📡 Nguồn", "value": outlet or short_src, "inline": True})
    if item.get("link"):
        fields.append({"name": "🔗 Chi tiết",
                       "value": f"[Mở bài viết ›]({item['link']})", "inline": True})
    embed["fields"] = fields

    # Prefer a wide og:image banner over a portrait inline image, then attach
    # it as a big image UNDER the embed (attachments render wider than embed
    # images → the card looks large and fills the max width Discord allows).
    image = None
    if source.get("og_image") and item.get("link"):
        image = fetch_og_image(item["link"])
    image = image or item.get("image") or source.get("default_image")

    payload = {"embeds": [embed]}
    if ping:
        payload["content"] = ping
        payload["allowed_mentions"] = mentions
    _send(payload, image_url=image)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    if not DRY_RUN and not WEBHOOK:
        log("ERROR: DISCORD_WEBHOOK is not set (and DRY_RUN is off).")
        sys.exit(1)

    sources = yaml.safe_load(FEEDS_PATH.read_text("utf-8")) or []
    state = load_state()
    posted_total = 0
    changed = False

    # Global content-keys already posted (across ALL sources) — the same story
    # aggregated by Google News, neverness.gg and PW official now posts once.
    dedup_list = state.get(DEDUP_KEY, [])
    dedup_set = set(dedup_list)

    def try_post(it, src, tag=""):
        """Post an item unless its story was already posted by another source.
        Returns True if it went out, False if suppressed as a duplicate."""
        keys = content_keys(it)
        if any(k in dedup_set for k in keys):
            log(f"⊘ dup{tag} [{src['name']}] {it['title']}")
            return False
        post_discord(it, src)
        for k in keys:
            if k not in dedup_set:
                dedup_set.add(k)
                dedup_list.append(k)
        time.sleep(1.0)  # be gentle with the webhook
        log(f"→ posted{tag} [{src['name']}] {it['title']}")
        return True

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

        items = apply_filters(items, src)
        if not items:
            log(f"· {name}: 0 items (after filter)")
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
                if try_post(it, src, tag=" [seed]"):
                    posted_total += 1
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
            if try_post(it, src):
                posted_total += 1

        merged = ids_now + [i for i in seen if i not in set(ids_now)]
        state[name] = merged[:KEEP_IDS]
        changed = True

    if not DRY_RUN and changed:
        state[DEDUP_KEY] = dedup_list[-KEEP_DEDUP:]
        save_state(state)
    log(f"\nDone. Posted {posted_total} new item(s).")


if __name__ == "__main__":
    main()
