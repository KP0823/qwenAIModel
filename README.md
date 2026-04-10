# Autonomous AI Macro-Strategy Agent

A fully autonomous hourly trading agent that manages an Alpaca Paper Trading account ($100k paper, transitioning to $50 live). It synthesizes macro news from multiple sources (RSS, Alpha Vantage, NewsAPI, Reddit), ETF technical indicators, and sentiment data, then uses a locally hosted Qwen 3.5 9B LLM to make structured multi-action trade decisions across Sector and Active ETFs. A Streamlit dashboard provides full observability into the AI's reasoning.

---

## Overview

Instead of chasing individual stocks based on social media hype, this agent acts as a **Quantitative Macro-Analyst**. Every hour during market hours it:

1. Pulls live technical data (price, RSI, 200-day MA) for 10 ETFs (6 sector + 4 active macro)
2. Scrapes macro headlines from Bloomberg, Yahoo Finance, and CNBC via RSS
3. Fetches ticker-specific news sentiment from Alpha Vantage
4. Pulls top business headlines from NewsAPI
5. Reads sentiment from financial subreddits via Reddit API
6. Feeds all of that into a locally hosted Qwen 3.5 9B model
7. Parses the AI's structured JSON array of 1-3 trade actions (`BUY` / `SELL` / `HOLD`)
8. Executes each order via Alpaca Paper Trading with automatic trailing stop-losses

All reasoning, decisions, and portfolio history are logged and visualized in a live Streamlit dashboard.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Qwen 3.5 9B (Q4_K_M) via Ollama — runs fully locally |
| Backend | Python 3.9+ |
| Frontend | Streamlit |
| Brokerage | Alpaca Paper Trading (`alpaca-py`) |
| Market Data | yfinance |
| News (RSS) | feedparser (Bloomberg, Yahoo Finance, CNBC) |
| News (API) | Alpha Vantage NEWS_SENTIMENT, NewsAPI top headlines |
| Sentiment | PRAW (Reddit API) |
| Hardware | M4 Mac Mini (16GB) — model fits in ~6GB RAM |

---

## Architecture

```
main.py  (launchd entry point — hourly Mon-Fri 10-15 ET)
  ├── is_market_hours()          → warns if outside market hours
  ├── ensure_ollama_running()    → restarts Ollama if crashed
  ├── sensors.py   →  data/system_state.json
  │     ├── yfinance (10 ETFs: RSI, 200MA, price)
  │     ├── RSS feeds (Bloomberg, Yahoo, CNBC)
  │     ├── Alpha Vantage NEWS_SENTIMENT (ticker-specific)
  │     ├── NewsAPI top headlines (business)
  │     └── Reddit PRAW (pending approval)
  └── agent.py
        ├── broker.process_pending_stops()  → attach stops from prior run
        ├── reads:  system_state.json, macro_trends.md, trade_journal.json
        ├── calls:  Ollama localhost:11434  (StockAI:latest)
        ├── parses: JSON array of 1-3 actions (batch execution)
        ├── calls:  broker.py  →  Alpaca paper-api
        └── writes: trade_journal.json, portfolio_history.csv

dashboard.py  (separate Streamlit process — read only)
```

### 4-Part System

**1. Market Sensors** — `sensors.py`
Runs hourly to collect unbiased market data from 5 sources. Fetches ETF technicals via yfinance, macro headlines via RSS, ticker-specific sentiment via Alpha Vantage, business headlines via NewsAPI, and community sentiment via Reddit PRAW. All headlines are deduplicated via MD5 hashing (7-day TTL) and age-filtered (3-day max). Writes everything atomically to `data/system_state.json`.

**2. Memory Hub** — `data/`
Gives the stateless LLM long-term context across sessions:
- `system_state.json` — live sensor snapshot (all 5 data sources)
- `macro_trends.md` — AI-written weekly macro outlook (regenerated every Monday)
- `trade_journal.json` — full history of every decision including internal reasoning and batch grouping
- `portfolio_history.csv` — intraday equity snapshots for charting
- `seen_headlines.json` — MD5 dedup hashes to prevent re-feeding headlines

