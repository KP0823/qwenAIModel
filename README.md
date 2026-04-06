# Autonomous AI Macro-Strategy Agent

A fully autonomous daily trading agent that manages a $50 Alpaca Paper Trading account. It synthesizes macroeconomic news, Reddit sentiment, and ETF technical indicators, then uses a locally hosted LLM to make structured trade decisions across Sector ETFs. A Streamlit dashboard provides full observability into the AI's reasoning.

---

## Overview

Instead of chasing individual stocks based on social media hype, this agent acts as a **Quantitative Macro-Analyst**. Every morning it:

1. Pulls live technical data (price, RSI, 200-day MA) for 6 Sector ETFs
2. Scrapes macro headlines from Reuters and CNBC via RSS
3. Reads sentiment from financial subreddits via Reddit API
4. Feeds all of that into a locally hosted Qwen 3.5 9B model
5. Parses the AI's structured JSON decision (`BUY` / `SELL` / `HOLD`)
6. Executes the order via Alpaca Paper Trading with an automatic trailing stop-loss

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
| News | feedparser (Reuters, CNBC RSS) |
| Sentiment | PRAW (Reddit API) |
| Hardware | M4 Mac Mini (16GB) — model fits in ~6GB RAM |

---

## Architecture

```
main.py  (daily cron entry point)
  ├── sensors.py   →  data/system_state.json
  └── agent.py
        ├── reads:  system_state.json, macro_trends.md, trade_journal.json
        ├── calls:  Ollama localhost:11434  (StockAI:latest)
        ├── calls:  broker.py  →  Alpaca paper-api
        └── writes: trade_journal.json, portfolio_history.csv

dashboard.py  (separate Streamlit process — read only)
```

### 4-Part System

**1. Market Sensors** — `sensors.py`
Runs daily to collect unbiased market data without expensive subscriptions. Fetches ETF technicals via yfinance, macro headlines via RSS, and community sentiment via Reddit PRAW. Writes everything atomically to `data/system_state.json`.

**2. Memory Hub** — `data/`
Gives the stateless LLM long-term context across sessions:
- `system_state.json` — live sensor snapshot
- `macro_trends.md` — AI-written weekly macro outlook (regenerated every Monday)
- `trade_journal.json` — full history of every decision including internal reasoning
- `portfolio_history.csv` — daily equity snapshots for charting

**3. Decision Engine** — `agent.py`
Builds a rich context prompt from sensor data and memory, calls the local Qwen model, and parses the forced Chain-of-Thought (`<think>`) + structured JSON response. Applies safety guardrails before routing to the execution layer.

**4. Execution & Guardrails** — `broker.py` + `agent.py`
Parses the AI's JSON and routes it to Alpaca, protected by three hard-coded safety nets: a circuit breaker, a PDT compliance proxy, and automatic trailing stop-losses on every BUY.

---

## Features

- **Forced Chain-of-Thought** — model outputs a `<think>` internal monologue debating risk/reward before every decision
- **Structured JSON output** — strict schema prevents hallucinations from crashing execution
- **Fractional shares** — notional (dollar-based) orders let the agent trade with any amount
- **Automatic trailing stops** — every BUY order gets a 10% trailing stop-loss attached after fill
- **Circuit breaker** — halts all trading if the portfolio drops >5% in a single day
- **PDT compliance proxy** — enforces swing-trading discipline (max 3 non-HOLD actions per 5-day rolling window)
- **Weekly macro outlook** — AI synthesizes a fresh 3-paragraph markdown outlook every Monday
- **Full observability** — Streamlit dashboard exposes every trade decision, reasoning, and equity curve
- **Zero-cost data** — no paid data subscriptions; yfinance, RSS, and PRAW are all free

---

## Setup

See **[SETUP.md](SETUP.md)** for the full step-by-step onboarding guide including API key registration for Alpaca and Reddit.

**Quick start:**
```bash
git clone <repo-url>
cd qwenAIModel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Fill in .env with your API keys
python3 main.py
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
```

### Automated Scheduling (9:00 AM, Mon–Fri)

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
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
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
| **Portfolio** | Line chart of equity over time, live ETF allocation donut chart, P&L vs $50 starting balance |
| **Brain Feed** | Timeline of every trade — expandable cards showing the AI's full `<think>` reasoning and JSON decision |
| **Market Sensors** | ETF technical table (price, RSI, 200MA, signal), API health indicators, RSS headlines, Reddit pulse |

Auto-refreshes every 60 seconds.

---

## Safety & Risk Management

| Guardrail | Behavior |
|-----------|---------|
| **Circuit Breaker** | Halts all trading if portfolio drops ≥5% in one day |
| **PDT Proxy** | Forces HOLD if ≥3 non-HOLD trades executed in the last 5 rolling days |
| **Amount Cap** | BUY orders capped to 95% of available cash |
| **Trailing Stop** | 10% trailing stop-loss automatically attached to every filled BUY order |
| **JSON Validation** | Decisions failing schema validation default to HOLD — never crash to an unintended trade |

---

## Project Structure

```
qwenAIModel/
├── ModelFile.example       # Ollama model config template — copy to ModelFile and update the FROM path
├── config.py               # Central constants hub (all modules import from here)
├── sensors.py              # Data pipeline: yfinance, RSS, Reddit → system_state.json
├── broker.py               # Alpaca paper trading: orders, trailing stops, portfolio history
├── agent.py                # AI brain: prompt builder, Ollama caller, parser, safety gates
├── main.py                 # Entry point: initializes files, runs sensors → agent
├── dashboard.py            # Streamlit observability dashboard
├── requirements.txt        # Python dependencies
├── .env                    # API secrets (gitignored)
└── data/                   # Auto-created by main.py (gitignored)
    ├── system_state.json
    ├── trade_journal.json
    ├── portfolio_history.csv
    └── macro_trends.md
```

---

## Target ETFs

| Ticker | Sector |
|--------|--------|
| XLK | Technology |
| XLE | Energy |
| XLU | Utilities |
| XLF | Financials |
| XLV | Healthcare |
| XLI | Industrials |

---

## License

MIT
