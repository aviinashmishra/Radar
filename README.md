# Pebble Competitor Radar

Live, end-to-end competitive intelligence for Pebble (pebblecart.com) in the Indian D2C electronics
sector: smartwatches, TWS earbuds, speakers, chargers. It watches what competitors **do**, not just what
they say, scores every change, suggests a counter-move, and pushes alerts and briefs.

```
SOURCES ──► COLLECTORS ──► SQLite ──► AI ENRICHMENT ──► SIGNAL ENGINE ──► OUTPUTS
 Shopify JSON   every 10 min     diffs      Claude or       price war        live dashboard (SSE)
 Google News    every 30 min     history    heuristic       stock-out opp.   Telegram / Discord / Slack
 Reddit, feeds  every 30 min                score 1-10      launch predictor Ask-the-Radar chat
 page watch     daily                       "so what"       momentum         daily + weekly brief
 Meta Ad Library, crt.sh  daily             counter-move    sale-start       JSON API
```

## Run it (one command)

```bash
pip install -r requirements-full.txt
python -m radar.server          # http://127.0.0.1:8080  - collectors, scheduler, dashboard, API, chat
```

Windows: double-click `start.bat`. Docker: `docker build -t radar . && docker run -p 8080:8080 -v radar:/data radar`.

The first sweep builds the baseline (about 2 minutes for 14 stores). From the second sweep on, every
price change, stock flip, new SKU, new collection, best-seller move and hidden product page becomes an event.

Optional keys in `.env` (copy `.env.example`):

| Key | Unlocks |
|---|---|
| `ANTHROPIC_API_KEY` | Claude scoring with real "so what" and counter-moves, AI-written briefs, natural-language chat. Without it a keyword heuristic scores events and chat does keyword search, so the radar still runs. |
| `GEMINI_API_KEY` | Same AI layer on Google Gemini (`GEMINI_MODEL`, default `gemini-3.8-flash`): scoring, briefs, chat with tools, screenshot vision. `RADAR_AI_PROVIDER` picks `auto` (Claude if its key is set, else Gemini), `claude`, `gemini` or `off`. |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Push alerts for events scored >= `ALERT_THRESHOLD` (default 6) and all signals |
| `WEBHOOK_URL` | Same alerts to a Discord or Slack incoming webhook |
| `META_ACCESS_TOKEN` | Meta Ad Library collector (new creatives, ad longevity) |
| `RADAR_PASSWORD` | The browser asks for this password (any user name) before showing anything. Use it whenever the dashboard is reachable by others. |

## Deploy to Vercel (cloud, no server to keep running)

Vercel runs Python as short-lived functions: no background scheduler, no disk that survives, no always-on
bot. So the radar runs in two halves that need no server of their own:

```
 GitHub Actions (worker, free on a public repo)            Vercel (dashboard)
 every 15 min / hourly / daily                            app.py -> radar.server in cloud mode
   pull last snapshot ──► run collectors ──► push ──►  release `data`: radar.db.gz  ◄── each instance downloads it,
                                                        (+ 3 days of screenshots)        re-checks once a minute
 Run button / Telegram /run  ◄──────────── GitHub API (workflow_dispatch) ◄────────────  (one tiny request)
```

* **Data travels as a release file, not a commit.** Each run replaces `radar.db.gz` (about 2.6 MB) on the
  release tagged `data`, so the repository never grows. Screenshots older than 3 days are pruned.
* **Checking for new data is nearly free.** Every upload gets a new download address, so a dashboard instance
  only downloads when the data actually changed.
* **The first run builds everything.** With no snapshot yet, the workflow runs all collectors (about 20 minutes),
  then the 15-minute / hourly / daily schedule takes over. Editing `config/competitors.yaml` on GitHub starts a sweep.

**Steps**

