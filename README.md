# Inf Money Stock Bot

A fully automated, catalyst-driven day-trading engine powered by Python, Interactive Brokers (IBKR), and GPT-4o. The bot scans the market every morning, scores hot stocks against real newswire data using AI, allocates capital proportionally to conviction, executes buy orders through IBKR, and publishes results to a live public dashboard via GitHub Pages.

> **Paper trading only** — all defaults point at the IBKR paper account. Switching to live requires explicit configuration.

---

## Table of Contents

1. [How It Works — Full Pipeline](#how-it-works--full-pipeline)
2. [AI Prompts & Scoring](#ai-prompts--scoring)
3. [Allocation Math](#allocation-math)
4. [Configuration Reference](#configuration-reference)
5. [Environment Variables](#environment-variables)
6. [Project Structure](#project-structure)
7. [Setup & Installation](#setup--installation)
8. [Running the Bot](#running-the-bot)
9. [Dashboard](#dashboard)

---

## How It Works — Full Pipeline

The bot runs once per day, triggered by a cron job at **9:31 AM ET** (one minute after market open). Here is every stage in order.

---

### Stage 1 — Market Scanner

The bot queries IBKR's built-in market scanner across **15 scan codes** simultaneously and deduplicates the results into a single candidate pool.

| Scan Code | What it finds |
|-----------|---------------|
| `TOP_PERC_GAIN` | Biggest % gainers on the day |
| `TOP_OPEN_PERC_GAIN` | Biggest % gainers since open |
| `HIGH_OPEN_GAP` | Stocks that gapped up at open |
| `TOP_AFTER_HOURS_PERC_GAIN` | Pre/post-market movers |
| `MOST_ACTIVE` | Highest share volume |
| `MOST_ACTIVE_USD` | Highest dollar volume |
| `HOT_BY_VOLUME` | Unusual volume relative to average |
| `TOP_VOLUME_RATE` | Volume acceleration rate |
| `TOP_TRADE_COUNT` | Most individual trades placed |
| `HIGH_VS_52W_HL` | Near 52-week highs |
| `HIGH_LAST_VS_EMA20` | Price well above 20-day moving average |
| `HOT_BY_PRICE_RANGE` | Widest intraday price range |
| `HOT_BY_OPT_VOLUME` | Unusual options contract volume |
| `TOP_OPT_IMP_VOLAT_GAIN` | Implied volatility spike (options market sensing a move) |
| `SCAN_socialSentimentScoreChange_DESC` | Reddit / social sentiment surge |

**Hard filters applied to every scanner result:**

- Price ≥ `price_min` (default $5 — filters out penny stocks)
- Volume ≥ `volume_min` (default 500,000 — ensures liquidity)
- Market cap ≤ `market_cap_max_b` billion (default $10B — large caps move too slowly)
- Any tickers already held in the account are excluded here so the bot never double-buys
- Any tickers in `always_exclude` are removed (e.g. NVDA, TSLA)

After deduplication, the candidate pool is typically **50–200 unique tickers**.

---

### Stage 2 — Momentum Filter

Because `aggressive_mode` is enabled by default, the long-term trend filter is skipped entirely (historical trend is irrelevant for catalyst-driven binary events). Instead, every candidate goes through an **aggressive momentum filter**:

A stock is **rejected** if:
- Its opening gap is **fading** — price is retreating back toward yesterday's close rather than holding the gap
- The stock is **red on the day** — trading below yesterday's close

The logic: a gapping stock that is already giving back gains has lost momentum. The bot only wants stocks that are holding or continuing their move.

After this filter, the pool typically narrows to **20–60 survivors**.

> In conservative mode (`aggressive_mode: false`), this is replaced by a trend filter requiring minimum monthly, quarterly, and overall price appreciation, followed by a simple gap size filter.

---

### Stage 3 — News Fetch

For every surviving ticker, IBKR is queried for up to **5 recent news articles** from three professional newswire providers:

| Code | Provider | Description |
|------|----------|-------------|
| `FLY` | Fly on the Wall | Fast-breaking market news, earnings wires, FDA decisions |
| `BRFG` | Briefing.com | Analyst upgrades/downgrades, previews, summaries |
| `DJ-N` | Dow Jones Newswires | Reuters/WSJ-tier institutional breaking news |

Each article returns a timestamp, provider code, headline, and full article body (HTML stripped).

**Tickers with zero news articles are dropped.** If no professional newswire has anything to say about a stock that is gapping up 15%, it is pure price action with no identifiable catalyst — not a trade the bot takes.

---

### Stage 4 — Trend Data Fetch

For every ticker that survived with news, **one year of daily price bars** is pulled from IBKR and compressed into a compact trend summary:

```
daily: +2.3% | weekly: +8.1% | monthly: +15.4% | quarterly: +22.0% | yearly: +45.2%
```

This string is passed directly into the AI prompt as context. It allows GPT to see whether the stock has a healthy underlying trend or whether the catalyst is a one-day spike on an otherwise collapsing stock.

---

### Stage 5 — AI Scoring (GPT-4o)

This is the core of the selection process. Every ticker with news gets its own **GPT-4o call**, and up to 10 run in **parallel** using a thread pool. See [AI Prompts & Scoring](#ai-prompts--scoring) for the full breakdown of what GPT receives and returns.

---

### Stage 6 — Score Threshold Filter

After all scores come back, the bot tries to fill `num_stocks` (default 10) slots:

1. Keep only tickers with `direction == "bullish"` and `score >= threshold`
2. Sort by score descending, take the top `num_stocks`
3. If fewer than `num_stocks` are found, lower the threshold by 1 and retry
4. Never go below `score_floor` (default 4)

**Market context adjustment:** If SPY (the S&P 500) is down more than `spy_down_threshold` percent on the day (default −1.0%), the minimum score is raised to 10 — only undeniable catalysts get bought when the broad market is selling off.

If after threshold relaxation there are still fewer than `num_stocks` picks, the bot expands the candidate pool to the full conservative universe (gap filter only, no momentum filter) and rescores those.

---

### Stage 7 — Proportional Allocation

Each surviving pick is assigned a capital allocation percentage using a conviction-weighted formula. See [Allocation Math](#allocation-math) for the full breakdown.

---

### Stage 8 — Order Execution

For each pick:

```
dollar_amount = (allocation_pct / 100) × NetLiquidation
shares        = dollar_amount / current_price
```

`NetLiquidation` is read directly from the live IBKR account — not from the JSON file — so the allocation always reflects the actual account balance.

Orders are submitted as **market orders** (filled at the best available price). After all orders are submitted, the bot waits **10 seconds** for fills. Actual `avgFillPrice` and `filled` quantity are then read back from IBKR and written to `portfolio.json`.

---

### Stage 9 — Portfolio Record

After fills are confirmed, `portfolio.json` is updated with:
- The session date, mode, and SPY context
- Per-pick data: actual fill price, actual shares, buy value, allocation %, score, risk, expected gain, reason
- QQQ price at buy time (used as the NASDAQ benchmark)
- Portfolio open value for the session

The `run_morning.sh` wrapper script then commits this file to GitHub so the dashboard updates automatically.

---

## AI Prompts & Scoring

### Active Prompt: `catalyst_prompt.txt`

This is the prompt currently used in production. It is injected with three pieces of live data at runtime:

- `{ticker}` — the stock symbol
- `{trend_summary}` — the multi-timeframe price trend string from Stage 4
- `{news_items}` — up to 5 formatted news articles with timestamps, provider, headline, and first 300 characters of body

The prompt instructs GPT to act as a **catalyst analyst**, not a general stock picker. The critical framing is that these stocks have **already moved 10–40% today** — GPT is asked to assess remaining upside, not confirm that a move happened.

GPT returns a JSON object with five fields:

---

#### `direction` — `"bullish"` or `"bearish"`

The binary gate. Only `"bullish"` picks survive to the next stage.

The prompt **defaults to `"bearish"` on any ambiguity**. This is an intentional conservative bias — it is better to miss a trade than to buy into a fading move.

---

#### `score` — integer 1–10 (catalyst quality)

Rates the **news quality**, not the price action. The strict scale:

| Score | Meaning |
|-------|---------|
| **9–10** | Undeniable binary event with clear continued upside. FDA approval on a small biotech, acquisition announced at a significant premium, massive earnings beat with raised guidance. News must be from today and not yet fully reflected in price. |
| **7–8** | Strong named catalyst with clear upside. Earnings beat, analyst upgrade with a large price target raise, major new contract with revenue numbers, confirmed short squeeze. |
| **5–6** | Real but moderate catalyst. Sector tailwind, minor guidance raise, analyst initiates at Buy, positive product update without revenue impact. |
| **3–4** | Weak or stale. Vague positive mention, news 1–2 days old and already reflected, generic commentary, no named catalyst. |
| **1–2** | Noise. No real catalyst, pure price action, clickbait headline, news clearly 100% priced in. |

**Hard override rules baked into the prompt:**
- Stock already up 20%+ today → `score ≤ 5` (unless there is a new secondary catalyst not yet reflected)
- Stock already up 40%+ today → `score ≤ 3`

---

#### `risk` — integer 1–5 (reversal risk)

Rates how likely the trade is to reverse sharply. Independent of catalyst quality — a great catalyst on a stock up 40% still carries high risk.

| Risk | Meaning |
|------|---------|
| **1** | Very low. Large-cap, strong multi-timeframe uptrend, high-conviction fresh catalyst, highly liquid. |
| **2** | Low. Solid catalyst, reasonable market cap, stock near support or early in the move. |
| **3** | Moderate. Speculative catalyst, extended from open, or weak underlying trend. |
| **4** | High. Stock already up 30%+, catalyst is soft, negative trend, or low float. |
| **5** | Very high. Parabolic move (40%+ today), vague or recycled catalyst, strong yearly downtrend. |

**Trend adjustments:**
- Yearly trend worse than −30% → +1 to risk
- Strong uptrend (monthly AND yearly both positive) → −1 to risk
- Final value is clamped to [1, 5]

---

#### `expected_gain_pct` — float (remaining intraday upside %)

GPT's conservative, realistic estimate of how much more the stock could gain **from the current price** within today's session.

| Scenario | Expected range |
|----------|----------------|
| Fresh FDA approval on small biotech | 15–30% |
| Earnings beat right after open | 5–12% |
| Analyst upgrade midday | 2–5% |
| Stock already ran 30% on vague news | 0–2% |
| Expects to fade or stall | 0.0–1.0% |

---

#### `reason` — one sentence

Plain-English explanation of the catalyst and the key risk or conviction driver. This is displayed on the website dashboard.

---

### Legacy Prompt: `picker_prompt.txt`

An earlier, simpler design that asked GPT to pick **one stock per call** from its training data — politician disclosures, Reddit hype, breaking news, unusual volume. It is **not used in the current pipeline** and has been superseded by the scanner → news → `catalyst_prompt.txt` flow, which grounds every decision in live IBKR data rather than model knowledge.

It is kept in the repo for reference.

---

### Model & Parameters

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model | `gpt-4o` | Best reasoning quality for financial analysis |
| Temperature | `0.3` | Near-deterministic — consistent, analytical, not creative |
| Max tokens | `200` | Short JSON response only |
| Parallelism | 10 workers | Score all tickers simultaneously to minimize latency |

---

## Allocation Math

Once the top picks are selected, capital is allocated proportionally to a **conviction score** derived from the three AI fields:

```
conviction = score × max(expected_gain_pct, 0.5) / risk
```

A high-quality catalyst (score 9), big remaining upside (20%), low risk (2):
```
conviction = 9 × 20 / 2 = 90
```

A moderate catalyst (score 6), modest upside (4%), higher risk (3):
```
conviction = 6 × 4 / 3 = 8
```

The first stock receives roughly **90 / (90 + 8) = 91.8%** of the budget relative to just those two — but it is capped at 35%.

### Iterative redistribution

The algorithm enforces two hard constraints:

- **Minimum per pick: 5%** — every selected stock gets at least a small position
- **Maximum per pick: 35%** — no single stock takes more than a third of the portfolio

It runs in passes:

1. Compute raw proportional allocations for all unconstrained picks
2. Any pick exceeding 35% is fixed at 35%; any pick below 5% is fixed at 5%
3. The leftover budget is redistributed proportionally to the remaining unconstrained picks
4. Repeat until no picks are hitting a constraint
5. Total is guaranteed to sum to exactly 100%

### Example — 10 picks, $10,000 account

| Ticker | Score | Risk | Exp. Gain | Conviction | Allocation | Amount |
|--------|-------|------|-----------|------------|------------|--------|
| BIOA | 9 | 2 | 25% | 112.5 | 35% | $3,500 |
| XYZ | 8 | 2 | 12% | 48.0 | 21% | $2,100 |
| ABC | 7 | 3 | 8% | 18.7 | 11% | $1,100 |
| DEF | 6 | 3 | 5% | 10.0 | 7% | $700 |
| GHI | 5 | 3 | 4% | 6.7 | 5% | $500 |
| *(5 more)* | | | | | 5% each | $500 each |

The biotech hits the 35% ceiling; the excess conviction flows to the next-highest picks. The weakest picks floor at 5%.

---

## Configuration Reference

All runtime behaviour is controlled by `src/stock_bot/config/picker_config.json`. Changes take effect on the next run — no code change required.

```json
{
  "aggressive_mode": true,
  "aggressive_min_score": 6,
  "spy_down_threshold": -1.0,
  "num_stocks": 10,
  "min_score": 5,
  "score_floor": 4,
  "max_open_gap_pct": 5.0,
  "always_exclude": ["NVDA", "TSLA", "BTC"],
  "trend_filters": { ... },
  "scanner": { ... },
  "news": { ... }
}
```

---

### Top-Level Fields

#### `aggressive_mode` — boolean (default: `true`)

Controls which filtering and scoring path is used.

| Mode | Behaviour |
|------|-----------|
| `true` | **Aggressive** — skips the long-term trend filter entirely. Uses the momentum filter (gap holding, not red on day). Suitable for catalyst-driven binary events (FDA, earnings, acquisitions) where historical trend is irrelevant. |
| `false` | **Conservative** — requires stocks to meet minimum trend thresholds (see `trend_filters`) before scoring. More suitable for swing-trade setups or quieter market conditions. |

---

#### `aggressive_min_score` — integer (default: `6`)

The starting minimum score threshold when `aggressive_mode` is `true`. The bot begins filtering at this score and lowers it one point at a time until `num_stocks` picks are found or `score_floor` is reached.

**Tuning guidance:**
- Raise to `7` or `8` for higher quality / fewer trades on busy news days
- Lower to `5` on slow news days if you find the bot is coming up empty
- In a strong market with multiple FDA approvals in one day, `8` is appropriate
- In a quiet market, `5` or `6` ensures the bot still deploys capital

---

#### `spy_down_threshold` — float, percentage (default: `-1.0`)

If SPY (S&P 500) is down more than this percentage on the day when the bot runs, the minimum score is automatically raised to **10** — only undeniable catalysts are traded in a falling market.

**Examples:**
- `-1.0` — market down 1% or more → raise bar to 10
- `-2.0` — only raise the bar if the market is down 2% or more (more tolerant)
- `-0.5` — very cautious, raises the bar on any meaningful red day

Set to a very large negative number (e.g. `-100`) to effectively disable this safety.

---

#### `num_stocks` — integer (default: `10`)

Target number of stocks to buy per session. The bot tries to hit exactly this number through score threshold relaxation. Capital is always divided among however many picks are actually found, even if fewer than `num_stocks`.

**Tuning guidance:**
- `10` — full diversification, 10% average position size (before conviction weighting)
- `5` — more concentrated, higher conviction required
- `15` — wider diversification, smaller average positions

---

#### `min_score` — integer (default: `5`)

The starting minimum score threshold when `aggressive_mode` is `false` (conservative mode). The same threshold relaxation logic applies.

---

#### `score_floor` — integer (default: `4`)

The absolute minimum score the bot will accept regardless of how many picks it has found. It will never buy a stock scoring below this threshold even if it means buying fewer than `num_stocks` stocks that day.

**Tuning guidance:**
- `4` — minimum bar; score 4 means the catalyst is weak or partially stale
- `5` — slightly higher bar; avoids the weakest plays
- `6` — only buys stocks with real, named catalysts

---

#### `max_open_gap_pct` — float, percentage (default: `5.0`)

Maximum allowed opening gap size. Stocks that opened more than this % above yesterday's close are considered to have already moved too far at open and are filtered out in conservative mode, or flagged for fading detection in aggressive mode.

**Tuning guidance:**
- `5.0` — standard; rejects stocks that gapped up more than 5% at open
- `10.0` — more permissive; allows larger gaps (useful for biotech events)
- `3.0` — stricter; only takes small, controlled gap-ups

---

#### `always_exclude` — list of strings (default: `["NVDA", "TSLA", "BTC"]`)

Tickers that are permanently excluded from consideration regardless of their score. In addition to this list, any tickers currently held in the IBKR account are also excluded at runtime (to prevent double-buying).

Add tickers here that you never want the bot to trade — for example, stocks you own in a separate long-term account, or instruments the scanner picks up that are not valid day-trade targets.

---

### `trend_filters` — used in conservative mode only

Minimum required price change over each timeframe for a stock to pass the trend filter. Only evaluated when `aggressive_mode` is `false`. All values are percentages.

```json
"trend_filters": {
  "monthly":   { "min": 5.0,  "max": null },
  "quarterly": { "min": 10.0, "max": null },
  "overall":   { "min": 10.0, "max": null }
}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `monthly.min` | `5.0` | Stock must be up at least 5% over the past month |
| `quarterly.min` | `10.0` | Stock must be up at least 10% over the past quarter |
| `overall.min` | `10.0` | Stock must be up at least 10% over the full measured window |
| `max` | `null` | No upper cap (set a value to exclude runaway stocks if desired) |

**Tuning guidance:**
- Setting all minimums to `0.0` effectively disables trend filtering without switching to aggressive mode
- Raising thresholds (e.g. monthly to `10.0`) focuses on stocks already in strong established uptrends
- Adding a `max` cap (e.g. `"monthly": {"min": 5.0, "max": 50.0}`) excludes stocks that have already run too far to have more upside

---

### `scanner`

Controls the IBKR market scanner behaviour.

```json
"scanner": {
  "market_cap_max_b": 10.0,
  "scan_codes": [ ... ],
  "price_min": 5.0,
  "volume_min": 500000,
  "max_per_scan": 50
}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `market_cap_max_b` | `10.0` | Maximum market cap in billions. Large-cap stocks (AAPL, MSFT) move 1–2% on big news; this bot targets 5–30% intraday moves. |
| `scan_codes` | *(15 codes)* | List of IBKR scanner codes to run. Add or remove codes to change what the universe looks like. |
| `price_min` | `5.0` | Minimum stock price in dollars. Filters out true penny stocks which have erratic price action and poor liquidity. |
| `volume_min` | `500000` | Minimum daily share volume. Ensures enough liquidity to enter and exit without significant slippage. |
| `max_per_scan` | `50` | Maximum results to pull per individual scan code. Higher values give a broader universe but increase processing time. |

**Tuning guidance:**
- Raise `market_cap_max_b` to `20.0` or `50.0` to include mid-cap stocks; these are more stable but less explosive
- Lower `price_min` to `2.0` to include small stocks (higher volatility, less reliable fills)
- Lower `volume_min` to `100000` on slow news days; raise to `1000000` for tighter liquidity requirements
- Remove `SCAN_socialSentimentScoreChange_DESC` if you want to filter out Reddit-driven meme plays
- Remove `TOP_AFTER_HOURS_PERC_GAIN` if you only want stocks with confirmed intraday momentum

---

### `news`

Controls how many news articles are fetched and from which providers.

```json
"news": {
  "providers": "FLY+BRFG+DJ-N",
  "max_articles": 5
}
```

| Field | Default | Meaning |
|-------|---------|---------|
| `providers` | `"FLY+BRFG+DJ-N"` | IBKR newswire codes to query, joined by `+`. Must match IBKR's supported provider codes for your subscription level. |
| `max_articles` | `5` | Maximum articles per ticker sent to GPT. More articles give GPT better context but increase token usage and add latency to each parallel scoring call. |

**Tuning guidance:**
- Increasing `max_articles` to `10` gives GPT more context on multi-day catalysts (e.g. ongoing FDA review) but increases cost and latency
- Setting `max_articles` to `1` is the fastest option; GPT only sees the headline and first 300 characters — suitable for high-frequency scanning on many tickers
- Remove provider codes from `providers` if you do not have a subscription to that newswire

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `OPENAI_API_KEY` | — | Yes | OpenAI API key for GPT-4o scoring |
| `IB_MODE` | `paper` | No | `paper` or `live` — controls which port and account are used |
| `IB_HOST` | `127.0.0.1` | No | TWS or IB Gateway host |
| `IB_PORT_PAPER` | `4002` | No | Port for the paper trading Gateway |
| `IB_PORT_LIVE` | `4001` | No | Port for the live trading Gateway |
| `IB_ACCOUNT_PAPER` | — | Yes | Paper account ID (e.g. `DU123456`) |
| `IB_ACCOUNT_LIVE` | — | If live | Live account ID |
| `IB_CLIENT_ID` | `1` | No | IBKR API client ID — change if running multiple connections |
| `IB_EXCHANGE` | `SMART` | No | Order routing exchange — `SMART` uses IBKR's smart routing |
| `IB_CURRENCY` | `USD` | No | Currency for all orders |
| `LOG_LEVEL` | `INFO` | No | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `LOG_FILE` | `logs/stock_bot.log` | No | Path to the rotating log file |

---

## Project Structure

```
stock_bot/
├── src/stock_bot/
│   ├── main.py                          # Entry point — orchestrates the full pipeline
│   ├── ai/
│   │   └── catalyst_scorer.py           # GPT-4o scoring, allocation math
│   ├── brokers/ib/
│   │   ├── connect_disconnect.py        # IBKR connection singleton
│   │   ├── buy_stocks.py                # Buy order execution (market, limit, bracket)
│   │   ├── sell_stocks.py               # Individual sell orders
│   │   └── sell_all.py                  # Close an entire position
│   ├── config/
│   │   ├── settings.py                  # Environment-based settings dataclasses
│   │   └── picker_config.json           # Runtime configuration (edit to tune behaviour)
│   ├── core/
│   │   └── logging_config.py            # Rotating file + console logging setup
│   ├── data_sources/
│   │   ├── scanner.py                   # IBKR market scanner
│   │   ├── news_fetcher.py              # IBKR newswire fetcher (FLY, BRFG, DJ-N)
│   │   ├── trend_checker.py             # Multi-timeframe price trend analysis
│   │   └── portfolio_writer.py          # Reads/writes portfolio.json + IBKR account value
│   └── templates/
│       ├── catalyst_prompt.txt          # Active AI prompt (edit to tune GPT behaviour)
│       └── picker_prompt.txt            # Legacy single-pick prompt (not in active use)
│
├── scripts/
│   ├── run_morning.sh                   # Cron wrapper: runs bot, commits portfolio.json
│   ├── liquidate_paper.py               # Close all open positions on the paper account
│   └── ...
│
├── docs/                                # GitHub Pages dashboard
│   ├── index.html
│   ├── styles.css
│   ├── script.js                        # Live price fetching, chart, KPIs
│   └── data/
│       └── portfolio.json               # Session history, equity curve, picks
│
└── CLAUDE.md                            # AI assistant instructions for this repo
```

---

## Setup & Installation

### Prerequisites

- Python 3.10+
- IBKR Trader Workstation (TWS) or IB Gateway running locally
- An IBKR paper trading account enabled
- OpenAI API key with GPT-4o access

### Install

```bash
git clone https://github.com/PCarroll9500/stock_bot.git
cd stock_bot
pip install -e ".[dev,ib]"
```

### Configure

```bash
cp .env.example .env
# Edit .env with your IBKR account IDs and OpenAI key
```

---

## Running the Bot

### Automatic (recommended)

A cron entry fires the wrapper script at 9:31 AM ET on trading days:

```
31 9 * * 1-5 /path/to/stock_bot/scripts/run_morning.sh
```

The wrapper activates the virtual environment, runs the bot, and commits the updated `portfolio.json` to GitHub so the dashboard updates automatically.

### Manual — paper trading (default)

```bash
.venv/bin/python -m stock_bot.main
```

Requires `IB_MODE=paper` (or unset) in `.env`. Connects to IBKR on port `IB_PORT_PAPER` (default `4002`) using `IB_ACCOUNT_PAPER`.

### Manual — live trading (real money)

```bash
.venv/bin/python -m stock_bot.main
```

Set the following in `.env` before running:

```env
IB_MODE=live
IB_ACCOUNT_LIVE=your_live_account_id
```

This connects on port `IB_PORT_LIVE` (default `4001`) and places real market orders.

> **Warning:** Live mode submits real orders immediately. Ensure IBKR Gateway/TWS is running on port 4001, your live account ID is correct, and you have reviewed `picker_config.json` before running.

### CLI flags

| Flag | Effect |
|------|--------|
| *(none)* | Normal run — scans, scores, and places real orders (paper or live depending on `IB_MODE`) |
| `--test` | Dry run — runs the full pipeline but **skips order execution**. Writes output to `portfolio_test.json` instead of `portfolio.json`. Use this to verify the pipeline works before trading real money. |
| `--sequential` | Processes one ticker at a time instead of running concurrently. Slower but easier to step through in a debugger. Combine with `--test` for safe debugging. |

```bash
# Dry run — no orders placed, safe to run any time
.venv/bin/python -m stock_bot.main --test

# Dry run, sequential (easiest to debug)
.venv/bin/python -m stock_bot.main --test --sequential

# Live run, sequential (useful for monitoring first live trade)
.venv/bin/python -m stock_bot.main --sequential
```

### Close all positions (paper account only)

```bash
.venv/bin/python scripts/liquidate_paper.py
```

---

## Dashboard

The public dashboard lives at:
**https://pcarroll9500.github.io/stock_bot/**

It is a static page served from the `docs/` folder via GitHub Pages. It reads `docs/data/portfolio.json` on load and:

- Displays live portfolio value by fetching current prices from Yahoo Finance / Stooq
- Shows total return and today's return
- Compares performance against QQQ (NASDAQ) indexed to the same starting capital
- Renders an equity curve chart
- Lists today's picks with live prices, per-position P&L, scores, and reasons
- Shows full session history

The dashboard auto-refreshes prices every 60 seconds while the page is open. It uses parallel CORS proxy racing across multiple providers with a Stooq.com fallback for reliability.
