# NTE News → Discord

A tiny, **free & unlimited** replacement for the MonitoRSS 3-feed limit. It polls
every source in [`feeds.yml`](feeds.yml) and posts new items to a Discord channel
through a **webhook**. You own the webhook and the runner, so there's no feed cap.

Runs on **GitHub Actions** every 30 minutes (24/7, even when your PC is off).

## How it works
- `post_feeds.py` — fetches each source, remembers what it already posted
  (`state/seen.json`), and posts only new items as rich embeds.
- `feeds.yml` — the source list. Add as many as you want.
- `.github/workflows/news.yml` — the 30-minute schedule + manual "Run workflow".

On the **first** run each source is *seeded* silently (no post) so the channel
isn't flooded with old articles. From then on, only genuinely new items appear.

## Features
- **Sources**: YouTube (playable inline), official Perfect World page, official
  **Steam** announcements, Reddit, **r/NTELeaks** (leaks), neverness.gg,
  Google News (pro coverage).
- **Cross-source de-dup** — the same story reported by several feeds is posted
  **once** (matched by URL + normalized title), by whichever source is listed
  first in `feeds.yml`. Keep the most authoritative sources near the top.
- **Real publisher byline** — Google News / Steam items show the actual outlet
  (IGN, AUTOMATON, …) instead of an opaque aggregator name.
- **Redeem-code detection** — codes found in a post get a prominent `🎁 Code`
  field (`` `NTE2026` ``), and can trigger an @-role ping (see below).
- **Live "active codes" card** (`type: codes_tracker`) — scrapes the current
  working codes and keeps **one message** updated in place; pings when a
  brand-new code appears. Pin the message it creates.
- **Daily digest** — at **20:00 Vietnam time** it posts one round-up of
  everything published that day (grouped by source). Quiet on empty days.
- **Smart de-noise** — `include` (stay on-topic) / `exclude` (drop guide &
  tier-list SEO spam) / `keep` (real news like codes/patches/banners overrides
  `exclude`). Google News goes from ~95 noisy hits to real news only.
- **Vietnamese auto-translation** — English headlines/summaries are translated
  to Vietnamese (free Google Translate, no key). The VN title leads, the English
  original is kept underneath. Turn off with env `TRANSLATE=0`, or per source
  with `translate: false`. Already-Vietnamese text is left as-is.
- **Vietnam time** — post times shown as `dd/mm/YYYY HH:MM` (Asia/Ho_Chi_Minh).

### Optional: @-ping on important news
Set a repository **variable** (not secret) `PING_ROLE_ID` to the Discord role id
you want mentioned when a post is important (new codes, maintenance, banners,
launch). Leave it unset for **no pings** (default).
GitHub: **Settings → Secrets and variables → Actions → Variables → New variable**
→ name `PING_ROLE_ID`, value = the role id (right-click a role → *Copy Role ID*,
Developer Mode on). Silence a source with `ping: false` in `feeds.yml`.

## Setup (one-time)
1. In Discord: channel **#neverness-to-everness → Edit → Integrations →
   Webhooks → New Webhook → Copy Webhook URL**.
2. In this repo on GitHub: **Settings → Secrets and variables → Actions →
   New repository secret**, name **`DISCORD_WEBHOOK`**, paste the URL.
3. **Actions** tab → *NTE News → Discord* → **Run workflow** (first run just seeds).
   After that it runs automatically every 30 min.

## Adding / changing sources
Edit `feeds.yml`:
```yaml
- name: "My source"
  type: rss                 # rss | pw_scrape (PW page) | steam_news (needs appid)
  url:  "https://.../feed/"
  color: 0x00C2FF
  enabled: true             # set false to pause it
  optional: true            # ignore fetch errors quietly
  ping: false               # (optional) never @-ping for this source
```
Most sites: try `https://SITE/feed/` or `/rss`. YouTube channel RSS is
`https://www.youtube.com/feeds/videos.xml?channel_id=UC...`.

### Reliable Reddit & leaks (optional)
Anonymous Reddit gets rate-limited (429/403) from cloud IPs, so `r/NTELeaks`
loads only intermittently. For a rock-solid feed, add a free Reddit app:
1. https://www.reddit.com/prefs/apps → **create app** → type **script** →
   redirect URI `http://localhost` → create.
2. Copy the **client id** (under the app name) and **secret**.
3. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`.

With those set the bot fetches Reddit through authenticated OAuth (no throttling);
without them it just falls back to anonymous RSS.

### About X/Twitter
X removed free RSS. The `X — @NTE_GL` entry is disabled by default. To enable it,
point its `url` at an RSSHub or Nitter instance you trust and set `enabled: true`.

## Test locally (no Discord needed)
```bash
pip install -r requirements.txt
DRY_RUN=1 python post_feeds.py      # prints what it WOULD post
```

## Notes
- Change frequency via the `cron:` line in the workflow.
- Public repo = unlimited Actions minutes. (Private also works: ~30-min cron
  fits inside the free 2000 min/month.)
- GitHub disables scheduled workflows after 60 days with **no commits** — the
  state commits on new posts normally keep it alive.
