import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import broker
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword-based News Triage Tables
# ---------------------------------------------------------------------------

_MACRO_KEYWORDS = frozenset([
    "fed", "federal reserve", "fomc", "interest rate", "rate cut", "rate hike",
    "inflation", "cpi", "pce", "deflation", "tariff", "trade war", "trade deal",
    "gdp", "recession", "treasury", "yield", "yield curve", "unemployment",
    "jobs report", "payroll", "nonfarm", "fiscal", "monetary policy", "powell",
    "debt ceiling", "deficit", "budget", "crude oil", "opec", "dollar index",
    "sanctions", "geopolit", "war", "ukraine", "china trade", "bank crisis",
])

_SECTOR_KEYWORDS: dict = {
    "XLK": ["nvidia", "apple", "microsoft", "google", "alphabet", "meta", "amazon",
             "semiconductor", "chip", "artificial intelligence", " ai ", "software",
             "cloud", "cybersecurity", "tech stock", "data center"],
    "XLF": ["bank", "jpmorgan", "goldman sachs", "wells fargo", "citigroup", "morgan stanley",
             "insurance", "fintech", "credit card", "berkshire", "lending", "loan default",
             "financial sector"],
    "XLE": ["exxon", "chevron", "shell", "bp ", "oil price", "natural gas", "refinery",
             "energy sector", "petroleum", "opec", "pipeline", "crude"],
    "XLV": ["healthcare", "pharmaceutical", "fda", "drug approval", "vaccine", "pfizer",
             "moderna", "unitedhealth", "johnson & johnson", "hospital", "biotech", "medicare"],
    "XLU": ["utility", "electric grid", "power generation", "renewable energy", "solar farm",
             "wind energy", "nuclear power"],
    "XLI": ["defense", "boeing", "lockheed", "caterpillar", "raytheon", "industrial",
             "aerospace", "manufacturing", "infrastructure bill"],
    "TOTL": ["bond market", "treasury bond", "fixed income", "yield curve", "credit rating",
              "corporate bond", "junk bond", "municipal bond", "bond fund"],
    "RLY": ["real asset", "commodity", "tips ", "reit", "real estate", "gold price",
             "silver price", "inflation hedge"],
    "SRLN": ["leveraged loan", "floating rate", "credit spread", "senior secured",
              "high yield loan", "clo "],
    "GAL": ["global market", "international", "emerging market", "china market",
             "europe stock", "asia pacific", "yen", "euro", "forex"],
}

_LARGE_CAP = frozenset([
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO",
    "JPM", "LLY", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ", "BAC",
    "WFC", "GS", "MS", "CVX", "ABBV", "KO", "PEP", "MRK", "TMO", "CRM", "ORCL",
    "AMD", "INTC", "QCOM", "TXN", "NFLX", "DIS", "BA", "CAT", "GE", "IBM",
    "UBER", "PYPL", "SHOP", "SNAP", "SPOT", "ZM", "PLTR", "COIN", "HOOD",
])


_BULL_SIGNALS = frozenset([
    # Standard financial
    "bullish", "buying", "long position", "going long", "strong buy", "upgrade",
    "beat earnings", "beat estimates", "beat expectations", "positive surprise",
    "optimistic", "rally", "breakout", "outperform", "record high", "all time high",
    "52-week high", "upside", "uptrend", "accumulate", "overweight", "buy the dip",
    "buying the dip", "soft landing", "rate cut", "easing", "stimulus",
    # Reddit-specific
    "calls ", "yolo", "to the moon", "moon ", "🚀", "tendies", "tendie",
    "diamond hands", "💎", "apes ", "squeeze", "short squeeze", "gamma squeeze",
    "printing", "calls printing", "green ", "all green", "green day", "ripping",
    "mooning", "loading up", "all in", "adding more", "averaging down",
    "leaps", "deep itm", "strong hands", "never selling", "send it",
])

