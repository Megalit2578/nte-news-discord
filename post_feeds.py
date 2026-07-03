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
DIGEST = os.environ.get("DIGEST", "").strip() not in ("", "0", "false", "False")
TRANSLATE = os.environ.get("TRANSLATE", "1").strip() not in ("0", "false", "False")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "8"))
PING_ROLE_ID = os.environ.get("PING_ROLE_ID", "").strip()
KEEP_IDS = 300     # how many recent item-ids to remember per source
KEEP_DEDUP = 500   # how many recent content-keys to remember globally
KEEP_DIGEST = 120  # how many recent posts to keep for the daily digest
# Reserved state keys (never a source name).
DEDUP_KEY = "__dedup__"
CODES_MSG_KEY = "__codes_msg__"    # discord message id of the live "active codes" card
CODES_LIST_KEY = "__codes__"       # last-seen active code set (skip needless edits)
DIGEST_LOG_KEY = "__digest_log__"  # rolling log of posts for the daily digest

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


_tr_cache = {}


def translate_vi(text):
    """Translate to Vietnamese via the free Google Translate endpoint (no key).
    Returns None if translation is off, fails, is empty, or the text is already
    Vietnamese — so callers can safely fall back to the original."""
    if not TRANSLATE:
        return None
    text = (text or "").strip()
    if not text:
        return None
    if text in _tr_cache:
        return _tr_cache[text]
    result = None
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "vi", "dt": "t", "q": text},
            headers=HEADERS, timeout=15)
        if r.ok:
            d = r.json()
            vi = "".join(seg[0] for seg in d[0] if seg and seg[0]).strip()
            src = d[2] if len(d) > 2 else ""
            if vi and src != "vi" and vi.lower() != text.lower():
                result = vi
    except Exception:
        result = None
    _tr_cache[text] = result
    return result


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
    # Reddit throttles shared cloud IPs (429). A couple of short retries help it
    # squeeze through on some runs; if it still fails the source is optional.
    for _ in range(2):
        if resp.status_code != 429:
            break
        time.sleep(4)
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


# ── reddit via official OAuth (reliable — anonymous cloud IPs get 429/403) ────
REDDIT_UA = "web:nte-news-bot:1.1 (by /u/Megalit2578)"
_reddit_token = None


def _reddit_token_get():
    """App-only (client_credentials) token, or None if no creds are configured."""
    global _reddit_token
    if _reddit_token:
        return _reddit_token
    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    sec = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        return None
    r = requests.post("https://www.reddit.com/api/v1/access_token",
                      data={"grant_type": "client_credentials"},
                      auth=(cid, sec), headers={"User-Agent": REDDIT_UA}, timeout=20)
    r.raise_for_status()
    _reddit_token = r.json().get("access_token")
    return _reddit_token


def parse_reddit_json(payload):
    """Turn a Reddit listing JSON into our item dicts."""
    items = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        ts = (dt.datetime.fromtimestamp(d["created_utc"], tz=dt.timezone.utc)
              if d.get("created_utc") else None)
        img = None
        try:
            img = html.unescape(d["preview"]["images"][0]["source"]["url"])
        except Exception:
            img = d.get("thumbnail") if str(d.get("thumbnail", "")).startswith("http") else None
        items.append({
            "id": d.get("name") or d.get("id") or d.get("permalink"),
            "title": (d.get("title") or "(no title)").strip(),
            "link": "https://www.reddit.com" + d.get("permalink", ""),
            "summary": (d.get("selftext") or "")[:600],
            "image": img,
            "ts": ts,
            "publisher": None,
        })
    return items


def fetch_reddit(sub):
    """Newest posts of a subreddit through authenticated OAuth (no 429s)."""
    token = _reddit_token_get()
    if not token:
        raise RuntimeError("no reddit creds")   # caller falls back to anon RSS
    r = requests.get(f"https://oauth.reddit.com/r/{sub}/new?limit=25",
                     headers={"Authorization": f"bearer {token}",
                              "User-Agent": REDDIT_UA}, timeout=25)
    r.raise_for_status()
    return parse_reddit_json(r.json())


_REDDIT_SUB_RE = re.compile(r"reddit\.com/r/([^/]+)", re.I)


def fetch(src):
    t = src.get("type")
    if t == "pw_scrape":
        return fetch_pw(src["url"])
    if t == "steam_news":
        return fetch_steam(src["appid"])
    url = src["url"]
    m = _REDDIT_SUB_RE.search(url)
    if m and os.environ.get("REDDIT_CLIENT_ID"):
        try:
            return fetch_reddit(m.group(1))      # reliable path when creds exist
        except Exception as ex:
            log(f"· reddit oauth failed ({ex}); falling back to RSS")
    return fetch_rss(url)


