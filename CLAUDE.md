# CLAUDE.md — stock_bot

## Project Overview

**"Inf Money"** — a fully automated stock trading engine powered by Interactive Brokers (IBKR) and AI agents. The system identifies high-potential stocks via AI-driven analysis, executes trades via IBKR API, and maintains a public dashboard updated through GitHub Actions. Trading focus: aggressive day trading targeting short-term explosive moves (5%+ intraday gains from recent catalysts).

## Tech Stack

- **Python 3.10+** with src layout (`src/stock_bot/`)
- **ib_insync** — Interactive Brokers API integration (optional dependency)
- **pandas** — data manipulation
- **python-dotenv** — environment configuration via `.env`
- **OpenAI API** — AI-driven stock selection
- **pytest** — testing | **black** — formatting | **ruff** — linting | **mypy** — type checking

## Project Structure

```
src/stock_bot/
├── main.py                         # Entry point
├── brokers/ib/connect_disconnect.py  # IBKR connection management
├── config/settings.py              # Settings dataclasses (IBSettings, LoggingSettings)
├── core/logging_config.py          # Centralized logging setup
├── data_sources/get_list_all_stocks.py  # NASDAQ stock fetcher
├── ai/                             # AI agent modules (in progress)
├── strategies/                     # Trading strategies (in progress)
└── templates/
    ├── picker_prompt.txt           # AI system prompt for stock selection
    └── JSON_Template.json          # Output schema for trading positions
```

## Commands

```bash
# Install
pip install -e ".[dev,ib]"

# Run
python -m stock_bot.main
# or
stock-bot

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
| `OPENAI_API_KEY` | — | AI stock selection |
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

## Key Conventions

- **Settings**: Environment-based dataclasses in `config/settings.py`. Module-level instances `ib_settings` and `logging_settings` are imported directly — do not re-instantiate.
- **Logging**: Call `setup_logging()` once at startup. Uses rotating file handler (5MB, 5 backups) + console handler. Import the logger per module: `logger = logging.getLogger(__name__)`.
- **Broker layer**: IBKR uses a singleton-like global `_ib` instance in `connect_disconnect.py`. Always check connection state before operations.
- **Data sources**: Return pandas DataFrames. Use defensive error handling with try/except and logging.
- **AI/templates**: `picker_prompt.txt` uses `{excluded_tickers}` placeholder. Output conforms to `JSON_Template.json` schema.
- **Startup sequence**: logging setup → config log → fetch stocks → connect IBKR → verify → run strategies → disconnect.

## Current Status

Early-stage / Beta. Core infrastructure is in place (logging, settings, IBKR connection, NASDAQ data source). Trading strategies and AI integration are not yet implemented (marked with `TODO` comments in `main.py`).