_BEAR_SIGNALS = frozenset([
    # Standard financial
    "bearish", "selling", "short position", "going short", "downgrade",
    "missed earnings", "miss estimates", "below expectations", "negative surprise",
    "pessimistic", "correction", "overvalued", "bubble", "collapse", "downturn",
    "downtrend", "underperform", "underweight", "sell off", "selloff",
    "recession", "stagflation", "rate hike", "tightening", "layoffs",
    "default", "bankruptcy", "insolvency", "delisting", "guidance cut",
    # Reddit-specific
    "puts ", "🐻", "to zero", "going to zero", "rekt", "getting rekt",
    "blood", "bloodbath", "red ", "all red", "red day", "tanking", "tank ",
    "drilling", "drill ", "gap down", "death cross", "inverse etf",
    "paper hands", "dumping", "dump ", "cratering", "crater ",
    "this is the end", "markets are cooked", "sell everything",
    "rug pull", "rugging", "bagholder", "bag holder", "bag holding",
    "exit liquidity", "short this", "puts are printing", "buying puts",
])


def _reddit_sentiment_keywords(text: str) -> str:
    """Keyword-based fallback — instant but context-blind."""
    lower = text.lower()
    bull = sum(1 for kw in _BULL_SIGNALS if kw in lower)
    bear = sum(1 for kw in _BEAR_SIGNALS if kw in lower)
    if bull > bear:
        return "BULLISH"
    if bear > bull:
        return "BEARISH"
    return "NEUTRAL"


def _reddit_sentiment_batch(posts: list) -> dict:
    """
    Single Qwen call to classify sentiment for a small list of Reddit posts.
    posts: list of {"idx": int, "title": str, "body": str}
    Returns dict of idx → "BULLISH"|"BEARISH"|"NEUTRAL".
    Falls back to keyword scoring if the call fails or times out.
    """
    if not posts:
        return {}

    lines = []
    for p in posts:
        text = p["title"]
        if p.get("body"):
            text += " — " + p["body"][:200]
        lines.append(f'{p["idx"]}. r/{p.get("subreddit","?")} | {text[:300]}')

    prompt = f"""/no_think
Classify the market sentiment of each Reddit post. Return a JSON array:
[{{"idx": 1, "sentiment": "BULLISH"}}, ...]

Options: BULLISH (community expects market/stock to rise), BEARISH (expects decline), NEUTRAL (mixed or unclear)
Consider Reddit-specific language: calls/puts, tendies, moon/crater, diamond hands/paper hands, rekt, etc.

Posts:
{chr(10).join(lines)}

Return ONLY the JSON array. No explanation."""

    try:
        raw = call_ollama(prompt, timeout=config.TRIAGE_OLLAMA_TIMEOUT)
        clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        clean = re.sub(r"```(?:json)?\s*", "", clean).strip()
        array_str = _extract_json_array(clean)
        if not array_str:
            raise ValueError("no JSON array found")
        parsed = json.loads(array_str)
        result = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item["idx"])
                sentiment = item.get("sentiment", "NEUTRAL")
                if sentiment not in ("BULLISH", "BEARISH", "NEUTRAL"):
                    sentiment = "NEUTRAL"
                result[idx] = sentiment
            except (KeyError, TypeError, ValueError):
                continue
        logger.info(f"Reddit sentiment: Qwen classified {len(result)} posts")
        return result
    except Exception as e:
        logger.warning(f"Reddit sentiment Qwen call failed ({e}) — falling back to keywords")
        return {p["idx"]: _reddit_sentiment_keywords(p["title"] + " " + p.get("body", "")) for p in posts}


def _score_article(title: str, existing_tickers: list, body: str = "") -> tuple:
    """
    Returns (relevance_score: int, sector_etf: str|None, tickers: list).
    Keyword-based — instantaneous, no model call required.
    Uses body text when available (e.g. Reddit self-posts) for richer matching.
    """
    lower = " " + (title + " " + body).lower() + " "

    macro_hits = sum(1 for kw in _MACRO_KEYWORDS if kw in lower)

    best_etf, best_hits = None, 0
    for etf, keywords in _SECTOR_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in lower)
        if hits > best_hits:
            best_hits, best_etf = hits, etf

    # Extract uppercase ticker-like words from the title
    found = [w for w in re.findall(r'\b[A-Z]{2,5}\b', title) if w in _LARGE_CAP]
    tickers = list(dict.fromkeys(existing_tickers + found))  # merge, preserve order, dedupe

    # Score
    if macro_hits >= 2:
        score = min(10, 7 + macro_hits)
    elif macro_hits == 1:
        score = 7
    elif best_hits >= 1 or tickers:
        score = 6
    else:
        score = 2  # niche / irrelevant

    if tickers:
        score = min(10, score + 1)

    return score, best_etf, tickers