1. Push this repo to GitHub (done for `aviinashmishra/Radar`). The workflow starts on the push and publishes the
   first snapshot; watch it under **Actions**. Add your keys as repository **secrets** (Settings > Secrets and
   variables > Actions): `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, ...
2. On [vercel.com/new](https://vercel.com/new), import the repository. Vercel detects FastAPI from `app.py`;
   keep the defaults.
3. Under the project's **Settings > Environment Variables**, add at least `RADAR_PASSWORD`, then redeploy.

| Vercel variable | Needed? | What it does |
|---|---|---|
| `RADAR_PASSWORD` | strongly recommended | Password prompt (any user name). Without it the dashboard is public, and Ask-the-Radar AI answers, Run and AI briefs are switched off so strangers cannot spend your credits. |
| `GITHUB_TOKEN` | optional | [Fine-grained token](https://github.com/settings/personal-access-tokens/new) for this one repo with **Actions: Read and write**: the Run buttons and Telegram `/run` start a workflow run. If the repo is private, also give it **Contents: Read** (the dashboard then reads the snapshot through the API). |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | optional | AI answers in Ask the Radar and AI-written briefs on demand. Scoring happens on GitHub Actions, so the repo secret is what matters for that. |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | optional | Two-way bot without a server: Settings > **Connect the bot to this site** registers a webhook. It answers only your chat. (A webhook replaces polling, so the local server's bot stops answering until you call `POST /api/telegram/connect?off=1`.) |
| `CRON_SECRET` | optional | A daily Vercel cron calls `/api/cron/heartbeat`; with this set, a snapshot older than 3 hours sends a Telegram / webhook warning and, with `GITHUB_TOKEN`, restarts the worker. |
| `RADAR_REPO` | only for a fork | `owner/name` of the repo that runs the workflow (default: `cloud_repo` in `config/competitors.yaml`). |

What is different in cloud mode: the live dot shows how old the data is ("cloud · updated 6m ago") and new
events arrive by a one-minute poll instead of a stream; **Done / Later** is remembered per browser because the
shared database is a read-only snapshot; briefs written from the Briefs page are shown but not kept (the
scheduled ones come from the workflow). Keep the repo **public**, where Actions minutes are free: the schedule uses
about 20,000 minutes a month (fast tier ~4 min x 96 runs a day, medium ~13 min x 24), and a private repo gets 2,000.

Locally nothing changes: `python run.py` still runs collectors, scheduler, stream and bot in one process.

## The dashboard

Seven views, light / dark / system theme, keyboard-first:

- **Today**: action cards (importance, why, counter-move, Done / Later), open signals, a threat radar ring of
  rivals by activity, and what changed in the last 24 hours per brand.
- **Feed**: every detected change with filters; new events arrive as a "N new" pill, click to show.
- **Brands**: one card per brand with a 14-day activity sparkline; brand pages have timeline, catalogue with
  price history, best-sellers and insights (launch evidence, companion app, complaint themes, homepage shot).
- **Pricing**: Pebble vs the field, undercuts, white-space heatmap, price position per Pebble SKU.
- **Market**: search demand, creators, companion apps, pain points, tech stack, visual watch.
- **Briefs** and **Settings** (run tiers, integrations, collector log, CSV export).
- `Ctrl K` or `/` opens the command palette: jump to a brand or page, search the feed, or ask the radar.

## Segments and the watchlist

Pebble's form factors are first-class: **smart bands (screenless)**, **smart rings**, **smartwatches**, **TWS earbuds**
and **speakers** (keys in `segments:` of `config/competitors.yaml`). Every store SKU is classified into one, and the
**Segments** page shows a log-scale price ladder per segment with Pebble highlighted, the field's median, how many
rivals are global, how many need a subscription, and the segment's events for the last 14 days.

`config/watchlist.yaml` holds the curated products from the Competitors sheet (Whoop, Oura, Garmin Cirqa, Fitbit Air,
Ultrahuman, Samsung Galaxy Ring, Gabit, FITTR, Sekyo, Urban, Luna, Amazfit, Noise REP, boAt rings, Pebble Qore) with
sheet price, subscription, status, accuracy evidence and origin. Rows with a `url` are read live every 30 minutes
(JSON-LD offers, meta tags, or the rupee amount nearest the last price; Whoop is rendered with Playwright because it
blocks plain requests). Price moves and sold-out / pre-order flips become `watch_price_change` and
`watch_status_change` events. A 12-month cost column adds the subscription where one is mandatory.

## Sensors beyond the store

| Sensor | Source | What it tells Pebble |
|---|---|---|
| Pre-launch pages | product sitemap vs catalogue, then `/products/<handle>.json` | Unreleased SKUs with title, price, variants before the listing goes live |
| Review velocity + pain points | Judge.me widget (6 of the tracked stores use it) | Which rival SKUs are selling; recurring 1-2★ complaints to design and advertise against |
| App health | Google Play pages of companion apps | Rating slides, rating-count growth (active device sales proxy), app updates before launches |
| Influencer map | YouTube search, newest uploads | Which creators cover which rivals, with views, so Pebble can reach them first |
| Search demand | Google autocomplete | Model names and price points India is typing right now |
| Tech stack | Store HTML markers | New checkout, BNPL, CRM or review apps = conversion/retention bets |
| Visual watch | Playwright screenshots + pixel diff (+ Claude vision when configured) | Banner, offer and layout changes text diffing misses |
| Price position | Fuzzy matcher: category + specs + price | Every Pebble SKU vs its nearest rival SKUs; `undercut` signal when a rival drops >10% below |

Plus a two-way Telegram bot (`/ask`, `/top`, `/brand`, `/signals`, `/brief`, `/run`, `/status`), CSV export of every
table (`/api/export/<table>.csv`), and an email digest for the briefs when `SMTP_*` is set.

Screenshots need a browser once: `python -m playwright install chromium`. Set `screenshots: false` in the config to skip.

## What is unusual about it

* **Catalogue as a sensor.** Every Shopify store exposes `/products.json`, `/collections.json` and a product
  sitemap without auth. Diffing them every 10 minutes gives prices, MRP, stock, variants, new SKUs and new
  campaigns before any press release.
* **Hidden-product detection.** Products that exist in `sitemap_products.xml` but are absent from the public
  catalogue are usually pre-launch pages. The radar links straight to them.
* **Best-seller rank as a sales proxy.** The order of a store's best-seller collection is its sales order.
  The radar tracks rank moves, and when a top-10 item goes out of stock it raises a *stock-out opportunity*.
* **Launch predictor.** Hidden products + new SKUs + "launch" news + new collections + homepage and careers
  changes over 14 days, capped per evidence type, give a per-brand launch score.
* **Sale-start and festival awareness.** Many drops in one sweep = a sale; it is labelled with the nearest
  Indian sale window (Diwali, Big Billion Days, Republic Day, ...) from `config/competitors.yaml`.
* **Self-benchmark.** Pebble is tracked by the same pipeline, so the dashboard shows Pebble vs the field
  (median price gap per category, SKU count, stock, discount depth).
* **White-space map.** Category x price band grid of competitor SKUs where Pebble has none.
* **Winner detector.** Rank + restock churn + discounting rank which rival products are actually selling.
* **Ask the Radar.** A chat box where Claude queries the radar's own database with tools (events, products,
  price history, benchmark, white-space, winners, signals).
* **New-entrant detection.** Certificate Transparency logs (crt.sh) surface new sector domains before launch.

## Layout

```
config/competitors.yaml   brands, store URLs, news terms, watch pages, festival calendar, price bands
radar/config.py           settings (.env + yaml)
radar/db.py               SQLite schema and helpers (swap for Postgres here)
radar/collectors/         shopify, news, reddit, feeds, youtube, searchdemand, pagewatch, techstack, reviews,
                          appstore, screenshots, ads, newstores
