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
# Context / Prompt Builder
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def build_prompt(state: dict, portfolio: dict) -> str:
    etf_lines = []
    for symbol, data in state.get("etf_data", {}).items():
        price = f"${data['price']:.2f}" if data.get("price") else "N/A"
        rsi = f"{data['rsi']:.1f}" if data.get("rsi") is not None else "N/A"
        ma = f"${data['ma_200']:.2f}" if data.get("ma_200") else "N/A"
        signal = data.get("signal", "UNKNOWN")
        etf_lines.append(f"  {symbol}: {price} | RSI: {rsi} | 200MA: {ma} | Signal: {signal}")

    headlines = state.get("rss_headlines", [])[:15]
    headline_lines = []
    for h in headlines:
        published = f" ({h['published'][:16]})" if h.get("published") else ""
        processed = f" [processed: {h['processed_at'][:10]}]" if h.get("processed_at") else ""
        headline_lines.append(f"  - [{h['source']}]{published} {h['title']}{processed}")

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

    reddit_posts = sorted(state.get("reddit_posts", []), key=lambda x: x.get("score", 0), reverse=True)[:10]
    reddit_lines = [f"  - r/{p['subreddit']}: \"{p['title']}\" (score: {p['score']})" for p in reddit_posts]

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

MACRO HEADLINES (RSS):
{chr(10).join(headline_lines) if headline_lines else "  No headlines available"}

ALPHA VANTAGE NEWS SENTIMENT:
{chr(10).join(av_lines) if av_lines else "  No Alpha Vantage data available"}

NEWSAPI HEADLINES:
{chr(10).join(newsapi_lines) if newsapi_lines else "  No NewsAPI data available"}

REDDIT PULSE:
{chr(10).join(reddit_lines) if reddit_lines else "  No Reddit data available"}

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

def call_ollama(prompt: str) -> str:
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    response = requests.post(config.OLLAMA_GENERATE_URL, json=payload, timeout=config.OLLAMA_TIMEOUT)
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

def check_safety_gates(decisions: list, portfolio: dict) -> tuple:
    """
    Accepts a list of decision dicts. Returns (safe_decisions_list, safety_note).
    safety_note: "none" | "circuit_breaker" | "pdt" | "amount_reduced" | "halt"
    """
    import csv

    hold_all = lambda reason, note: ([{"action": "HOLD", "ticker": "NONE", "amount_usd": 0.0, "reasoning": reason}], note)

    # 1. Circuit breaker: compare today's value to yesterday's close
    try:
        if os.path.exists(config.PORTFOLIO_HISTORY_FILE):
            with open(config.PORTFOLIO_HISTORY_FILE) as f:
                rows = list(csv.DictReader(f))
            if rows:
                yesterday_value = float(rows[-1]["total_value"])
                current_value = portfolio["total_value"]
                drop_pct = (yesterday_value - current_value) / yesterday_value
                if drop_pct >= config.CIRCUIT_BREAKER_PCT:
                    logger.warning(f"CIRCUIT BREAKER triggered: drop={drop_pct:.1%}")
                    return hold_all(f"Circuit breaker: portfolio dropped {drop_pct:.1%} today.", "halt")
    except Exception as e:
        logger.error(f"Circuit breaker check failed: {e}")

    # If entire batch is HOLD, pass through
    if all(d["action"] == "HOLD" for d in decisions):
        return decisions, "none"

    # 2. PDT check: count existing trades + new batch against limit
    try:
        journal = _load_json(config.TRADE_JOURNAL_FILE, [])
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.PDT_ROLLING_DAYS)
        existing_trades = len([
            e for e in journal
            if e.get("action") in ("BUY", "SELL")
            and _parse_iso(e.get("timestamp", "")) >= cutoff
        ])
        new_trades = len([d for d in decisions if d["action"] in ("BUY", "SELL")])
        if existing_trades + new_trades > config.PDT_MAX_DAY_TRADES:
            logger.warning(f"PDT limit: {existing_trades} existing + {new_trades} new > {config.PDT_MAX_DAY_TRADES}")
            return hold_all(f"PDT guardrail: {existing_trades}+{new_trades} trades exceed rolling window limit.", "pdt")
    except Exception as e:
        logger.error(f"PDT check failed: {e}")

    # 3. Amount cap: total BUY amounts must not exceed cash
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
    # Update on Mondays (weekday 0)
    return datetime.now(timezone.utc).weekday() == 0


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

    # Build prompt and call Ollama
    prompt = build_prompt(state, portfolio)
    logger.info("Calling Ollama...")
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
    safe_decisions, safety_note = check_safety_gates(decisions, portfolio)
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


def _parse_iso(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)