# ---------------------------------------------------------------------------
# Context / Prompt Builder
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def build_prompt(state: dict, portfolio: dict, enriched_news: list = None) -> str:
    etf_lines = []
    for symbol, data in state.get("etf_data", {}).items():
        price = f"${data['price']:.2f}" if data.get("price") else "N/A"
        rsi = f"{data['rsi']:.1f}" if data.get("rsi") is not None else "N/A"
        ma = f"${data['ma_200']:.2f}" if data.get("ma_200") else "N/A"
        ma50 = f"${data['ma_50']:.2f}" if data.get("ma_50") else "N/A"
        macd = data.get("macd", "N/A")
        ema_fast = data.get("ema_cross_fast", "N/A")
        signal = data.get("signal", "UNKNOWN")
        dc_high = f"${data['donchian_high']:.2f}" if data.get("donchian_high") else "N/A"
        dc_low = f"${data['donchian_low']:.2f}" if data.get("donchian_low") else "N/A"
        vol_str = f" | Vol: {data['volume_ratio']:.2f}x avg" if data.get("volume_ratio") else ""
        etf_lines.append(
            f"  {symbol}: {price} | RSI: {rsi} | EMA(8/21): {ema_fast} | MACD: {macd}"
            f" | 50MA: {ma50} | 200MA: {ma} | DC(20): {dc_low}–{dc_high}{vol_str} | Signal: {signal}"
        )

    # News section: enriched (post-triage) if available, else raw fallback
    if enriched_news:
        news_lines = []
        for art in enriched_news:
            etf_tag = art.get("sector_etf") or "MACRO"
            rel = art.get("relevance", 0)
            tickers_str = ", ".join(art.get("tickers", [])) if art.get("tickers") else "—"
            sentiment = art.get("sentiment")
            sentiment_tag = f" | {sentiment}" if sentiment else ""
            engagement = ""
            if art.get("subreddit"):
                engagement = f" | r/{art['subreddit']} ↑{art.get('reddit_score',0)} 💬{art.get('num_comments',0)}"
            news_lines.append(
                f"  [{etf_tag} | {rel}/10{sentiment_tag}] {art['summary']}\n"
                f"    Tickers: {tickers_str}{engagement}"
            )
        news_section = (
            f"ENRICHED NEWS ({len(enriched_news)} articles, relevance ≥ {config.TRIAGE_MIN_RELEVANCE}/10):\n"
            + (chr(10).join(news_lines) if news_lines else "  No relevant articles passed triage filter.")
        )
    else:
        # Fallback: raw headlines from all sources
        headlines = state.get("rss_headlines", [])[:15]
        headline_lines = []
        for h in headlines:
            published = f" ({h['published'][:16]})" if h.get("published") else ""
            headline_lines.append(f"  - [{h['source']}]{published} {h['title']}")

        av_news = state.get("alphavantage_news", [])[:10]
        av_lines = []
        for a in av_news:
            tickers_str = ", ".join(a.get("tickers", [])) if a.get("tickers") else "general"
            av_lines.append(f"  - [{a['source']}] \"{a['title']}\" (sentiment: {a.get('sentiment', 'N/A')}, tickers: {tickers_str})")

        newsapi = state.get("newsapi_headlines", [])[:10]
        newsapi_lines = []
        for n in newsapi:
            pub = f" ({n['published'][:16]})" if n.get("published") else ""
            newsapi_lines.append(f"  - [{n['source']}]{pub} {n['title']}")

        alpaca_news = state.get("alpaca_news", [])[:10]
        alpaca_lines = []
        for a in alpaca_news:
            tickers_str = ", ".join(a.get("tickers", [])) if a.get("tickers") else ""
            ticker_tag = f" ({tickers_str})" if tickers_str else ""
            alpaca_lines.append(f"  - [{a['source']}]{ticker_tag} {a['title']}")

        reddit_posts = sorted(state.get("reddit_posts", []), key=lambda x: x.get("score", 0), reverse=True)[:10]
        reddit_lines = [f"  - r/{p['subreddit']}: \"{p['title']}\" (score: {p['score']})" for p in reddit_posts]

        news_section = f"""MACRO HEADLINES (RSS):
{chr(10).join(headline_lines) if headline_lines else "  No headlines available"}

ALPHA VANTAGE NEWS SENTIMENT:
{chr(10).join(av_lines) if av_lines else "  No Alpha Vantage data available"}

NEWSAPI HEADLINES:
{chr(10).join(newsapi_lines) if newsapi_lines else "  No NewsAPI data available"}

ALPACA NEWS:
{chr(10).join(alpaca_lines) if alpaca_lines else "  No Alpaca news available"}

REDDIT PULSE:
{chr(10).join(reddit_lines) if reddit_lines else "  No Reddit data available"}"""

    positions = portfolio.get("positions", {})
    pos_lines = [f"    {sym}: {d['qty']:.4f} shares @ ${d['current_price']:.2f} = ${d['market_value']:.2f}"
                 for sym, d in positions.items()]
    pos_str = "\n".join(pos_lines) if pos_lines else "    No open positions (all cash)"

    journal = _load_json(config.TRADE_JOURNAL_FILE, [])
    recent = journal[-5:] if len(journal) >= 5 else journal
    journal_lines = []
    for entry in reversed(recent):
        ts = entry.get("timestamp", "")[:10]
        journal_lines.append(
            f"  {ts} {entry.get('action','?')} {entry.get('ticker','?')} ${entry.get('amount_usd', 0):.2f}"
            f" — \"{entry.get('reasoning', '')}\""
        )

    macro = ""
    if Path(config.MACRO_TRENDS_FILE).exists():
        with open(config.MACRO_TRENDS_FILE) as f:
            macro = f.read().strip()

    sync_time = state.get("last_sync", "unknown")

    prompt = f"""=== MARKET DATA (as of {sync_time}) ===
ETF TECHNICAL SNAPSHOT:
{chr(10).join(etf_lines)}

{news_section}

=== PORTFOLIO CONTEXT ===
Total Value: ${portfolio['total_value']:.2f} | Cash: ${portfolio['cash']:.2f}
Open Positions:
{pos_str}

=== PRIOR REASONING (last 5 trades) ===
{chr(10).join(journal_lines) if journal_lines else "  No prior trades recorded"}

=== WEEKLY MACRO OUTLOOK ===
{macro if macro else "No macro outlook yet — this may be the first run."}

=== DECISION REQUIRED ===
Based on the above data, output your trade actions as a JSON array (1-3 actions).
You may BUY/SELL multiple ETFs in one response. Output HOLD alone if no trade is justified.
Remember: it is disciplined and correct to output HOLD if conditions do not justify a trade."""

    return prompt


