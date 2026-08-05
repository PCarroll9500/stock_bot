# CLAUDE.md — stock_bot

## Project Overview

**"Inf Money"** — a fully automated day-trading engine powered by Interactive Brokers (IBKR) and GPT-based catalyst scoring. Runs once daily via EC2 cron: scans premarket for gappers/momentum, scores candidates with an LLM against news + earnings + social sentiment + SEC filings, buys the top picks at the open, and liquidates everything (except explicit multi-day holds) ~90 minutes before close. A public dashboard (GitHub Pages, `docs/`) is updated by committing `docs/data/portfolio.json` after each run — there is no GitHub Actions workflow; the EC2 box pushes directly. See **`ARCHITECTURE.md`** for the full pipeline diagram, data flow, and the score-vs-returns feedback loop — read that first for anything beyond a quick orientation.

Trading focus: aggressive day trading targeting short-term explosive moves (5%+ intraday gains from recent catalysts).

## Tech Stack

- **Python 3.10+** with src layout (`src/stock_bot/`)
- **ib_insync** — Interactive Brokers API integration
- **pandas / beautifulsoup4 / requests** — data fetching and scraping (earnings, sentiment, SEC filings, web news)
- **python-dotenv** — environment configuration via `.env`
- **OpenAI API** — GPT-based catalyst scoring (`ai/catalyst_scorer.py`)
- **boto3** — SNS email reports (morning picks, close-of-day, weekly scoring digest)
- **pytest** — testing | **black** — formatting | **ruff** — linting | **mypy** — type checking

## Project Structure

```
src/stock_bot/
├── main.py                              # Premarket entry point — full pipeline (see ARCHITECTURE.md)
├── ai/catalyst_scorer.py                # GPT scoring: score_candidates(), filter_and_rank()
├── brokers/ib/
│   ├── connect_disconnect.py            # IBKR connection management
│   ├── buy_stocks.py / sell_stocks.py / sell_all.py
├── config/
│   ├── settings.py                      # IBSettings/LoggingSettings dataclasses + finnhub_api_key
│   └── picker_config.json               # Runtime tuning: filters, scanner, catalyst_type_weights,
│                                         # risk_penalty, trailing_stop_by_risk, etc. (no code change needed)
├── core/
│   ├── logging_config.py                # Centralized logging setup
│   └── llm_input_logger.py              # Logs every GPT input/output to logs/llm_inputs/{date}/
├── data_sources/                        # scanner, news_fetcher, trend_checker, earnings/sentiment/sec/web
│   └── portfolio_writer.py              # Writes docs/data/portfolio.json (the dashboard's data source)
├── strategies/                          # Currently empty — no separate strategy abstraction; the picking
│                                         # logic lives directly in main.py + ai/catalyst_scorer.py
└── templates/
    ├── catalyst_prompt.txt              # Active GPT scoring prompt (used by catalyst_scorer.py)
    └── picker_prompt.txt                # Legacy/unused — superseded by catalyst_prompt.txt

scripts/                                 # Cron entry points + operational tools
├── run_morning.sh / run_close.sh / run_weekly_report.sh   # cron wrappers (git pull → run → push → email)
├── close_of_day.py                      # 2:30 PM ET liquidation + per-pick P&L
├── analyze_scoring.py                   # Score-vs-returns feedback loop (see ARCHITECTURE.md)
├── backtest_prompt.py                   # Replay logged LLM inputs through the current prompt/config
└── email_*.py                           # SNS report senders
```

## Commands

