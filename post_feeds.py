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

try:
    import trafilatura   # best-in-class main-article text extraction
except Exception:
    trafilatura = None

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
TEST_URL = os.environ.get("TEST_URL", "").strip()  # post ONE article as a live test
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


# Classify each post by topic → (emoji, Vietnamese label, side-bar color).
# Order matters: most specific first. Drives the title emoji, a badge line at
# the top of the body, and the embed's color so each news TYPE looks distinct.
CATEGORIES = [
    (r"code|redeem|giftcode|coupon|voucher", "🎁", "CODE", 0xFFD700),
    (r"maintenance|hotfix|patch ?notes?|server (down|maintenance)|downtime|đang bảo trì",
     "🛠️", "BẢO TRÌ", 0xE67E22),
    (r"banner|gacha|pull|wish|rate ?up|character trailer", "🎰", "BANNER", 0x9B59B6),
    (r"leak|datamine|beta|prefarm|drip ?marketing|upcoming|silhouette",
     "🔍", "LEAK", 0x1ABC9C),
    (r"anniversary|festival|celebrat|collab(oration)?", "🎉", "SỰ KIỆN", 0xE91E63),
    (r"launch|release|out now|available now|coming to (steam|epic|ps5|xbox|switch)|now (live|available)|steam",
     "🚀", "RA MẮT", 0x2ECC71),
    (r"compensat|refund|reward|gift", "🎁", "QUÀ TẶNG", 0xF1C40F),
    (r"notice|account|\bban\b|penal|action against|violation", "📢", "THÔNG BÁO", 0xE74C3C),
    (r"update|version|v?\d+\.\d+|new (content|chapter|arc|story)", "🆕", "CẬP NHẬT", 0x3498DB),
    (r"revenue|sales|earnings|financial|profit|download|milestone|top ?grossing|chart",
     "📊", "DOANH THU", 0xF39C12),
    (r"nerf|buff|balance|tier|meta|ranking|rework", "⚖️", "CÂN BẰNG", 0x95A5A6),
    (r"event", "🎉", "SỰ KIỆN", 0xE91E63),
    (r"character|showcase|trailer|preview|teaser|reveal|new (unit|weapon|esper)",
     "✨", "NHÂN VẬT", 0x00BCD4),
    (r"interview|impression|review|opinion|hands-on|análise", "🗣️", "ĐÁNH GIÁ", 0x8E44AD),
]


def classify(title):
    """(emoji, Vietnamese category label, color) for a headline; a neutral
    default when nothing matches."""
    t = (title or "").lower()
    for pat, emo, label, color in CATEGORIES:
        if re.search(pat, t):
            return emo, label, color
    return "📰", "TIN TỨC", 0x5865F2


def topic_emoji(title):
    return classify(title)[0]


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