radar/enrich.py           Claude structured-output classification + heuristic fallback
radar/gemini.py           Gemini backend: JSON scoring, text, vision, tool-use chat (google-genai SDK)
radar/signals.py          cross-event rules (price war, stock-out, launch, undercut, pain point, momentum)
radar/matcher.py          Pebble SKU <-> rival SKU matching, price position, undercuts
radar/analytics.py        white-space, benchmark, winners, influencers, app health, search demand, pain points
radar/bot.py              two-way Telegram bot
radar/exports.py          CSV export, email digest
radar/alerts.py           Telegram / webhook / console
radar/brief.py            daily and weekly brief
radar/chat.py             Ask the Radar (tool-use loop)
radar/runner.py           scheduler and CLI
radar/server.py           FastAPI: dashboard, SSE stream, JSON API, password gate
radar/cloud.py            cloud mode: snapshot pull / push, Vercel refresh, GitHub dispatch, webhook, heartbeat
radar/static/index.html   dashboard
app.py, vercel.json       Vercel entrypoint and function / cron config
.github/workflows/        the cloud worker: collectors on a schedule, snapshot on the `data` release
requirements.txt          what Vercel installs; requirements-full.txt adds the local server and screenshots
```

## CLI

```bash
python -m radar.runner --once --tier fast     # stores only (10-minute tier)
python -m radar.runner --once --tier all      # everything once
python -m radar.runner --loop                 # scheduler without the web server
python -m radar.runner --brief 7              # write + push the weekly brief
python -m radar.cloud pull | push             # cloud worker: restore / publish the snapshot (GitHub Actions)
```

## API

`/api/stats` `/api/events?brand=&kind=&min_importance=&q=&days=` `/api/stream` (SSE) `/api/signals`
`/api/brand/{name}` `/api/products?q=` `/api/price_history?brand=&product_id=` `/api/whitespace`
`/api/benchmark` `/api/winners` `/api/launch_scores` `/api/briefs` `/api/matches` `/api/influencers`
`/api/app_health` `/api/search_demand` `/api/review_pain_points` `/api/techstack` `/api/screenshots`
`/api/sale_cadence` `/api/export/{table}.csv` `/api/telegram/preview?cmd=/top`
`POST /api/chat {question}` `POST /api/run?tier=fast|medium|slow|all` `POST /api/brief?days=7`
`/api/updates?after_id=&after_sig=` (polling twin of the stream) `POST /api/telegram/webhook` `POST /api/telegram/connect`
`/api/cron/heartbeat`

## Adding a competitor

Add a block under `brands:` in `config/competitors.yaml`. A Shopify URL gives the full catalogue sensor;
brands without one are still covered by news, Reddit and watch pages. Restart the server (in cloud mode, commit
the change on GitHub: the push starts a sweep).

## Ground rules baked in

Public, unauthenticated data only. One request per second per host, a descriptive User-Agent, official
APIs where they exist (Meta Ad Library, Google News RSS, Reddit RSS, crt.sh). Nothing behind a login.
