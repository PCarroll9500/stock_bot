# Stock Bot Architecture & Data Flow

## System Overview

The bot runs once daily at **8:00 AM ET** (premarket) to identify and trade high-potential stocks before market open.

```
┌─────────────────────────────────────────────────────────────────┐
│                      STOCK BOT PIPELINE                         │
└─────────────────────────────────────────────────────────────────┘

 START (8:00 AM ET)
   │
   ├─→ [1] CONNECT TO IBKR
   │        └─ Interactive Brokers Gateway (TWS/Gateway)
   │
   ├─→ [2] MARKET SCAN
   │        └─ 15 IBKR scanner codes (gap, momentum, volume, etc)
   │           Output: 50-200 candidates
   │
   ├─→ [3] FILTER CANDIDATES
   │        ├─ Gap filter (max 5% open move)
   │        ├─ Momentum filter (aggressive/conservative)
   │        └─ Output: 20-60 survivors
   │
   ├─→ [4] FETCH NEWS
   │        ├─ IBKR newswire (primary)
   │        └─ Finviz + Yahoo + Google (fallback)
   │           Output: Tickers WITH news only
   │
   ├─→ [5] FETCH ENHANCED DATA (NEW)
   │        ├─ Earnings calendar (Yahoo Finance)
   │        ├─ Social sentiment (Reddit, StockTwits)
   │        ├─ SEC filings (EDGAR, Form 4/8-K)
   │        └─ Web scraper (Seeking Alpha, Motley Fool, etc)
   │
   ├─→ [6] FETCH PRICE TRENDS
   │        ├─ 1-year daily bars → % changes
   │        ├─ Pre-score trend filter (drop weak trends)
   │        └─ Output: Trends formatted for GPT
   │
   ├─→ [7] LLM SCORING (GPT-4o-mini)
   │        ├─ Input: news + earnings + sentiment + SEC + trends
   │        ├─ Score: 1-10 catalyst quality
   │        ├─ Direction: bullish/bearish
   │        ├─ Risk: 1-5
   │        └─ Expected gain: % upside remaining
   │
   ├─→ [8] PICK SELECTION
   │        ├─ Filter: bullish, score ≥ threshold, gain ≥ min
   │        ├─ Rank by score (descending)
   │        └─ Output: Top 10 picks (or configurable)
   │
   ├─→ [9] CAPITAL ALLOCATION
   │        ├─ Proportional: score × gain / risk
   │        ├─ Constraints: 5-35% per pick, 100% total
   │        └─ Output: $ amount per ticker
   │
   ├─→ [10] PLACE PREMARKET ORDERS
   │        ├─ Market orders (MKT)
   │        ├─ Session: PREMARKET (fills before 9:30 AM)
   │        ├─ Optional: Bracket orders (take-profit + stop-loss)
   │        └─ Wait 60s for fills
   │
   ├─→ [11] LOG & RECORD
   │        ├─ Portfolio.json (picks, fills, allocation)
   │        ├─ LLM inputs/outputs (transparency)
   │        └─ Push to GitHub
   │
   └─→ [12] CLOSE OF DAY (2:30 PM ET)
            ├─ Liquidate all positions (market order)
            ├─ Record P&L
            └─ Email report via SNS
```

---

## Component Diagram

