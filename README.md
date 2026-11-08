# Nazar

**One eye on every market.** Nazar (नज़र, "sight") is live, end-to-end market and competitive intelligence across
sectors, India and world. Every sector is a complete radar (its own companies, sources, database, scoring and
schedule); Nazar runs them all in one process and reads them together.

```
 config/nazar.yaml  ──►  one radar per sector  ──►  SQLite per sector  ──►  Nazar hub (one origin)
   iot  ⌚  Pebble's own radar (self brand: Pebble)                         /            Eye · Pulse · Markets · Universe
   auto 🚗  travel ✈️  fintech ₹  qcom ⚡  ecom 🛍️                           /s/<sector>   Themes · India·World · Sectors
   edtech 🎓  health 🩺  ai ✦  energy ☀️                                     /app          each sector's full dashboard
```

```bash
pip install -r requirements-full.txt
python run.py                 # or: python -m nazar      -> http://127.0.0.1:8080
python run.py --radar         # the single-sector Pebble radar only, as before
```

Nazar today: **10 sectors, 228 companies (146 Indian, 82 global, 115 listed), 7 hub views** plus the fifteen-page radar
dashboard inside every sector.

| Hub view | What it shows |
|---|---|
| **Eye** | The iris: one wedge per sector, brighter the busier, dots for the hottest companies; the top move per sector; KPI strip; sector cards with sparklines. |
| **Pulse** | Every scored move across every sector, newest first, with the sector's "so what" and counter-move; filters by sector, importance, kind, window; refreshes itself. |
| **Markets** | A live tape (Nifty, Sensex, S&P 500, Nasdaq, USD/INR, Bitcoin, crude, gold) and every listed company: close, 1-day move, 52-week position, 30-day sparkline, gainers and losers. |
| **Universe** | Every company categorised: sector, India / world, HQ country, type, segments, ownership, ticker, founded, parent, investors, moves and news this week, app rating, Wikipedia attention, latest revenue or valuation with its confidence. Cards or table, searchable. |
| **Themes** | Topic × sector heat map of what the news is about (launch, funding, financials, legal, leadership, partnership, distribution, regulation ...), with the change against the previous window, the headlines and who is in each story. |
| **India · World** | Share of moves by origin per sector, attention leaders (Wikipedia views), headquarters map, listed vs private. |
| **Sectors** | The atlas: each sector's segments, regulators, trackers, the dates that move it, scheduler state, a Sweep button and its dashboard. |

`Ctrl K` anywhere: jump to any company or sector, search every sector's feed, or ask a question (answered by the
chosen sector's analyst with the radar's tools; keyword search without an AI key).

### How a sector is defined

`config/nazar.yaml` lists the sectors. Each one points at a folder with the same three files the Pebble radar uses:
`competitors.yaml` (companies with origin, country, type, segments, news terms, aliases, look-alikes to exclude, apps,
pages to watch; segments with keywords; feeds; Reddit; a `vocab` block that tunes news relevance and scoring for that
industry; `voice_themes` for reviews; `world` facts for the atlas), `companies.yaml` (legal entity, HQ, founded,
ownership, Yahoo ticker, SEC CIK, Wikipedia title, investors, seeded financials with confidence) and `market.yaml`
(segment sizes, growth, shares, all flagged estimates). The IoT sector is the original `config/*.yaml`.

No sector other than IoT has a brand marked `self: true`, so their analysis is written for a neutral reader competing
in the sector ("Check your price on the nearest SKU"). Mark one company `self: true` (and give `market.yaml` a `self:`
block with its revenue and segment mix) and that sector's radar turns into that company's radar: rupees at risk,
undercuts, pressure by rival, strategy playbook, the works.

Add a sector: copy a folder under `config/sectors/`, edit the three files, add a line to `config/nazar.yaml`, restart.

### Running it

- **Local**: `python run.py` starts every sector's scheduler inside one process (news, feeds, Reddit, YouTube, search
  demand, Trends, app stores, filings, share prices, Wikipedia attention, pages, reviews; stores and marketplaces where
  a sector has them). Sector starts are staggered; non-store sectors run the news tier every 45 minutes. Share prices
  and attention are fetched first for a new sector so the tape is alive within minutes; the first full pass of all
  sectors takes a couple of hours.