# ---------------------------------------------------------------------------
# Ollama API
# ---------------------------------------------------------------------------

def call_ollama(prompt: str, timeout: int = None) -> str:
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    t = timeout if timeout is not None else config.OLLAMA_TIMEOUT
    response = requests.post(config.OLLAMA_GENERATE_URL, json=payload, timeout=t)
    response.raise_for_status()
    return response.json()["response"]


# ---------------------------------------------------------------------------
# Response Parser
# ---------------------------------------------------------------------------

def _validate_decision(decision: dict) -> bool:
    """Validate a single decision dict. Returns True if valid."""
    required = {"action", "ticker", "amount_usd", "reasoning"}
    if not required.issubset(decision.keys()):
        logger.error(f"JSON missing fields: {required - decision.keys()}")
        return False
    if decision["action"] not in ("BUY", "SELL", "HOLD"):
        logger.error(f"Invalid action: {decision['action']}")
        return False
    try:
        decision["amount_usd"] = float(decision["amount_usd"])
    except (TypeError, ValueError):
        logger.error(f"Invalid amount_usd: {decision['amount_usd']}")
        return False
    if not (0 <= decision["amount_usd"] <= config.MAX_PORTFOLIO_USD):
        logger.warning(f"amount_usd {decision['amount_usd']} out of bounds — clamping")
        decision["amount_usd"] = max(0.0, min(decision["amount_usd"], config.MAX_PORTFOLIO_USD))
    return True