```
┌───────────────────────────────────────────────────────────────────┐
│                         MAIN.PY (Orchestrator)                    │
└───────────────────────────────────────────────────────────────────┘
                                  │
                ┌─────────────────┼─────────────────┐
                │                 │                 │
        ┌───────▼────────┐  ┌────▼────────┐  ┌───▼────────────┐
        │  DATA SOURCES  │  │     AI      │  │    BROKERS     │
        └────────────────┘  └─────────────┘  └────────────────┘
                │                 │                 │
        ┌───────┴─────────┬───────┴───────┐  ┌──────┴──────┐
        │                 │               │  │             │
   ┌────▼─────────┐  ┌───▼──────────┐ ┌─▼──▼────────┐  ┌─▼────────┐
   │ Data Fetchers│  │ Catalyst     │ │ Buy/Sell    │  │ Portfolio│
   │              │  │ Scorer       │ │ Orders      │  │ Writer   │
   ├──────────────┤  ├──────────────┤ ├─────────────┤  ├──────────┤
   │ • scanner    │  │ • _score     │ │ • buy_stock │  │ • write  │
   │ • news       │  │   _ticker()  │ │ • sell_     │  │   session│
   │ • earnings   │  │ • filter_    │ │   stock()   │  │ • get_   │
   │ • sentiment  │  │   and_rank() │ │ • close_    │  │   account│
   │ • sec        │  │ • _compute   │ │   position()│  │   _value │
   │ • web_scrape │  │   allocations│ └─────────────┘  └──────────┘
   │ • trends     │  └──────────────┘
   └──────────────┘

        IBKR API        OpenAI API        IBKR API      GitHub
        (Scanner,       (GPT-4o-mini)     (Orders)      (Push)
        News, Prices)
```

---

## Data Flow Diagram

```
┌──────────────┐
│  8:00 AM ET  │
└──────┬───────┘
       │
       ▼
  ┌─────────────────────────────────────────┐
  │ IBKR Scanner                            │
  │ (15 scanner codes: gap, momentum, vol)  │
  └────────────────┬────────────────────────┘
                   │ 50-200 tickers
                   ▼
  ┌─────────────────────────────────────────┐
  │ Filter: Gap, Momentum, Price Filters    │
  └────────────────┬────────────────────────┘
                   │ 20-60 tickers
                   ▼
  ┌──────────────────────────────────────────────┐
  │ NEWS FETCHING (Parallel)                     │
  │  ├─ IBKR Newswire (primary)                  │
  │  └─ Finviz/Yahoo/Google (fallback)           │
  │     ↓ Tickers WITHOUT news are DROPPED      │
  └────────────────┬─────────────────────────────┘
                   │ Tickers WITH news
                   ▼
  ┌──────────────────────────────────────────────┐
  │ ENHANCED DATA FETCHING (Parallel - NEW)      │
  │  ├─ earnings_fetcher.py                      │
  │  │   ├─ Scrape Yahoo Finance                 │
  │  │   └─ → {earnings_date, eps, time}         │
  │  ├─ sentiment_fetcher.py                     │
  │  │   ├─ Scrape Reddit (r/wsb, r/stocks)      │
  │  │   ├─ Scrape StockTwits                    │
  │  │   └─ → {mentions, sentiment_score}        │
  │  ├─ sec_fetcher.py                           │
  │  │   ├─ Scrape SEC EDGAR                     │
  │  │   ├─ Scrape Yahoo insider trades          │
  │  │   └─ → {form_type, summary, significance} │
  │  └─ web_scraper.py                           │
  │      ├─ Seeking Alpha, Motley Fool, etc      │
  │      └─ → {title, source, sentiment}         │
  │          [Sentiment: bullish/neutral/bearish]│
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ TREND FETCHING (1Y daily bars)               │
  │  ├─ 1d, 1w, 1m, 3m, 1yr % returns            │
  │  ├─ Pre-score filter (drop weak trends)      │
  │  └─ Format as: "1d: +2.3% | 1w: +8.1%..."    │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ LLM SCORING (GPT-4o-mini, Parallel)          │
  │                                               │
  │  For each ticker:                            │
  │    Input → {                                 │
  │      ticker: "XYZ",                          │
  │      news: [{title, source, ...}],          │
  │      trends: "1d: +2.3% | ...",             │
  │      earnings: {date, eps, time},           │
  │      sentiment: {reddit, stocktwits},       │
  │      sec: [{form, summary, significance}],  │
  │    }                                         │
  │                                               │
  │    ↓ (catalyst_prompt.txt)                   │
  │                                               │
  │    Output → {                                │
  │      ticker: "XYZ",                          │
  │      score: 8,          [1-10]               │
  │      direction: "bullish",                   │
  │      risk: 2,           [1-5]                │
  │      expected_gain_pct: 12.5,                │
  │      reason: "..."                           │
  │    }                                         │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ LOG LLM INPUTS/OUTPUTS (NEW)                 │
  │  └─ logs/llm_inputs/{date}/{ticker}.json     │
  │     Allows inspection: inspect_llm_inputs.py │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ PICK SELECTION                               │
  │  ├─ Filter: direction="bullish" AND          │
  │  │          score >= threshold AND           │
  │  │          expected_gain >= min             │
  │  ├─ Rank by score (descending)               │
  │  └─ Target: 10 stocks (configurable)         │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ CAPITAL ALLOCATION                           │
  │  ├─ Proportional: score × gain / risk        │
  │  ├─ Min: 5%, Max: 35% per position           │
  │  └─ Total: exactly 100%                      │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ PLACE PREMARKET ORDERS (NEW SESSION)         │
  │  ├─ Session: PREMARKET (not REGULAR)         │
  │  ├─ Type: MKT (market orders)                │
  │  ├─ Qty: calculated from account value       │
  │  ├─ Optional: Bracket (TP + SL)              │
  │  └─ Wait 60s for fills                       │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ RECORD & PUSH                                │
  │  ├─ portfolio.json (picks, fills, P&L)       │
  │  ├─ GitHub commit (docs/data/portfolio.json) │
  │  └─ Email report (AWS SNS)                   │
  └────────────────┬─────────────────────────────┘
                   │
                   ▼
  ┌──────────────────────────────────────────────┐
  │ 2:30 PM ET: CLOSE OF DAY                     │
  │  ├─ Liquidate all positions (MKT order)      │
  │  ├─ Record closing prices & P&L              │
  │  └─ Email close report                       │
  └──────────────────────────────────────────────┘
```