**3. Decision Engine** — `agent.py`
Builds a rich context prompt from sensor data and memory, calls the local Qwen model, and parses the forced Chain-of-Thought (`<think>`) + structured JSON array response. The parser accepts both JSON arrays (1-3 actions) and single JSON objects (backward compatible). A single HOLD in any batch cancels all actions. Applies safety guardrails before routing to the execution layer.

**4. Execution & Guardrails** — `broker.py` + `agent.py`
Executes each action in the batch sequentially via Alpaca, protected by three safety nets: a circuit breaker, a PDT compliance proxy (accounts for batch size), and automatic trailing stop-losses on every BUY. BUY amounts exceeding available cash are proportionally scaled down across the batch.

---

## Features

- **Multi-action decisions** — model outputs 1-3 trade actions per run as a JSON array; HOLD cancels entire batch
- **5 data sources** — RSS, Alpha Vantage, NewsAPI, Reddit, yfinance — all zero-cost
- **Hourly execution** — 6 runs per day during market hours (10 AM - 3 PM ET, Mon-Fri)
- **Forced Chain-of-Thought** — model outputs a `<think>` internal monologue debating risk/reward before every decision
- **Structured JSON output** — strict schema prevents hallucinations from crashing execution
- **Fractional shares** — notional (dollar-based) orders let the agent trade with any amount
- **Automatic trailing stops** — every BUY order gets a 10% trailing stop-loss attached after fill
- **Circuit breaker** — halts all trading if the portfolio drops >5% in a single day
- **PDT compliance proxy** — enforces swing-trading discipline (max 6 non-HOLD actions per 5-day rolling window)
- **Headline deduplication** — MD5 hashing with 7-day TTL ensures news is never re-fed to the model
- **Ollama auto-restart** — detects and restarts Ollama if it crashes between runs
- **Weekly macro outlook** — AI synthesizes a fresh 3-paragraph markdown outlook every Monday
- **Batch grouping** — multi-action decisions are linked by `batch_id` in the trade journal
- **Full observability** — Streamlit dashboard exposes every trade decision, reasoning, and equity curve
- **Pending stops** — trailing stops that can't attach (market closed) are saved and retried next run

---

## Setup

**Quick start:**
```bash
git clone <repo-url>
cd qwenAIModel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Fill in .env with your API keys (see below)
python3 main.py
```

### Environment Variables (`.env`)

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPHA_VANTAGE_API_KEY=...     # free tier: 25 req/day — https://www.alphavantage.co/support/#api-key
NEWS_API_KEY=...              # free tier: 100 req/day — https://newsapi.org/register
REDDIT_CLIENT_ID=...          # optional, pending Reddit API approval
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=MacroAgent/1.0 by YourUsername
```

### ModelFile Setup

The model configuration is not committed to this repo since it contains a local file path. A template is provided:

```bash
cp ModelFile.example ModelFile
```

Then open `ModelFile` and update the `FROM` line to point to your local GGUF model:

```
FROM /path/to/your/models/Qwen3.5-9B-Q4_K_M.gguf
```

Common locations:
- **LM Studio (macOS):** `~/.lmstudio/models/lmstudio-community/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf`
- **Ollama default (macOS):** `~/.ollama/models/blobs/<model-blob>`
- **Linux:** `~/.ollama/models/...`

Once updated, register the model with Ollama:

```bash
ollama create StockAI -f ModelFile
```

Verify it's loaded:

```bash
ollama list
# Should show: StockAI:latest
```

---

## Usage

```bash
# Run the full pipeline once (sensors → AI decision → Alpaca order)
python3 main.py

# Watch live logs
tail -f logs/agent.log

# Launch the dashboard (separate terminal)
streamlit run dashboard.py