def parse_response(raw: str) -> tuple:
    """
    Extract (<think> block, list of decision dicts) from raw Ollama output.
    Supports both JSON array and single JSON object (wrapped in list).
    Returns (think_text, None) if JSON validation fails.
    """
    # Extract think block
    think_match = re.search(r"<think>(.*?)</think>", raw, re.DOTALL)
    think_text = think_match.group(1).strip() if think_match else ""

    # Try JSON array first (multi-action)
    array_match = re.search(r"\[[\s\S]*?\]", raw)
    if array_match:
        try:
            parsed = json.loads(array_match.group(0))
            if isinstance(parsed, list) and parsed and all(isinstance(d, dict) and "action" in d for d in parsed):
                decisions = parsed[:3]  # cap at 3
                valid = all(_validate_decision(d) for d in decisions)
                if valid:
                    # If any action is HOLD, cancel all — return HOLD only
                    if any(d["action"] == "HOLD" for d in decisions):
                        hold = decisions[0] if decisions[0]["action"] == "HOLD" else {
                            "action": "HOLD", "ticker": "NONE", "amount_usd": 0.0,
                            "reasoning": "HOLD in batch cancels all actions"}
                        return think_text, [hold]
                    return think_text, decisions
        except (json.JSONDecodeError, TypeError):
            pass  # fall through to single-object extraction

    # Fallback: single JSON object containing "action"
    json_match = re.search(r"\{[^{}]*\"action\"[^{}]*\}", raw, re.DOTALL)
    if not json_match:
        logger.error(f"No JSON found in response. Raw (first 500 chars): {raw[:500]}")
        return think_text, None

    try:
        decision = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed: {e}. Raw match: {json_match.group(0)}")
        return think_text, None

    if not _validate_decision(decision):
        return think_text, None

    return think_text, [decision]


# ---------------------------------------------------------------------------
# Safety Guardrails
# ---------------------------------------------------------------------------

def _check_signal_agreement(decision: dict, state: dict) -> bool:
    """
    Returns True if the LLM's action agrees with the ETF's technical signal.
    BUY is blocked when OVERBOUGHT or MACD is BEARISH.
    SELL is blocked when OVERSOLD (don't sell into a potential bottom).
    """
    ticker = decision.get("ticker", "")
    action = decision.get("action", "")
    etf_data = state.get("etf_data", {}).get(ticker, {})
    if not etf_data:
        return True  # no data — allow through
    signal = etf_data.get("signal", "")
    macd = etf_data.get("macd", "")
    if action == "BUY" and ("OVERBOUGHT" in signal or macd == "BEARISH"):
        logger.warning(f"Signal filter: BUY {ticker} blocked — signal={signal}, macd={macd}")
        return False
    if action == "SELL" and "OVERSOLD" in signal:
        logger.warning(f"Signal filter: SELL {ticker} blocked — signal={signal} (oversold, possible bottom)")
        return False
    return True