---

## Class Diagram (Key Classes)

```
┌─────────────────────────────────────┐
│        CatalystScorer               │
├─────────────────────────────────────┤
│ - MODEL: "gpt-4o-mini"              │
│ - TEMPERATURE: 0.3                  │
│ - MAX_WORKERS: 1                    │
├─────────────────────────────────────┤
│ + score_candidates(news, excluded,  │
│     trend, earnings, sentiment, sec)│
│ + filter_and_rank(scored, num)      │
│ - _score_ticker(client, ticker,     │
│     articles, trend, earnings,      │
│     sentiment, sec_filings)         │
│ - _format_news_items(articles)      │
│ - _format_earnings_signal(earnings) │
│ - _format_sentiment_signal(sent)    │
│ - _format_sec_signal(filings)       │
└─────────────────────────────────────┘
           uses                 uses
             │                    │
             ▼                    ▼
    ┌──────────────────┐  ┌──────────────────┐
    │ NewsArticle      │  │ EarningsSignal   │
    ├──────────────────┤  ├──────────────────┤
    │ - headline       │  │ - ticker         │
    │ - body           │  │ - earnings_date  │
    │ - provider       │  │ - eps_estimate   │
    │ - time           │  │ - time_of_day    │
    └──────────────────┘  └──────────────────┘

    ┌──────────────────┐  ┌──────────────────┐
    │ SentimentSignal  │  │ SECFiling        │
    ├──────────────────┤  ├──────────────────┤
    │ - reddit_mentions│  │ - form_type      │
    │ - reddit_sent    │  │ - filing_date    │
    │ - stocktwits_sent│  │ - summary        │
    └──────────────────┘  │ - significance   │
                          └──────────────────┘

    ┌──────────────────┐
    │ ScoredCandidate  │
    ├──────────────────┤
    │ - ticker         │
    │ - score (1-10)   │
    │ - direction      │
    │ - risk (1-5)     │
    │ - expected_gain  │
    │ - reason         │
    └──────────────────┘
```

---

## Data Structure: Scoring Input