# Reset account (cancel orders, close positions, wipe local data)
python3 reset_account.py
```

### Automated Scheduling (Hourly, Mon–Fri, 10-15 ET)

The agent runs via **macOS launchd** rather than cron. launchd is the macOS-native scheduler and supports `WakeForNetworkAccess`, which wakes the machine from sleep to ensure the job always fires — something cron cannot do.

Create a plist at `~/Library/LaunchAgents/com.yourname.macroagent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yourname.macroagent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/venv/bin/python</string>
        <string>/path/to/qwenAIModel/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/qwenAIModel</string>
    <key>StandardOutPath</key>
    <string>/path/to/qwenAIModel/logs/cron.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/qwenAIModel/logs/cron.log</string>
    <key>StartCalendarInterval</key>
    <array>
        <!-- Repeat for each weekday (1=Mon..5=Fri) x each hour (10-15) -->
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
        <!-- ... 10, 11, 12, 13, 14, 15 for each weekday ... -->
    </array>
    <key>WakeForNetworkAccess</key>
    <true/>
</dict>
</plist>
```

Then register it:

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.macroagent.plist
```

---

## Dashboard

The Streamlit dashboard (`localhost:8501`) has three tabs:

| Tab | Contents |
|-----|---------|
| **Portfolio** | Equity line chart, live ETF allocation donut, Cash vs ETF Holdings donut, positions table with cost basis/P&L |
| **Brain Feed** | Timeline of every trade — expandable cards showing the AI's full `<think>` reasoning, batch grouping for multi-action decisions |
| **Market Sensors** | ETF technical table, API health indicators, 2x2 news grid (RSS, Alpha Vantage, NewsAPI, Reddit) |

Auto-refreshes every 60 seconds.

---

## Safety & Risk Management

| Guardrail | Behavior |
|-----------|---------|
| **Circuit Breaker** | Halts all trading if portfolio drops ≥5% in one day |
| **PDT Proxy** | Forces HOLD if ≥6 non-HOLD trades executed in the last 5 rolling days |
| **Amount Cap** | Total BUY amounts across batch capped to 95% of available cash (proportionally scaled) |
| **Trailing Stop** | 10% trailing stop-loss automatically attached to every filled BUY order |
| **Pending Stops** | Stops that can't attach (market closed) are saved to `pending_stops.json` and retried next run |
| **JSON Validation** | Decisions failing schema validation default to HOLD — never crash to an unintended trade |
| **HOLD Cancels Batch** | A single HOLD action in any multi-action response cancels all other actions |

---

## Project Structure

```
qwenAIModel/
├── ModelFile.example       # Ollama model config template — copy to ModelFile and update the FROM path
├── config.py               # Central constants hub (all modules import from here)
├── sensors.py              # Data pipeline: yfinance, RSS, Alpha Vantage, NewsAPI, Reddit
├── broker.py               # Alpaca paper trading: orders, trailing stops, portfolio history
├── agent.py                # AI brain: prompt builder, Ollama caller, multi-action parser, safety gates
├── main.py                 # Entry point: market hours check, Ollama health, sensors → agent
├── dashboard.py            # Streamlit observability dashboard (3 tabs)
├── reset_account.py        # Utility: cancel orders, close positions, wipe local data
├── requirements.txt        # Python dependencies
├── .env                    # API secrets (gitignored)
└── data/                   # Auto-created by main.py (gitignored)
    ├── system_state.json   # Live sensor data from all 5 sources
    ├── trade_journal.json  # Full trade history with <think> reasoning
    ├── portfolio_history.csv # Intraday equity snapshots
    ├── macro_trends.md     # AI-written weekly macro outlook
    ├── seen_headlines.json # MD5 dedup hashes (7-day TTL)
    └── pending_stops.json  # BUY orders awaiting trailing stop attachment
```

---

## Target ETFs (10 total)

**Passive Sector ETFs (sector rotation core):**

| Ticker | Sector |
|--------|--------|
| XLK | Technology |
| XLE | Energy |
| XLU | Utilities |
| XLF | Financials |
| XLV | Healthcare |
| XLI | Industrials |

**Active SPDR ETFs (macro depth):**

| Ticker | Focus |
|--------|-------|
| GAL | Global Allocation |
| TOTL | Total Return Bonds |
| SRLN | Senior Loans (floating rate) |
| RLY | Real Return / Inflation Hedge |

---

## License

MIT