```bash
# Install
pip install -e ".[dev,ib]"

# Run the full premarket pipeline (writes docs/data/portfolio_test.json in --test mode)
python -m stock_bot.main [--test] [--sequential]

# Close-of-day liquidation
python scripts/close_of_day.py [--test]

# Score-vs-returns feedback loop (reads docs/data/portfolio.json by default)
python scripts/analyze_scoring.py [portfolio.json ...] [--output PATH] [--quiet]

# Backtest a prompt/config change against historical LLM inputs
python scripts/backtest_prompt.py --start 2026-07-01 --end 2026-07-31 --dry-run   # no API key needed
python scripts/backtest_prompt.py --start 2026-07-01 --end 2026-07-31             # real replay

# Format / lint / type check / test
black src/
ruff check src/
mypy src/
pytest
```

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | GPT catalyst scoring |
| `FINNHUB_API_KEY` | — | Optional, used by `news_fetcher.py` |
| `IB_USERNAME` / `IB_PASSWORD` | — | IBKR credentials |
| `IB_ACCOUNT_LIVE` / `IB_ACCOUNT_PAPER` | — | Account IDs |
| `IB_HOST` | `127.0.0.1` | TWS/Gateway host |
| `IB_PORT_PAPER` | `4002` | Paper trading port |
| `IB_PORT_LIVE` | `4001` | Live trading port |
| `IB_MODE` | `paper` | `paper` or `live` |
| `IB_CLIENT_ID` | `1` | Client ID |
| `IB_EXCHANGE` | `SMART` | Order routing |
| `IB_CURRENCY` | `USD` | Currency |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_FILE` | `logs/stock_bot.log` | Log output path |
| `GITHUB_PAT` / `GITHUB_USER` | — | Used only by `scripts/run_*.sh` on the EC2 box, to push `docs/data/*.json` after each run |

AWS credentials for SNS email reports (`scripts/email_*.py`) come from the EC2 instance role, not `.env`.

## Key Conventions

- **Settings**: Environment-based dataclasses in `config/settings.py`. Module-level instances `ib_settings` and `logging_settings` are imported directly — do not re-instantiate.
- **Runtime tuning lives in `picker_config.json`, not code.** Filters, scanner codes, thresholds, `catalyst_type_weights`, `risk_penalty`, `trailing_stop_by_risk` are all config-driven so behavior can be tuned from `scripts/analyze_scoring.py` findings without a deploy.
- **Logging**: Call `setup_logging()` once at startup. Uses rotating file handler (5MB, 5 backups) + console handler. Import the logger per module: `logger = logging.getLogger(__name__)`.
- **Broker layer**: IBKR uses a singleton-like global `_ib` instance in `connect_disconnect.py`. Always check connection state before operations. Cash-only account — shorts must never exist; `main.py` aborts the run if one is detected.
- **Data sources**: Return pandas DataFrames. Use defensive error handling with try/except and logging — scrapers (earnings/sentiment/SEC/web news) must fail open (return an empty, correctly-shaped DataFrame) rather than raise.
- **AI scoring**: `catalyst_prompt.txt` is the active prompt (not `picker_prompt.txt`, which is legacy). GPT returns `score`, `direction`, `risk`, `expected_gain_pct`, `reason`, `sector`, `catalyst_type` — all persisted to `docs/data/portfolio.json` for `scripts/analyze_scoring.py` to break down later.
- **Portfolio writes**: `docs/data/portfolio.json` is the single source of truth for the public dashboard. Written atomically (tmp → rename). `portfolio_test.json` is the `--test` mode equivalent — never touches real trading data.
- **Startup sequence** (main.py): logging setup → load config → connect IBKR → scan → filter → fetch news + enhanced data → score with GPT → pick + allocate → place premarket orders → write portfolio session → disconnect.

## Current Status

The core pipeline (scan → filter → news → GPT scoring → pick → allocate → order → record) is fully built and running live in paper mode via EC2 cron — this is well past early-stage. `strategies/` is currently unused (the picking logic lives directly in `main.py` + `ai/catalyst_scorer.py` rather than a separate strategy abstraction). The score-vs-returns feedback loop (`scripts/analyze_scoring.py`, `scripts/backtest_prompt.py`) is the active area of iteration — see `ARCHITECTURE.md` for what it has found so far and how those findings map to `picker_config.json`.
