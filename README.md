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
  type: rss                 # or: pw_scrape (official Perfect World page)
  url:  "https://.../feed/"
  color: 0x00C2FF
  enabled: true             # set false to pause it
  optional: true            # ignore fetch errors quietly
```
Most sites: try `https://SITE/feed/` or `/rss`. YouTube channel RSS is
`https://www.youtube.com/feeds/videos.xml?channel_id=UC...`.

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