```python
# What GPT receives for each ticker:

{
  "ticker": "XYZ",
  "current_price": 42.50,
  
  # News
  "news": [
    {
      "headline": "XYZ beats earnings on strong guidance",
      "provider": "Reuters",
      "time": "2026-05-27 08:15",
      "body": "...",
    },
    # ... up to 5 articles
  ],
  
  # Price trends (formatted as string for LLM)
  "trends": "1d: +2.3% | 1w: +8.1% | 1m: +15.4% | ...",
  
  # Earnings calendar (NEW)
  "earnings": {
    "earnings_date": "2026-05-28",
    "eps_estimate": 2.15,
    "time_of_day": "pre-market",
  },
  
  # Social sentiment (NEW)
  "sentiment": {
    "reddit_mentions": 450,
    "reddit_sentiment": 0.78,
    "stocktwits_sentiment": 0.82,
  },
  
  # SEC filings (NEW)
  "sec_filings": [
    {
      "form_type": "FORM 8-K",
      "filing_date": "2026-05-24",
      "summary": "CEO purchase 50k shares",
      "significance": "high",
    },
  ],
  
  # Web scraper news (NEW)
  "web_news": [
    {
      "title": "...",
      "source": "Seeking Alpha",
      "sentiment": "bullish",
    },
  ],
}
```

---

## Key Improvements in This Version

| Component | Before | After |
|-----------|--------|-------|
| **Execution Time** | 9:31 AM ET (post-open) | 8:00 AM ET (premarket) |
| **Order Session** | Regular (REGULAR_TRADING) | Premarket (PREMARKET) |
| **News Sources** | IBKR only (+fallback) | 6+ sources (web scraping) |
| **Earnings Data** | ❌ Not used | ✅ Yahoo Finance calendar |
| **Social Signals** | ❌ Not used | ✅ Reddit + StockTwits |
| **SEC Data** | ❌ Not used | ✅ Form 4/8-K + insider |
| **Transparency** | Limited | ✅ Full LLM input logging |
| **Decision Review** | ❌ No tool | ✅ inspect_llm_inputs.py CLI |

---

## Execution Timeline

```
7:55 AM ET: EC2 cron wakes up (0 13 UTC)
8:00 AM ET: Run morning.sh
  │
  ├─ 0s: Connect to IBKR Gateway
  ├─ 5s: Market scan (15 scanner codes)
  ├─ 10s: Filter candidates (gap, momentum)
  ├─ 15s: Fetch news (IBKR + web fallback)
  ├─ 20s: Fetch earnings + sentiment + SEC + web (parallel)
  ├─ 25s: Fetch trend data (1Y bars)
  ├─ 30s: Score with GPT (parallel, max 10 workers)
  ├─ 35s: Select picks + allocate capital
  ├─ 37s: Place PREMARKET market orders
  ├─ 38s: Wait 60s for fills
  └─ 98s: Record results + push to GitHub

9:30 AM ET: Market opens, orders fill at premarket prices
  │
  └─ Positions held through day

2:30 PM ET: close.sh runs
  │
  ├─ Liquidate all positions (MKT order)
  ├─ Record closing prices & P&L
  └─ Send close report
```

---

## Score-vs-Returns Feedback Loop

`scripts/analyze_scoring.py` joins every realized pick's GPT score, sector,
catalyst_type and expected_gain_pct against its actual `day_return_pct`, so
config changes are driven by what's actually working rather than guesses.

```
scripts/run_weekly_report.sh   (cron: Sunday 8:00 AM ET)
  ├─ git pull
  ├─ analyze_scoring.py docs/data/portfolio.json --quiet --output docs/data/scoring_report.json
  ├─ commit + push scoring_report.json
  └─ email_scoring_report.py   (SNS digest: win rate, catalyst_type/score
                                 breakdowns, expected_gain_pct calibration)
```

Findings from this loop feed back into `picker_config.json`:
- `catalyst_type_weights` — conviction/allocation multiplier per catalyst_type
  (e.g. `ma_acquisition` discounted, `analyst_action` boosted based on
  realized win rate/return).
- `risk_penalty` — configurable allocation penalty for risk>=threshold picks,
  kept tunable rather than hardcoded since the realized-return-by-risk
  breakdown is still a small sample. Threshold raised 3→5 on 2026-08-06:
  once the SIZE RULE fix (below) started telling GPT to flag genuine
  outlier catches as risk 4-5, a threshold of 3 was halving their
  allocation on top of their already-tight `trailing_stop_by_risk` stop —
  double-penalizing exactly the picks the SIZE RULE fix exists to surface.
  Live data backed this up: risk>=3 picks got only 8-15% of total capital
  across the first two days despite including the best single performer
  both days.