def check_safety_gates(decisions: list, portfolio: dict, state: dict = None) -> tuple:
    """
    Accepts a list of decision dicts. Returns (safe_decisions_list, safety_note).
    safety_note: "none" | "circuit_breaker" | "rate_limit" | "amount_reduced" | "halt"
    """
    import csv

    hold_all = lambda reason, note: ([{"action": "HOLD", "ticker": "NONE", "amount_usd": 0.0, "reasoning": reason}], note)

    # 1. Circuit breaker: compare today's value to prior calendar day's last close
    try:
        if os.path.exists(config.PORTFOLIO_HISTORY_FILE):
            with open(config.PORTFOLIO_HISTORY_FILE) as f:
                rows = list(csv.DictReader(f))
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            prior_rows = [r for r in rows if not r["date"].startswith(today_str)]
            if prior_rows:
                prior_value = float(prior_rows[-1]["total_value"])
                current_value = portfolio["total_value"]
                drop_pct = (prior_value - current_value) / prior_value
                if drop_pct >= config.CIRCUIT_BREAKER_PCT:
                    logger.warning(f"CIRCUIT BREAKER triggered: drop={drop_pct:.1%}")
                    return hold_all(f"Circuit breaker: portfolio dropped {drop_pct:.1%} vs prior day.", "halt")
    except Exception as e:
        logger.error(f"Circuit breaker check failed: {e}")

    # If entire batch is HOLD, pass through
    if all(d["action"] == "HOLD" for d in decisions):
        return decisions, "none"

    # 2. Rolling trade rate limiter: cap total BUY/SELL actions in rolling window
    try:
        journal = _load_json(config.TRADE_JOURNAL_FILE, [])
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.ROLLING_TRADE_DAYS)
        existing_trades = len([
            e for e in journal
            if e.get("action") in ("BUY", "SELL")
            and _parse_iso(e.get("timestamp", "")) >= cutoff
        ])
        new_trades = len([d for d in decisions if d["action"] in ("BUY", "SELL")])
        if existing_trades + new_trades > config.MAX_TRADES_ROLLING:
            logger.warning(f"Trade rate limit: {existing_trades} existing + {new_trades} new > {config.MAX_TRADES_ROLLING}")
            return hold_all(f"Rate limiter: {existing_trades}+{new_trades} trades exceed {config.ROLLING_TRADE_DAYS}-day window limit.", "rate_limit")
    except Exception as e:
        logger.error(f"Trade rate limit check failed: {e}")

    # 3. Technical signal agreement filter
    if state:
        filtered = []
        for d in decisions:
            if d["action"] in ("BUY", "SELL") and not _check_signal_agreement(d, state):
                filtered.append({**d, "action": "HOLD", "ticker": "NONE", "amount_usd": 0.0,
                                  "reasoning": f"Signal filter overrode {d['action']} {d['ticker']}: technical indicators disagree"})
            else:
                filtered.append(d)
        decisions = filtered

    if all(d["action"] == "HOLD" for d in decisions):
        return decisions, "signal_filter"

    # 4. Position-aware SELL sizing: clip SELL amount to actual position value
    positions = portfolio.get("positions", {})
    for d in decisions:
        if d["action"] == "SELL":
            pos = positions.get(d["ticker"])
            if pos and d["amount_usd"] > pos["market_value"]:
                logger.warning(f"SELL {d['ticker']}: requested ${d['amount_usd']:.2f} > position ${pos['market_value']:.2f} — clipping")
                d["amount_usd"] = round(pos["market_value"], 2)

    # 5. Amount cap: total BUY amounts must not exceed cash
    safety_note = "none"
    total_buy = sum(d["amount_usd"] for d in decisions if d["action"] == "BUY")
    cash = portfolio["cash"]
    if total_buy > cash:
        safe_cash = cash * 0.95
        scale = safe_cash / total_buy if total_buy > 0 else 0
        for d in decisions:
            if d["action"] == "BUY":
                d["amount_usd"] = round(d["amount_usd"] * scale, 2)
        logger.warning(f"Total BUY ${total_buy:.2f} exceeds cash ${cash:.2f} — scaled down by {scale:.2f}")
        safety_note = "amount_reduced"

    return decisions, safety_note


# ---------------------------------------------------------------------------
# Trade Journal
# ---------------------------------------------------------------------------

def append_trade_journal(entry: dict) -> None:
    journal = _load_json(config.TRADE_JOURNAL_FILE, [])
    journal.append(entry)
    tmp = config.TRADE_JOURNAL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(journal, f, indent=2)
    os.replace(tmp, config.TRADE_JOURNAL_FILE)
    logger.info(f"Trade journal: {entry['action']} {entry['ticker']} ${entry['amount_usd']:.2f} logged")


# ---------------------------------------------------------------------------
# Weekly Macro Trends Update
# ---------------------------------------------------------------------------

def _should_update_macro_trends() -> bool:
    if not os.path.exists(config.MACRO_TRENDS_FILE):
        return True
    # Update on Mondays, but only once — skip if already written today
    now = datetime.now(timezone.utc)
    if now.weekday() != 0:
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(config.MACRO_TRENDS_FILE), tz=timezone.utc)
    return mtime.date() < now.date()


def update_macro_trends(state: dict) -> None:
    logger.info("Generating weekly macro outlook...")
    etf_summary = "\n".join(
        f"  {sym}: ${d.get('price', 'N/A')} | RSI: {d.get('rsi', 'N/A')} | Signal: {d.get('signal', 'N/A')}"
        for sym, d in state.get("etf_data", {}).items()
    )
    macro_prompt = f"""Write a 3-paragraph macro market outlook in Markdown for the week ahead.
Focus on: sector rotation signals, key macro risks, and 1-week directional bias across ETFs.
Be concise and analytical. Do not repeat the raw numbers — synthesize them.

CURRENT ETF DATA:
{etf_summary}
"""
    try:
        raw = call_ollama(macro_prompt)
        # Strip think tags from macro outlook
        clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        with open(config.MACRO_TRENDS_FILE, "w") as f:
            f.write(f"# Weekly Macro Outlook\n_Updated: {_now_iso()}_\n\n{clean}\n")
        logger.info("macro_trends.md updated")
    except Exception as e:
        logger.error(f"Macro trends update failed: {e}")