- If an older single-sector radar window is still holding port 8080, Nazar starts on 8090, uses that process's
  database for the IoT sector without starting a second scheduler on it, and says so in the console.
- **Cloud**: the GitHub Actions workflow runs one job per sector (matrix, three at a time) and publishes
  `<sector>.db.gz` next to the IoT `radar.db.gz` on the `data` release. The Vercel app still serves the IoT radar.
- Alerts from a sector without a home brand are prefixed with the sector name (`[Fintech] ...`); the Telegram bot
  answers for the IoT sector.

Environment: `NAZAR_PORT`, `NAZAR_SECTORS` (subset), `NAZAR_SCHEDULERS=off` (dashboards only), `NAZAR_MEDIUM_MIN`,
`NAZAR_STAGGER_SEC`, `NAZAR_DATA_DIR` (see `.env.example`). Everything below describes the radar that runs inside each sector.

---

## The radar inside every sector: Pebble Competitor Radar

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

Fifteen views, light / dark / system theme, keyboard-first:

- **Today**: a KPI strip (open actions, changes in 24 h, news in 7 d, ₹ at risk this month, money moves, days to the
  next sale), action cards (importance, why, **₹/month impact**, counter-move, Done / Later), open signals, the threat
  radar ring, what changed per brand, pressure on Pebble by rival, this week's most-covered stories, listed rivals that moved.
- **Feed**: every detected change with filters; new events arrive as a "N new" pill, click to show.
- **News**: stories clustered across publishers (one headline = every outlet that ran it), topic chips (launch, funding,
  financials, legal, leadership, partnership, distribution, manufacturing, marketing, market data, reviews), filters by
  brand / topic / window, a headline search, strongest-first or newest-first, share of voice with trend, topic mix,
  what is trending in India right now (falls back to what India is starting to search for in the category), news
  velocity per brand, and the publishers that cover the sector. Every story has a topic-specific "why it matters" and
  "what to do" line, the ₹/month impact estimate when the model can price it, and a details panel with the summary,
  first-seen / last-seen, every outlet, and links to the brand page, its financials and the web. "Refresh news" fetches
  Google News, Bing News and the newsroom feeds now.
- **Financials**: the company behind every brand with revenue, YoY growth, net result and margin, valuation or funding,
  scale versus Pebble and the parent's share price (1-day, 30-day, 52-week position). Conglomerates (Titan, Alphabet,
  Samsung; `scope: parent` in `companies.yaml`) are never compared on group revenue: seed a `segment_revenue` row and the
  wearables arm is compared instead. Cards for scale versus Pebble, growth and a funding / valuation timeline sit above
  the peer table. Click a row for the revenue and profit series, the share-price line, every figure on file with its
  source link and confidence, the money news, and links to SEC filings, Yahoo Finance and Wikipedia. "Refresh
  financials" reads EDGAR, share prices and money news now.
- **Brands**: one card per brand with a 14-day activity sparkline; brand pages have timeline, catalogue with
  price history, best-sellers, insights (launch evidence, companion app, complaint themes, homepage shot), **News**
  (stories, attention curve, pressure on Pebble, topics) and **Financials** tabs.