- `trailing_stop_by_risk` — per-pick trailing-stop width keyed by GPT's risk
  score instead of one flat `trailing_stop_pct` for every position: tighter
  for high-risk picks (cut losers faster), wider for low-risk/high-conviction
  picks (let winners run). This is the mechanism that actually "cuts losers
  early" — the bot only runs at open and close, so a continuously-live
  trailing-stop order on the exchange is the only way to react intraday
  without a new always-on process.

### Catching explosive outlier moves (Profile A)

`catalyst_prompt.txt`'s Profile A (small/mid-cap, already up 10-40%+ on a
fresh binary event — FDA approval, acquisition, short squeeze) was originally
defined around large intraday moves but then immediately capped its own core
case: the SIZE RULE scored a genuine, high-volume 30-50% mover at only 4, and
the ALREADY-MOVED PENALTY applied a second, stacking penalty on top of that.
Combined with `aggressive_min_score: 8` / `score_floor: 7`, this meant real
Profile A outliers were almost never actually selected, despite being exactly
the trade this bot is nominally built for.

Fixed by:
- Scoping ALREADY-MOVED PENALTY to Profile B only (Profile A has its own
  SIZE RULE covering the same concept — stacking both punished the profile's
  defining case twice).
- Raising the SIZE RULE caps so a genuine, high-volume binary catalyst can
  reach 8 (20-30% already up), 7 (30-50%), or 5 (50%+) instead of 6/4/2 —
  while still capping vague/generic-news movers low, so this widens the door
  for real catalysts without turning into blind momentum chasing.
- Instructing GPT to set risk 4-5 on any such pick regardless of catalyst
  quality, so `trailing_stop_by_risk`'s tight 1.5-2.0% stops carry the
  downside risk of a large move reversing, instead of the only risk control
  being "don't buy it."
- Keeping `catalyst_type_weights["fda_approval"]` neutral (1.0) rather than
  discounted — the original -0.85x was drawn from only 3 historical picks,
  too small a sample to justify discounting exactly the catalyst type this
  change is trying to let through.

Run manually any time with `python scripts/analyze_scoring.py` (reads
`docs/data/portfolio.json` by default; pass one or more portfolio.json paths,
including old snapshots pulled from git history via `git show <ref>:docs/data/portfolio.json`,
to analyze a longer history than what's in the live file).

### Backtest harness

`scripts/backtest_prompt.py` replays logged LLM inputs
(`logs/llm_inputs/{date}/{ticker}_input.json`) through whatever
`catalyst_prompt.txt` / `picker_config.json` is currently checked out, and
compares the new score/direction against the original logged decision and
the realized `day_return_pct`. This validates a prompt/config change against
real historical inputs before it ships live.

```
python scripts/backtest_prompt.py --start 2026-07-01 --end 2026-07-31 --dry-run   # plumbing check, no API key
python scripts/backtest_prompt.py --start 2026-07-01 --end 2026-07-31             # real replay, needs OPENAI_API_KEY
```

Known limitation: `spy_context` / `volume_context` aren't logged as separate
fields (only baked into the original prompt text), so replays use fixed
placeholders for both — score deltas from SPY-down-day or volume-modifier
logic won't replay accurately until `log_llm_input()` stores those two
fields directly.

---

## Configuration

See `src/stock_bot/config/picker_config.json`:

```json
{
  "aggressive_mode": true,
  "aggressive_min_score": 6,
  "num_stocks": 10,
  "min_score": 5,
  "score_floor": 4,
  "pre_score_trend_filters": {
    "weekly": {"min": 0.0},
    "monthly": {"min": -5.0}
  },
  "scanner": {
    "market_cap_max_b": 500,
    "price_min": 5.0,
    "volume_min": 500000
  },
  "take_profit_pct": 8.0,
  "stop_loss_pct": 3.0
}
```