def _chunk_text(text, size):
    """Split text into pieces of at most `size` chars, breaking on paragraph
    then sentence boundaries (only hard-splitting a single giant sentence), and
    greedily packing so we use as few pieces as possible."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    units = []
    for para in text.split("\n\n"):
        if len(para) <= size:
            units.append(para)
            continue
        for sent in re.split(r"(?<=[.!?…])\s+", para):
            if len(sent) <= size:
                units.append(sent)
            else:
                for k in range(0, len(sent), size):
                    units.append(sent[k:k + size])
    chunks, cur = [], ""
    for u in units:
        piece = ("\n\n" + u) if cur else u
        if len(cur) + len(piece) <= size:
            cur += piece
        else:
            if cur:
                chunks.append(cur)
            cur = u
    if cur:
        chunks.append(cur)
    return chunks


def _alnum(s):
    """Lowercase, alnum-only, single-spaced — for loose text comparison."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def is_title_echo(summary, title):
    """True when a 'summary' carries no real information beyond the headline.
    Google News (and some feeds) set the summary to just the linked title +
    publisher, so rendering it as a description merely repeats the title. We
    detect that so those posts don't show a redundant/empty body."""
    d, t = _alnum(summary), _alnum(title)
    if not d:
        return True
    # d is often "<title> <publisher>" (or vice-versa) → one contains the other.
    return d == t or d.startswith(t) or t.startswith(d)


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
        # Translate in <=3500-char pieces (POST, so URL length is never the cap)
        # and stitch them back together — lets us translate whole long articles.
        pieces = _chunk_text(text, 3500) or [text]
        parts, src0 = [], None
        for i, piece in enumerate(pieces):
            r = requests.post(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "vi", "dt": "t"},
                data={"q": piece}, headers=HEADERS, timeout=25)
            if not r.ok:
                parts = None
                break
            d = r.json()
            parts.append("".join(seg[0] for seg in d[0] if seg and seg[0]))
            if i == 0:
                src0 = d[2] if len(d) > 2 else ""
        if parts:
            vi = "".join(parts).strip()
            if vi and src0 != "vi" and vi.lower() != text.lower():
                result = vi
    except Exception:
        result = None
    _tr_cache[text] = result
    return result


def _spoiler(text):
    """Wrap text as a click-to-reveal Discord spoiler (escape stray pipes so
    they don't break the ||…|| markup). Empty string if there's nothing."""
    text = (text or "").replace("|", "∣").strip()
    return f"||{text}||" if text else ""


def vi_reveal(title, desc, source):
    """The on-demand '🇻🇳 Tiếng Việt (bấm để xem)' line: the Vietnamese
    translation hidden behind a spoiler so English stays the default and readers
    tap to reveal it. '' when translation is off or unavailable. (A real Discord
    button needs an application-owned webhook — a plain channel webhook can't
    send message components — so a spoiler is the reliable in-message toggle.)"""
    if not source.get("translate", True):
        return ""
    vi_t = translate_vi(title)
    vi_d = translate_vi(desc) if desc else None
    body = " — ".join(x for x in (vi_t, vi_d) if x).strip()
    return f"🇻🇳 *Tiếng Việt (bấm để xem):* {_spoiler(body)}" if body else ""


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


_CONTENT_CLASSES = ("articleContent", "article-content", "entry-content",
                    "post-content", "richtext")


BODY_LIMIT = 6000        # max chars of English article to keep (split across embeds)
EMBED_DESC_MAX = 4000    # per-embed description budget (Discord hard cap 4096)
EMBED_TOTAL_MAX = 5800   # Discord caps ALL embed text in one message at 6000


def _trafilatura_extract(raw, limit=BODY_LIMIT):
    """Clean MAIN article text via trafilatura (handles arbitrary news sites far
    better than regex). `raw` is the page bytes (so encoding is auto-detected).
    Strips breadcrumb/date lines, keeps paragraph breaks, trims to `limit` on a
    word boundary. Returns None if unavailable, empty, or clearly junk (paywall
    stubs are short)."""
    if not trafilatura:
        return None
    try:
        txt = trafilatura.extract(raw, include_comments=False,
                                  include_tables=False, favor_precision=True)
    except Exception:
        txt = None
    if not txt:
        return None
    lines, started = [], False
    for ln in txt.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if ">" in ln and len(ln) < 70:               # breadcrumb "A>B>News"
            continue
        if re.fullmatch(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}", ln):  # bare date
            continue
        if not started:
            # skip leading section labels / repeated headline (short lines that
            # aren't full sentences) until the real prose starts
            if len(ln) < 80 and not re.search(r'[.!?…]["\')”]?$', ln):
                continue
            started = True
        lines.append(ln)
    body = "\n\n".join(lines).strip()
    if len(body) < 120:                              # paywall/login stub → junk
        return None
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.") + " …"
    return body


_BOILER_RE = re.compile(
    r"(?i)subscribe|newsletter|sign ?up|cookie|advertis|follow us|read more|"
    r"related:|share this|all rights reserved|©|table of contents")