- **Segments**: a log-scale price ladder per form factor with Pebble highlighted (see below).
- **Pricing**: Pebble vs the field, undercuts, white-space heatmap, price position per Pebble SKU.
- **Market**: market size per segment (units, value, growth, ASP, shares, Pebble's rank), money at stake from rivals'
  moves, the share-of-signal table, then search demand, creators, companion apps, pain points, tech stack, visual watch.
- **Channels**: where every brand is sold (Amazon.in, Flipkart, Blinkit per city, Zepto, Instamart, Croma, Reliance
  Digital, Vijay Sales, offline), marketplace prices and ratings, price parity vs own store, discount depth.
- **Reviews**: voice of the customer end to end. A written market read (what Indian buyers praise and complain about
  across every brand, the segment that draws the most complaints, where Pebble sits against the field), demand themes
  split into praise and complaints and labelled table stakes / open wound / contested, "what India asks for" (Google
  autocomplete grouped into feature asks: blood pressure, calling, AMOLED, battery, rings, price point ...), mood by
  week, a per-segment table, a ratings scoreboard (Google Play, App Store, own store, Amazon.in, composite), the brand x
  theme heat matrix, a conclusion paragraph per brand with quotes, an item explorer (brand, source, theme, polarity,
  text), optional AI reads of a brand or of the whole field, and a "refresh reviews" button. Brand pages get a Reviews
  tab with the same read for one brand.
- **Regions**: India map of search interest per state, own sales per state and city, quick-commerce shelves per city;
  where rivals out-index Pebble.
- **Reports**: daily / weekly / monthly / yearly sales and performance reports, the sector & industry report, rivals'
  sales-proxy index, CSV import of Pebble's own sales.
- **Strategy**: segment scorecard, USP claims with evidence, problems to fix, threats, opportunities, SWOT, 30/60/90 plan.
- **Briefs** and **Settings** (run tiers, integrations, collector log, CSV export).
- `Ctrl K` or `/` opens the command palette: jump to a brand or page, search the feed, or ask the radar.

## Money, markets and impact

Three config files drive it, all plain YAML:

| File | Holds | Used by |
|---|---|---|
| `config/companies.yaml` | per brand: legal entity, HQ, ownership, Yahoo ticker, SEC CIK, Wikipedia article, App Store ids, investors, seeded financial figures with source and confidence | Financials page, financials / attention / app-store collectors |
| `config/market.yaml` | India market model: units, value, growth, ASP and shares per segment, Pebble's revenue and segment mix, the impact-model assumptions | Market page, the ₹/month impact on every event and signal, briefs |
| `config/competitors.yaml` | brands, stores, news terms, newsroom and analyst RSS feeds, sector searches | everything else |

**Where the money figures come from.** US-listed parents (Garmin, Zepp Health / Amazfit, Alphabet / Fitbit) are read
from SEC EDGAR's XBRL company facts: revenue, gross and operating profit, net result per fiscal year, straight from
the 10-K / 20-F (`SEC_CONTACT` in `.env` identifies the tool, as EDGAR asks). Listed parents (also Titan / Fastrack,
Samsung, Palred / pTron) get a daily closing price, and a move of 5 % or more, a 52-week high or low, becomes a
`stock_move` event. For private Indian companies the radar searches the news for revenue, profit, loss, funding and
valuation per brand and extracts the figures (₹ crore and $ million amounts next to the metric word; with a Claude or
Gemini key the model does the extraction). Every figure carries its source and a confidence (`filing` > `reported` >
`ai` > `extracted` > `estimate`), and seeded estimates in `companies.yaml` are shown as estimates until a better source
replaces them. A new revenue, funding or valuation figure for a core rival raises a `war_chest` signal.

**Impact model.** `market.yaml` states Pebble's revenue (an estimate until you set it) and segment mix. For each rival
move the radar prints ₹/month at risk or upside: a price cut is monthly segment revenue × overlap with that rival ×
elasticity × the cut; a launch shifts a stated share of the segment; a rival's best-seller going out of stock is
upside (a share of its stuck demand). The assumptions are listed on the Market page and every amount's basis is in its
tooltip. Sums are capped at what a segment earns. It is an estimate to argue with, not a forecast.

**Market size.** Segment units, value, growth, ASP and the brand shares are seeds from press coverage of the IDC and
Counterpoint trackers; replace them with licensed numbers and the Market page, briefs and chat follow. The page adds
what the radar measures live: median store price, rival count and Pebble SKUs per segment, and the share of signal
(news mentions, autocomplete suggestions, creator views, app rating counts, Wikipedia attention) per brand.

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
| News, two aggregators | Google News RSS + Bing News RSS (India editions), de-duplicated by headline | Every brand and sector story once, with a topic; `news_storm` signal when 4+ outlets run one story |
| Newsrooms + analyst feeds | Samsung, Google Fitbit, Garmin, Oura, Whoop, Ultrahuman, Polar RSS; Counterpoint, Entrackr, Inc42, Gizmochina, 91mobiles filtered to the sector | Official announcements days before coverage; shipment data and funding news filed under the brand they name |
| Trending now | Google Trends "trending in India" RSS, matched to brands and segments | The moment a rival or category trends nationally (`trending_now`) |
| Attention | Wikipedia daily page views per company | A public-attention index; `attention_spike` at 3x the 30-day median |
| Apple App Store | iTunes lookup API, India store | iOS rating, rating count, version and release date next to the Play figures (premium buyers skew iPhone) |
| Share prices | Yahoo Finance daily chart for listed parents | `stock_move` on a 5 %+ day, 52-week highs and lows, 30-day trend on the Financials page |
| Filings | SEC EDGAR XBRL company facts | Revenue, gross / operating / net result per fiscal year for Garmin, Zepp Health, Alphabet (`financial_result`) |
| Money news | Google + Bing searches for revenue / loss / funding / valuation per brand, regex or AI extraction | Revenue, profit, loss, funding and valuation figures for private Indian rivals (`financial_fact`, `war_chest`) |

Plus a two-way Telegram bot (`/ask`, `/top`, `/brand`, `/news [brand]`, `/money [brand]`, `/reviews [brand]`, `/signals`, `/brief`, `/run`, `/status`), a stale-build guard (the dashboard notices when the files on disk are newer than the running
server and offers a one-click restart, `POST /api/restart`; a 404 from a known route shows the same button), CSV export of every
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
radar/relevance.py        is a headline / video / search / post really about the brand (aliases, look-alikes)
radar/maintenance.py      versioned data repairs that run in every copy of the database (local and the worker)
radar/signals.py          cross-event rules (price war, stock-out, store dry, launch, undercut, pain point, momentum)
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
radar/static/theme.css    design system shared by the hub and every sector dashboard (tokens, light/dark, components)
radar/static/index.html   dashboard shell and the core views; long pages (Market, Financials, Pricing, Reviews, Channels, Strategy) are tabbed
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
python -m radar.maintenance                   # apply pending data repairs now (the runner does it on every sweep)
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

Optional keys per brand, for keeping its page clean:

| Key | Use it when |
|---|---|
| `apps` | Google Play package ids of its companion apps (rating, installs, update tells) |
| `search_terms` | the name is an everyday word: YouTube and autocomplete search these phrases instead (`"URBAN smartwatch"`) |
| `aliases` | product lines that can only mean this brand (`"Airdopes"`, `"Galaxy Ring"`); regex allowed (`"Whoop \d"`) |
| `exclude` | look-alikes that are never this brand (`"Urban Cruiser"`, `"Pebble Time"`, `"Insta360"`) |
| `store` + `platform` | the store left Shopify but has a JSON catalogue the radar can read (`platform: justcorseca`) |

A brand whose name is an everyday word (Noise, Urban, Polar, Whoop) or a global brand (`tier: adjacent`) only
gets a result when it also mentions a product or business context; rules live in `radar/relevance.py`.

## Ground rules baked in

Public, unauthenticated data only. One request per second per host, a descriptive User-Agent, official
APIs where they exist (Meta Ad Library, Google News RSS, Reddit RSS, crt.sh). Nothing behind a login.

## Distribution, voice of the customer, regions, sales reports and strategy

Five more pages turn the radar from "what rivals changed" into "where they sell, what buyers say, where demand is,
how Pebble is doing and what to do about it". All of it runs on the same collectors / SQLite / dashboard pipeline;
the tables live in `radar/schema_ext.py`.

| Page | What it shows | Where the data comes from |
|---|---|---|
| **Channels** | brand x channel matrix (Amazon.in, Flipkart, Blinkit per city, Zepto, Instamart, Croma, Reliance Digital, Vijay Sales, offline), prices, MRP, ratings, sponsored slots, price parity vs the brand's own store, discount depth, quick-commerce shelves per city | `collectors/channels.py` reads the public search results of Amazon.in and Flipkart for every brand (medium tier, each brand refreshed every 6 h) and Blinkit's search API per city (daily); retailers that block automated reads (Croma, Reliance) and app-only quick commerce (Zepto, Instamart) are marked **reported** from news / press / social mentions. Events: `channel_listed`, `channel_delisted`, `channel_price_drop`/`rise`, `channel_oos`/`back`, `channel_rating_drop`, `channel_undercut` (marketplace below D2C price), `qc_expansion`. Config: `config/channels.yaml` |
| **Reviews** | what buyers say per brand: items, average rating, negative share, complaint and praise themes (battery, accuracy, app, build, comfort, display, sound, service, value, delivery, design, water, features), a brand x theme heat matrix, quotes, an optional AI read per brand | `collectors/voice.py`: YouTube comments under each brand's newest videos (YouTube's own endpoint, no key), App Store reviews in the India store (`ios:` ids in `companies.yaml`), Google Play reviews (`apps:` ids), Amazon.in product-page reviews of each brand's most-rated listings, plus the Judge.me store reviews already collected. Sentiment from the star rating or a lexicon that knows Hinglish. Events: `voice_wave` (5+ complaints on one theme in a week), `praise_wave` |
| **Regions** | an India map with three modes: search interest per state per brand, Pebble's own sales per state and city, Blinkit shelves per city; "where rivals out-index Pebble", strongholds, brand reach | `collectors/regions.py`: Google Trends interest by sub-region (no key, 12 brands a day), place names mentioned next to each brand in news / social / comments, the quick-commerce listings, and the `sales` table |
| **Reports** | daily / weekly / monthly / yearly performance reports and the sector & industry report, with an AI executive summary when a key is set; a sales trend, segment / channel / state / SKU splits and growth vs the previous period; rivals' **sales-proxy index** (units estimated from reviews and ratings gained in the period); CSV import of Pebble's own sales | `radar/sales.py`, `radar/reports.py`. Own sales come from a CSV / JSON import (`POST /api/sales/import`, template on the page) or hourly from the Shopify Admin API when `SHOPIFY_ADMIN_TOKEN` is set. Reports are written automatically (daily 08:00 IST, weekly Monday, monthly + sector on the 1st, yearly on 1 January) by the scheduler and the GitHub Actions workflow, pushed to Telegram / email, stored in `reports`, downloadable as Markdown |
| **Strategy** | segment scorecard (defend / attack / enter / watch from size, growth, HHI concentration, Pebble's share, price fit, white space), USP claims with the evidence (rival complaint themes Pebble does not share, Pebble's praise, subscription-free wedge, accuracy evidence), problems to fix by severity, threats, opportunities, SWOT, a 30 / 60 / 90-day plan and an AI strategy memo | `radar/strategy.py` over everything above plus `config/market.yaml`; recomputed every 6 hours or on demand |