def apply_filters(items, src):
    """Keep only items whose title passes the source's regex rules:
      include — must match to be considered at all (stay on-topic)
      keep    — whitelist that OVERRIDES exclude (real news beats guide-noise)
      exclude — drop as noise, unless `keep` already rescued it
    Used to de-noise busy feeds (Google News guides/tier-lists, Reddit memes)."""
    inc, keep, exc = src.get("include"), src.get("keep"), src.get("exclude")
    if not (inc or keep or exc):
        return items
    kept = []
    for it in items:
        title = it.get("title") or ""
        if inc and not re.search(inc, title):
            continue
        if exc and re.search(exc, title) and not (keep and re.search(keep, title)):
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

    # Vietnamese-first: translate the headline (and summary) for VN readers,
    # keeping the English original underneath. Falls back cleanly on failure.
    en_title = item["title"]
    vi_title = translate_vi(en_title) if source.get("translate", True) else None

    now_vn = dt.datetime.now(VN_TZ).strftime("%H:%M")
    embed = {
        "title": f'{topic_emoji(en_title)} {vi_title or en_title}'[:256],
        "url": item["link"] or None,
        "color": int(source.get("color", 0x5865F2)),
        "author": {"name": author_name[:256]},
        "footer": {"text": f"Neverness to Everness • auto-news • {now_vn}"},
    }
    if source.get("icon"):
        embed["author"]["icon_url"] = source["icon"]

    desc = clean_summary(item.get("summary"))
    vi_desc = translate_vi(desc) if (desc and source.get("translate", True)) else None
    parts = []
    if vi_title:                       # show the original English for reference
        parts.append(f"*{en_title}*")
    if vi_desc or desc:
        parts.append(vi_desc or desc)
    if parts:
        embed["description"] = "\n\n".join(parts)[:4096]

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


# ── active-codes tracker (a single, self-updating "live codes" message) ───────
CODE_TOKEN_RE = re.compile(r"<strong>\s*([A-Za-z0-9]{4,20})\s*</strong>")


def _codes_in(region):
    out = []
    for m in CODE_TOKEN_RE.finditer(region):
        tok = m.group(1).strip()
        if tok in CODE_STOP or tok in out:
            continue
        if re.search(r"\d", tok) or len(tok) >= 6:  # skip stray short words
            out.append(tok)
    return out


def fetch_active_codes(url):
    """Scrape the ACTIVE redeem codes (the section between the 'Active … Codes'
    and 'Expired … Codes' headings). The page also mentions those phrases in its
    table-of-contents, so we try every Active→Expired span and take the one that
    actually contains the most codes — robust to intro/TOC noise."""
    t = requests.get(url, headers=HEADERS, timeout=30).text
    expireds = [m.start() for m in re.finditer(r"(?i)expired[^<]{0,15}codes", t)]
    best = []
    for a in re.finditer(r"(?i)active[^<]{0,15}codes", t):
        end = min([x for x in expireds if x > a.start()] or [len(t)])
        codes = _codes_in(t[a.start():end])
        if len(codes) > len(best):
            best = codes
    return best