def _extract_body(text, limit=600):
    """Pull the MAIN article prose out of page HTML: locate a known content
    container, then join its first real <p> paragraphs (skipping short/boilerplate
    lines) so we get the actual point of the article, not nav/captions/promos.
    Falls back to a plain tag-strip. Returns cleaned text or None."""
    region = text
    for cls in _CONTENT_CLASSES:
        m = re.search(rf'(?is)class="[^"]*{cls}[^"]*"[^>]*>(.*)', text)
        if m:
            region = m.group(1)
            break

    paras = []
    for pm in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", region):
        p = re.sub(r"<[^>]+>", " ", pm.group(1))
        p = re.sub(r"\s+", " ", html.unescape(p)).strip()
        if len(p) < 40 or _BOILER_RE.search(p):
            continue
        paras.append(p)
        if sum(len(x) for x in paras) >= limit:
            break
    if paras:
        return " ".join(paras)[:limit]

    chunk = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", region)
    chunk = re.sub(r"<[^>]+>", " ", chunk)
    chunk = re.sub(r"\s+", " ", html.unescape(chunk)).strip()
    return chunk[:limit] if len(chunk) > 40 else None


# og:descriptions that are a site-wide tagline, not about the specific article
# (Perfect World reuses one blurb on every page) → treat as "no real summary".
_GENERIC_DESC_RE = re.compile(
    r"(?i)developed by Hotta Studio|supernatural urban open-world")


def _looks_generic(desc):
    return not desc or bool(_GENERIC_DESC_RE.search(desc))


_page_cache = {}


def _get_page(url):
    """Fetch a page's raw bytes once and cache them, so text + images come from
    a single request. None on failure."""
    if url in _page_cache:
        return _page_cache[url]
    raw = None
    try:
        raw = requests.get(url, headers=HEADERS, timeout=25,
                           allow_redirects=True).content
    except Exception:
        raw = None
    _page_cache[url] = raw
    return raw


def _meta(text, *pats):
    for p in pats:
        m = re.search(p, text, re.I | re.S)
        if m and m.group(m.lastindex).strip():
            return html.unescape(m.group(m.lastindex).strip())
    return None


# image URLs that are chrome, not article content
_IMG_JUNK_RE = re.compile(
    r"(?i)avatar|gravatar|/icon|-icon|logo|sprite|emoji|1x1|pixel|blank|spinner|"
    r"loading|placeholder|/ads?[/_]|doubleclick|/badge|favicon|/thumb|-\d{2,3}x\d{2,3}\.")


def _extract_images(text, base_url, lead=None, limit=4):
    """Up to `limit` real content-image URLs: the og:image lead first, then
    <img> sources inside the article container, skipping icons/avatars/ads."""
    out, seen = [], set()

    def add(u):
        if not u or len(out) >= limit:
            return
        u = html.unescape(u.strip())
        if u.startswith("//"):
            u = "https:" + u
        u = urljoin(base_url or "", u)
        base = u.lower().split("?")[0]
        if not u.startswith("http") or base.endswith(".svg") or _IMG_JUNK_RE.search(u):
            return
        if u in seen:
            return
        seen.add(u)
        out.append(u)

    add(lead)
    region = text
    for cls in _CONTENT_CLASSES:
        m = re.search(rf'(?is)class="[^"]*{cls}[^"]*"[^>]*>(.*)', text)
        if m:
            region = m.group(1)
            break
    for m in re.finditer(r'<img[^>]+(?:data-src|data-lazy-src|src)=["\']([^"\']+)', region, re.I):
        add(m.group(1))
        if len(out) >= limit:
            break
    return out


_article_cache = {}