# ---------------------------------------------------------------------------
# News Triage
# ---------------------------------------------------------------------------

def triage_news(state: dict) -> list:
    """
    Keyword-based news triage — instantaneous, no Ollama call.
    Scores each article for macro/sector relevance, identifies the most relevant
    ETF, and extracts large-cap tickers. Filters to TRIAGE_MIN_RELEVANCE.
    Summaries come from each source's existing data (Alpaca has built-in summaries).
    """
    all_articles = []
    for h in state.get("rss_headlines", [])[:10]:
        all_articles.append({"title": h["title"], "summary": "", "tickers": [], "body": "", "is_reddit": False})
    for a in state.get("alphavantage_news", [])[:8]:
        all_articles.append({"title": a["title"], "summary": "", "tickers": a.get("tickers", []), "body": "", "is_reddit": False})
    for n in state.get("newsapi_headlines", [])[:8]:
        all_articles.append({"title": n["title"], "summary": "", "tickers": [], "body": "", "is_reddit": False})
    for a in state.get("alpaca_news", [])[:8]:
        all_articles.append({"title": a["title"], "summary": a.get("summary", ""),
                              "tickers": a.get("tickers", []), "body": "", "is_reddit": False})
    for p in sorted(state.get("reddit_posts", []), key=lambda x: x.get("score", 0), reverse=True)[:10]:
        body = p.get("body", "")
        all_articles.append({
            "title": p["title"],
            "summary": body[:300] if body else "",
            "tickers": [],
            "body": body,
            "num_comments": p.get("num_comments", 0),
            "score": p.get("score", 0),
            "subreddit": p.get("subreddit", ""),
            "is_reddit": True,
        })

    # First pass: score all articles, collect relevant Reddit posts for sentiment batch
    scored = []
    reddit_for_sentiment = []
    for i, art in enumerate(all_articles):
        score, etf, tickers = _score_article(art["title"], art["tickers"], art["body"])
        scored.append((score, etf, tickers))
        if art["is_reddit"] and score >= config.TRIAGE_MIN_RELEVANCE:
            reddit_for_sentiment.append({
                "idx": i,
                "title": art["title"],
                "body": art.get("body", ""),
                "subreddit": art.get("subreddit", ""),
            })

    # Single Qwen batch call for Reddit sentiment (only relevant posts, minimal output)
    sentiment_map = _reddit_sentiment_batch(reddit_for_sentiment)

    result = []
    for i, (art, (score, etf, tickers)) in enumerate(zip(all_articles, scored)):
        if score < config.TRIAGE_MIN_RELEVANCE:
            continue
        sentiment = sentiment_map.get(i) if art["is_reddit"] else None
        entry = {
            "title": art["title"][:200],
            "summary": (art["summary"] or art["title"])[:300],
            "sector_etf": etf,
            "tickers": tickers,
            "relevance": score,
            "sentiment": sentiment,
        }
        if art["is_reddit"]:
            entry["num_comments"] = art.get("num_comments", 0)
            entry["reddit_score"] = art.get("score", 0)
            entry["subreddit"] = art.get("subreddit", "")
        result.append(entry)

    result.sort(key=lambda x: x["relevance"], reverse=True)
    logger.info(f"Triage: {len(result)}/{len(all_articles)} articles passed (relevance ≥ {config.TRIAGE_MIN_RELEVANCE})")
    return result