Chat tools (`channels`, `voice`, `regions`, `sales_report`, `strategy`, `reports`), CSV exports (`channel_listings`,
`channel_history`, `voice`, `region_interest`, `sales`, `reports`) and the Telegram `/report [daily|weekly|monthly|yearly|sector]`
command cover the same data. `python -m radar.reports --kind monthly` writes a report from the command line;
`python -m radar.collectors.sales file.csv` imports sales on the worker.

Sales proxies are a relative gauge, not a count: `proxy:` in `config/channels.yaml` holds the ratings-per-100-units
assumptions; replace them with your own conversion rates. They use two kinds of evidence: counter deltas between
snapshots (store review totals, app rating counts, marketplace rating counts) once two days of history exist, and the
publish dates of the reviews and comments themselves from day one. Listings of third-party straps, cases and
"compatible with" accessories are ignored so a ₹186 strap for a Pebble watch never counts as a Pebble listing.

The same data is reflected on the older pages: **Today** carries "On the shelf" (live listings, Pebble's quick-commerce
cities) and "Buyer voice 7d" tiles; every **brand page** has Channels (listings per marketplace and city, marketplace
vs own-store parity, shelf changes, retail mentions) and Regions (search interest by state, Blinkit cities) tabs next
to Reviews; **Pricing** opens with "Marketplace vs own store"; the daily / weekly **brief** gets "On the shelf" and
"Buyer voice" sections. Price parity is computed live from the current listings matched to the brand's store catalogue
(fuzzy title match plus a model-number guard, so Champ 3 never matches Champ 2).

Amazon.in throttles bursts with a 503 page: the reader raises `Blocked`, retries once after a pause, and after two
consecutive throttles the channel backs off for 45 minutes; a (brand, channel) pair only counts as refreshed when it
was actually read, so nothing is silently skipped for six hours.