def fetch_article(url):
    """(main_text, [image_urls]) for an article page from ONE cached fetch.
    Text prefers real extracted body (trafilatura), then og:description, then a
    regex body for sites whose og is a generic site-wide blurb."""
    if url in _article_cache:
        return _article_cache[url]
    result = (None, [])
    raw = _get_page(url)
    if raw:
        text = raw.decode("utf-8", "replace")
        og_desc = _meta(text,
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=(["\'])(.*?)\1',
            r'<meta[^>]+name=["\']description["\'][^>]+content=(["\'])(.*?)\1',
            r'<meta[^>]+content=(["\'])(.*?)\1[^>]+property=["\']og:description["\']')
        og_image = _meta(text,
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=(["\'])(.*?)\1',
            r'<meta[^>]+content=(["\'])(.*?)\1[^>]+property=["\']og:image["\']')
        article = _trafilatura_extract(raw)
        if article:
            desc = article
        elif not _looks_generic(og_desc):
            desc = og_desc
        else:
            desc = _extract_body(text) or og_desc
        result = (desc, _extract_images(text, url, og_image))
    _article_cache[url] = result
    return result


_gn_cache = {}


def resolve_google_news(url):
    """Google News RSS links are opaque redirects (news.google.com/rss/articles/
    CBMi…) that don't resolve over plain HTTP. Decode them to the REAL publisher
    URL via Google's batchexecute endpoint so we can link to — and pull real body
    text from — the actual article. Returns the resolved URL, the original url if
    it isn't a Google-News link, or None on failure (caller falls back)."""
    if not url or "news.google.com" not in url:
        return url
    if url in _gn_cache:
        return _gn_cache[url]
    real = None
    try:
        tok = re.search(r"/(?:articles|read)/([A-Za-z0-9_\-]+)", url).group(1)
        r = requests.get(f"https://news.google.com/rss/articles/{tok}",
                         headers=HEADERS, timeout=20)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if sg and ts:
            req = ["garturlreq",
                   [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                     None, None, None, None, None, 0, 1],
                    "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                   tok, int(ts.group(1)), sg.group(1)]
            payload = [[["Fbv4je", json.dumps(req)]]]
            r2 = requests.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                headers={**HEADERS,
                         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                data={"f.req": json.dumps(payload)}, timeout=20)
            for u in re.findall(r'https?://[^\\"\s]+', r2.text):
                if "google.com" not in u and "gstatic" not in u:
                    real = u
                    break
    except Exception:
        real = None
    _gn_cache[url] = real
    return real


def fetch_article_body(url):
    """The main article text for feeds with no real summary (Perfect World
    notices, neverness.gg excerpts). Shares fetch_article's cached fetch."""
    return fetch_article(url)[0]


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
    # Shared cloud IPs get throttled: Reddit → 429, Google News → occasional
    # 503. A few short retries usually squeeze through; if not, the source is
    # optional or just skipped for this run.
    for _ in range(3):
        if resp.status_code not in (429, 500, 502, 503, 504):
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
           f"?appid={appid}&count=15&maxlength=3000")
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
      block_publishers — drop items from these outlets (case-insensitive substr)
    Used to de-noise busy feeds (Google News guides/tier-lists, Reddit memes)."""
    inc, keep, exc = src.get("include"), src.get("keep"), src.get("exclude")
    block = [b.lower() for b in (src.get("block_publishers") or [])]
    if not (inc or keep or exc or block):
        return items
    kept = []
    for it in items:
        title = it.get("title") or ""
        pub = (it.get("publisher") or "").lower()
        if block and any(b in pub for b in block):
            continue
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
        header = f"{emoji} **{item['title']}**".strip()   # English by default
        rv = vi_reveal(item["title"], "", source)          # tap-to-reveal VN
        vi_line = f"\n{rv}" if rv else ""
        lead = f"{ping}\n" if ping else ""
        # link stays on its own last line so Discord renders the player
        payload = {"content": f"{lead}{header}{vi_line}\n{link}"[:2000]}
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

    # English by default; the Vietnamese translation is offered on demand as a
    # tap-to-reveal spoiler below (see vi_reveal).
    en_title = item["title"]

    # For aggregators (Google News) decode the opaque redirect to the REAL
    # article URL, so the card links to the actual article AND we can fetch its
    # body text below. Falls back to the original link if decoding fails.
    link = item.get("link") or ""
    if source.get("resolve_content") and "news.google.com" in link:
        real = resolve_google_news(link)
        if real and "news.google.com" not in real:
            link = real

    # Classify by topic → title emoji, a category badge, and a per-type color so
    # each kind of news (code / maintenance / banner / launch…) looks distinct.
    cat_emoji, cat_label, cat_color = classify(en_title)
    color = cat_color if cat_label != "TIN TỨC" else int(source.get("color", 0x5865F2))
    now_vn = dt.datetime.now(VN_TZ).strftime("%H:%M")
    embed = {
        "title": f'{cat_emoji} {en_title}'[:256],
        "url": link or None,
        "color": color,
        "author": {"name": author_name[:256]},
        "footer": {"text": f"{cat_emoji} {cat_label} • Neverness to Everness • {now_vn}"},
    }
    if source.get("icon"):
        embed["author"]["icon_url"] = source["icon"]

    # REAL body only: a feed summary that just echoes the headline (Google News
    # and other aggregators do this) adds nothing, so we drop it. neverness.gg /
    # Steam carry genuine excerpts and pass. For `resolve_content` sources with
    # no real excerpt, pull the article's og:description so the post has actual
    # content instead of only a title.
    desc = clean_summary(item.get("summary"), BODY_LIMIT)
    if is_title_echo(desc, en_title):
        desc = ""
    if source.get("resolve_content") and link and "news.google.com" not in link:
        og_desc, _ = fetch_article(link)
        if not desc:
            og = clean_summary(og_desc or "", BODY_LIMIT)
            if og and not is_title_echo(og, en_title) and not _looks_generic(og):
                desc = og
    # `fetch_body` sources → pull the article's full text off the page and use it
    # when it's richer than the feed excerpt (Perfect World has none; neverness.gg
    # ships only a short excerpt). Gives the post the complete article, not a stub.
    if source.get("fetch_body") and link:
        body = clean_summary(fetch_article_body(link) or "", BODY_LIMIT)
        if body and not is_title_echo(body, en_title) and len(body) > len(desc):
            desc = body

    fields = []
    # Redeem codes, if any, get a prominent full-width field at the top.
    codes = extract_codes(item["title"], item.get("summary"))
    if codes:
        fields.append({"name": "🎁 Code",
                       "value": " ".join(f"`{c}`" for c in codes), "inline": False})
    # A row of inline fields — informative AND it forces the embed to widen.
    when = fmt_vn(item.get("ts"))
    if when:
        fields.append({"name": "📅 Đăng lúc", "value": when, "inline": True})
    fields.append({"name": "📡 Nguồn", "value": outlet or short_src, "inline": True})
    if link:
        fields.append({"name": "🔗 Chi tiết",
                       "value": f"[Mở bài viết ›]({link})", "inline": True})

    # Up to 4 real content images (og:image + article images) → a gallery.
    images = []
    if link and (source.get("resolve_content") or source.get("fetch_body")
                 or source.get("og_image")):
        images = fetch_article(link)[1]
    if not images:
        single = item.get("image") or source.get("default_image")
        images = [single] if single else []
    images = images[:4] or ([source["default_image"]] if source.get("default_image") else [])

    # Full English article, category badge on top, split into embeds that respect
    # Discord's per-description cap (badge rides on the first chunk).
    badge = f"{cat_emoji} `{cat_label}`"
    body_chunks = _chunk_text(desc, EMBED_DESC_MAX - len(badge) - 4) if desc else []
    if body_chunks:
        en_chunks = [f"{badge}\n\n{body_chunks[0]}"] + body_chunks[1:]
    else:
        en_chunks = [badge]
    embed["description"] = en_chunks[0]
    embed["fields"] = fields
    embeds = [embed]
    for ch in en_chunks[1:]:               # continuation embeds (no url → no merge)
        embeds.append({"color": color, "description": ch})

    # Image(s): a gallery only works when English is a single embed (Discord
    # merges same-url embeds), otherwise attach one lead image.
    if images:
        embed["image"] = {"url": images[0]}
        if len(en_chunks) == 1 and link:
            embed["url"] = link
            for img in images[1:4]:
                embeds.append({"url": link, "image": {"url": img}})

    # The FULL Vietnamese translation, hidden behind spoilers, split as needed.
    if source.get("translate", True):
        vi_full = "\n\n".join(x for x in (translate_vi(en_title),
                                          translate_vi(desc) if desc else None) if x)
        for j, ch in enumerate(_chunk_text(vi_full, EMBED_DESC_MAX - 60)):
            label = "🇻🇳 **Tiếng Việt** — bấm để xem:\n" if j == 0 else ""
            embeds.append({"color": color, "description": label + _spoiler(ch)})

    _send_messages(embeds, ping, mentions)


def _embed_len(e):
    n = len(e.get("title", "")) + len(e.get("description", ""))
    n += len(e.get("author", {}).get("name", ""))
    n += len(e.get("footer", {}).get("text", ""))
    for f in e.get("fields", []):
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


def _send_messages(embeds, ping=None, mentions=None):
    """Send the embeds, packing them into as few messages as possible while
    respecting Discord's limits (≤10 embeds and ≤6000 chars per message). The
    @-ping rides only on the first message."""
    groups, cur, cur_len = [], [], 0
    for e in embeds:
        el = _embed_len(e)
        if cur and (cur_len + el > EMBED_TOTAL_MAX or len(cur) >= 10):
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(e)
        cur_len += el
    if cur:
        groups.append(cur)
    for i, group in enumerate(groups):
        payload = {"embeds": group}
        if i == 0 and ping:
            payload["content"] = ping
            payload["allowed_mentions"] = mentions
        _send(payload)
        if i < len(groups) - 1:
            time.sleep(0.7)


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


def codes_embed(codes, src, new_codes=None):
    now = dt.datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M")
    new_set = set(new_codes or [])
    # brand-new codes float to the top with a ✨ badge so they stand out
    ordered = [c for c in codes if c in new_set] + [c for c in codes if c not in new_set]
    lines = [f"{'✨' if c in new_set else '🎁'}  **`{c}`**"
             + ("  ← mới" if c in new_set else "") for c in ordered]
    body = "\n".join(lines) or "_Hiện chưa có code nào._"
    intro = ""
    if new_set:
        intro = "🆕 **Code mới:** " + ", ".join(f"`{c}`" for c in new_codes) + "\n\n"
    return {
        "title": "🎁 Code NTE đang hoạt động",
        "url": src.get("url"),
        "color": int(src.get("color", 0xFFD700)),
        "description": f"{intro}{body}\n\n[Cách nhập code ›]({src.get('url')})",
        # 'kiểm tra' = last CHECK time, refreshed every run → visibly live even
        # when the code list itself hasn't changed.
        "footer": {"text": f"Nguồn: neverness.gg • {len(codes)} code đang hoạt động "
                           f"• tự động kiểm tra {now} (giờ VN)"},
    }


def update_codes_tracker(src, state):
    """Maintain ONE live 'active codes' message. The card is re-rendered on EVERY
    run so its 'last checked' time stays current (proves it's alive), the message
    is recreated if it was deleted, and a @-ping fires only for brand-new codes."""
    codes = fetch_active_codes(src["url"])
    prev = state.get(CODES_LIST_KEY, [])
    msg_id = state.get(CODES_MSG_KEY)
    new = [c for c in codes if c not in set(prev)] if prev else []
    changed = False

    embed = codes_embed(codes, src, new_codes=new)

    if not msg_id:
        state[CODES_MSG_KEY] = _webhook_post_get_id({"embeds": [embed]})
        state[CODES_LIST_KEY] = codes
        log(f"✓ codes card created (id={state[CODES_MSG_KEY]}) — PIN this message once")
        return True

    # Refresh the card in place every run. If it 404s (someone deleted it), make
    # a fresh one — this now happens even when the code list is unchanged, so a
    # missing card always self-heals.
    if not _webhook_edit(msg_id, {"embeds": [embed]}):
        state[CODES_MSG_KEY] = _webhook_post_get_id({"embeds": [embed]})
        log(f"✓ codes card recreated (id={state[CODES_MSG_KEY]}) — PIN this message once")
        changed = True

    if new:  # real-time alert only for genuinely new codes
        lead = f"<@&{PING_ROLE_ID}> " if PING_ROLE_ID else ""
        _send({"content": f"{lead}🎁 **Code NTE mới:** "
                          + ", ".join(f"`{c}`" for c in new),
               "allowed_mentions": {"parse": [],
                                    "roles": [PING_ROLE_ID] if PING_ROLE_ID else []}})
        log(f"→ new-code alert: {', '.join(new)}")

    if set(codes) != set(prev):
        state[CODES_LIST_KEY] = codes
        changed = True
        log(f"✓ codes updated: {len(codes)} active")
    else:
        log(f"· codes unchanged ({len(codes)} active) — card refreshed")
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
            title = (e.get("t") or "").strip()   # English by default
            url = e.get("u")
            lines.append(f"{e.get('e', '•')} [{title}]({url})" if url
                         else f"{e.get('e', '•')} {title}")

    # One-stop: remind readers of the codes that are live right now.
    codes = state.get(CODES_LIST_KEY, [])
    if codes:
        lines.append("\n**🎁 Code đang hoạt động**")
        lines.append(" ".join(f"`{c}`" for c in codes[:20]))

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

    # Test mode: build ONE post from a given article URL and send it, bypassing
    # the dedup/seen state — a way to verify the live format on demand.
    if TEST_URL:
        is_google = "news.google.com" in TEST_URL
        page_url = (resolve_google_news(TEST_URL) or TEST_URL) if is_google else TEST_URL
        title = None
        raw = _get_page(page_url)
        if raw:
            t = raw.decode("utf-8", "replace")
            title = _meta(t, r'<meta[^>]+property=["\']og:title["\'][^>]+content=(["\'])(.*?)\1',
                          r"<title[^>]*>(.*?)</title>")
        item = {"title": (title or "NTE test article").strip()[:250], "link": TEST_URL,
                "summary": "", "ts": dt.datetime.now(dt.timezone.utc),
                "publisher": urlsplit(page_url).netloc.replace("www.", "")}
        src = {"name": "🧪 Test — NTE", "emoji": "🧪", "color": 0x5865F2, "translate": True,
               "resolve_content": is_google, "fetch_body": not is_google,
               "default_image": "https://nte.perfectworld.com/public/images/share_en.jpg"}
        log(f"[TEST] posting: {item['title']}")
        post_discord(item, src)
        log("✓ TEST post sent to Discord")
        return

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
                # verify the Google-News decode + real-content fetch on the runner
                if src.get("resolve_content") and "news.google.com" in (it.get("link") or ""):
                    real = resolve_google_news(it["link"])
                    if real and "news.google.com" not in real:
                        og_desc, imgs = fetch_article(real)
                        log(f"       🔗 {real[:80]}")
                        log(f"       📄 {(clean_summary(og_desc or '') or '(no body)')[:120]}")
                        log(f"       🖼️ {len(imgs)} image(s)")
                    else:
                        log("       🔗 (decode failed → falls back to title + banner)")
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