def _write_enriched_news(articles: list) -> None:
    data = {"triage_at": _now_iso(), "articles": articles}
    tmp = config.ENRICHED_NEWS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, config.ENRICHED_NEWS_FILE)


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run() -> None:
    logger.info("Agent: starting decision cycle")

    # Attach any trailing stops that couldn't be placed last run (e.g. market was closed)
    try:
        broker.process_pending_stops()
    except Exception as e:
        logger.error(f"Pending stops processing failed: {e}")

    # Load sensor data
    state = _load_json(config.SYSTEM_STATE_FILE, {})
    if not state:
        logger.error("system_state.json is empty or missing — run sensors first")
        return

    # Get portfolio from Alpaca
    try:
        portfolio = broker.get_portfolio_value()
    except Exception as e:
        logger.error(f"Could not fetch portfolio from Alpaca: {e}")
        return

    # Triage: classify and filter all raw headlines via Qwen before building the trading prompt
    enriched_news = triage_news(state)
    _write_enriched_news(enriched_news)

    # Build prompt and call Ollama
    prompt = build_prompt(state, portfolio, enriched_news)
    logger.info("Calling Ollama for trading decision...")
    try:
        raw_response = call_ollama(prompt)
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return

    # Parse response (supports multi-action)
    think_text, decisions = parse_response(raw_response)
    batch_id = str(uuid.uuid4())[:8]

    if decisions is None:
        logger.error("Failed to parse a valid decision from Ollama response")
        append_trade_journal({
            "id": str(uuid.uuid4()),
            "batch_id": batch_id,
            "timestamp": _now_iso(),
            "action": "HOLD",
            "ticker": "NONE",
            "amount_usd": 0.0,
            "reasoning": "Parse failure — defaulting to HOLD",
            "think_reasoning": think_text,
            "safety_applied": "parse_failure",
            "order_id": None,
            "executed": False,
        })
        return

    # Safety gates (operates on full batch)
    safe_decisions, safety_note = check_safety_gates(decisions, portfolio, state)
    for d in safe_decisions:
        logger.info(f"Decision: {d['action']} {d['ticker']} ${d['amount_usd']:.2f} (safety: {safety_note})")

    if safety_note == "halt":
        for d in safe_decisions:
            append_trade_journal({
                "id": str(uuid.uuid4()),
                "batch_id": batch_id,
                "timestamp": _now_iso(),
                **d,
                "think_reasoning": think_text,
                "safety_applied": "circuit_breaker",
                "order_id": None,
                "executed": False,
            })
        logger.warning("Pipeline halted by circuit breaker")
        return

    # Execute each action in batch
    for decision in safe_decisions:
        order_id = None
        executed = False

        # Guard: skip SELL if we don't hold the position
        if decision["action"] == "SELL" and decision["ticker"] not in portfolio.get("positions", {}):
            logger.warning(f"SELL skipped — no position in {decision['ticker']}")
            append_trade_journal({
                "id": str(uuid.uuid4()),
                "batch_id": batch_id,
                "timestamp": _now_iso(),
                "action": "SELL",
                "ticker": decision["ticker"],
                "amount_usd": decision["amount_usd"],
                "reasoning": decision["reasoning"],
                "think_reasoning": think_text,
                "safety_applied": "no_position",
                "order_id": None,
                "executed": False,
            })
            continue

        if decision["action"] in ("BUY", "SELL"):
            try:
                order = broker.place_order(
                    ticker=decision["ticker"],
                    side=decision["action"].lower(),
                    amount_usd=decision["amount_usd"],
                )
                order_id = str(order.id)
                executed = True

                if decision["action"] == "BUY":
                    broker.attach_trailing_stop(order_id, decision["ticker"])
            except Exception as e:
                logger.error(f"Order execution failed for {decision['ticker']}: {e}")
        elif decision["action"] == "HOLD":
            executed = True

        # Log each action to trade journal
        append_trade_journal({
            "id": str(uuid.uuid4()),
            "batch_id": batch_id,
            "timestamp": _now_iso(),
            "action": decision["action"],
            "ticker": decision["ticker"],
            "amount_usd": decision["amount_usd"],
            "reasoning": decision["reasoning"],
            "think_reasoning": think_text,
            "safety_applied": safety_note,
            "order_id": order_id,
            "executed": executed,
        })

    # Always snapshot portfolio history — even on HOLD
    try:
        broker.update_portfolio_history()
    except Exception as e:
        logger.error(f"Portfolio history update failed: {e}")

    # Weekly macro update
    if _should_update_macro_trends():
        update_macro_trends(state)

    logger.info(f"Agent: decision cycle complete (batch {batch_id}, {len(safe_decisions)} action(s))")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json_array(text: str):
    """Return the first bracket-balanced [...] substring, or None if not found."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_iso(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)