def _webhook_post_get_id(payload):
    """POST a webhook message and return its message id (needs ?wait=true)."""
    payload.setdefault("username", "NTE News")
    payload.setdefault("allowed_mentions", {"parse": []})
    r = requests.post(WEBHOOK + "?wait=true", json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("id")


def _webhook_edit(msg_id, payload):
    """Edit a previous webhook message in place. False if it no longer exists."""
    r = requests.patch(f"{WEBHOOK}/messages/{msg_id}", json=payload, timeout=30)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def codes_embed(codes, src):
    now = dt.datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M")
    body = "\n".join(f"🎁  **`{c}`**" for c in codes) or "_Hiện chưa có code nào._"
    return {
        "title": "🎁 Code NTE đang hoạt động",
        "url": src.get("url"),
        "color": int(src.get("color", 0xFFD700)),
        "description": f"{body}\n\n[Cách nhập code ›]({src.get('url')})",
        "footer": {"text": f"Nguồn: neverness.gg • cập nhật {now} (giờ VN)"},
    }


def update_codes_tracker(src, state):
    """Maintain ONE live 'active codes' message: create it once, then edit it in
    place whenever the code list changes (+ ping when a brand-new code appears)."""
    codes = fetch_active_codes(src["url"])
    prev = state.get(CODES_LIST_KEY, [])
    msg_id = state.get(CODES_MSG_KEY)
    changed = False

    if not msg_id:
        msg_id = _webhook_post_get_id({"embeds": [codes_embed(codes, src)]})
        state[CODES_MSG_KEY] = msg_id
        state[CODES_LIST_KEY] = codes
        log(f"✓ codes card created (id={msg_id}) — PIN this message once")
        return True

    if set(codes) != set(prev):
        if not _webhook_edit(msg_id, {"embeds": [codes_embed(codes, src)]}):
            # message was deleted → recreate
            state[CODES_MSG_KEY] = _webhook_post_get_id(
                {"embeds": [codes_embed(codes, src)]})
        new = [c for c in codes if c not in set(prev)]
        if prev and new:  # real-time alert for genuinely new codes
            lead = f"<@&{PING_ROLE_ID}> " if PING_ROLE_ID else ""
            _send({"content": f"{lead}🎁 **Code NTE mới:** "
                              + ", ".join(f"`{c}`" for c in new),
                   "allowed_mentions": {"parse": [],
                                        "roles": [PING_ROLE_ID] if PING_ROLE_ID else []}})
            log(f"→ new-code alert: {', '.join(new)}")
        state[CODES_LIST_KEY] = codes
        changed = True
        log(f"✓ codes updated: {len(codes)} active")
    else:
        log(f"· codes unchanged ({len(codes)} active)")
    return changed


# ── daily digest ─────────────────────────────────────────────────────────────
def digest_record(item, source):
    """One compact entry for the daily-digest log (VN-dated)."""
    short = source["name"].split("—")[-1].strip() or source["name"]
    return {
        "t": item.get("title", "")[:120],
        "u": item.get("link"),
        "s": short,
        "e": topic_emoji(item.get("title", "")),
        "d": dt.datetime.now(VN_TZ).strftime("%Y-%m-%d"),
    }


def run_digest(state):
    """Post a single round-up of everything published TODAY (VN date). Nothing
    posted → nothing to summarise, so it stays quiet."""
    today = dt.datetime.now(VN_TZ).strftime("%Y-%m-%d")
    todays = [e for e in state.get(DIGEST_LOG_KEY, []) if e.get("d") == today]
    if not todays:
        log("digest: no items today — skipping")
        return False

    groups = {}
    for e in todays:
        groups.setdefault(e.get("s", "News"), []).append(e)

    lines = []
    for name, entries in groups.items():
        lines.append(f"\n**{name}**")
        for e in entries[:12]:
            title = (e.get("t") or "").strip()
            url = e.get("u")
            lines.append(f"{e.get('e', '•')} [{title}]({url})" if url
                         else f"{e.get('e', '•')} {title}")

    dd = dt.datetime.now(VN_TZ).strftime("%d/%m/%Y")
    embed = {
        "title": f"📰 Tổng hợp NTE hôm nay — {dd}",
        "description": "\n".join(lines).strip()[:4000],
        "color": 0x5865F2,
        "footer": {"text": f"{len(todays)} tin • Neverness to Everness • auto-digest"},
    }
    _send({"embeds": [embed]})
    log(f"→ digest posted: {len(todays)} items")
    return True


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    if not DRY_RUN and not WEBHOOK:
        log("ERROR: DISCORD_WEBHOOK is not set (and DRY_RUN is off).")
        sys.exit(1)

    sources = yaml.safe_load(FEEDS_PATH.read_text("utf-8")) or []
    state = load_state()
    posted_total = 0
    changed = False

    # Daily-digest mode (its own scheduled run): summarise today's posts and stop.
    if DIGEST:
        if DRY_RUN:
            todays = [e for e in state.get(DIGEST_LOG_KEY, [])
                      if e.get("d") == dt.datetime.now(VN_TZ).strftime("%Y-%m-%d")]
            log(f"[dry] digest would summarise {len(todays)} item(s) today")
            return
        if run_digest(state):
            save_state(state)
        return

    # Global content-keys already posted (across ALL sources) — the same story
    # aggregated by Google News, neverness.gg and PW official now posts once.
    dedup_list = state.get(DEDUP_KEY, [])
    dedup_set = set(dedup_list)
    digest_log = state.get(DIGEST_LOG_KEY, [])

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
        digest_log.append(digest_record(it, src))
        time.sleep(1.0)  # be gentle with the webhook
        log(f"→ posted{tag} [{src['name']}] {it['title']}")
        return True

    for src in sources:
        name = src["name"]
        if src.get("enabled") is False:
            log(f"— skip (disabled): {name}")
            continue

        # The live "active codes" card is maintained separately (edit-in-place),
        # not posted as a normal feed item.
        if src.get("type") == "codes_tracker":
            try:
                if DRY_RUN:
                    codes = fetch_active_codes(src["url"])
                    log(f"\n=== {name}: {len(codes)} active codes ===\n"
                        f"   {', '.join(codes) or '—'}")
                elif update_codes_tracker(src, state):
                    changed = True
            except Exception as ex:
                log(f"! {name}: codes tracker failed: {ex}")
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
                if src.get("translate", True) and not src.get("video"):
                    vi = translate_vi(it["title"])
                    if vi:
                        log(f"       🇻🇳 {vi}")
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
        state[DIGEST_LOG_KEY] = digest_log[-KEEP_DIGEST:]
        save_state(state)
    log(f"\nDone. Posted {posted_total} new item(s).")


if __name__ == "__main__":
    main()
