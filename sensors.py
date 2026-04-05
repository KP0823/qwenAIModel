import json
import logging
import os
import time
from datetime import datetime, timezone

import feedparser
import praw
import requests
import yfinance as yf

import config

logger = logging.getLogger(__name__)


def fetch_technical_data(symbol: str) -> dict:
    """Fetch price, RSI(14), and 200-day MA for a single ETF symbol."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1y")

        if hist.empty or len(hist) < 15:
            logger.warning(f"{symbol}: insufficient history")
            return {"symbol": symbol, "price": None, "rsi": None, "ma_200": None, "signal": "INSUFFICIENT_DATA", "error": "insufficient history"}

        price = float(hist["Close"].iloc[-1])

        # Wilder's Smoothing RSI (14-period, no TA library)
        delta = hist["Close"].diff()
        gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        rs = gain / loss
        rsi_series = 100 - (100 / (1 + rs))
        rsi = float(rsi_series.iloc[-1])

        # 200-day MA
        ma_raw = hist["Close"].rolling(200).mean().iloc[-1]
        ma_200 = float(ma_raw) if not _is_nan(ma_raw) else None

        # Signal
        if ma_200 is None:
            trend = "INSUFFICIENT_DATA"
        elif price > ma_200:
            trend = "ABOVE_200MA"
        else:
            trend = "BELOW_200MA"

        if rsi >= 70:
            momentum = "OVERBOUGHT"
        elif rsi <= 30:
            momentum = "OVERSOLD"
        else:
            momentum = "NEUTRAL"

        signal = f"{momentum}|{trend}"

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "rsi": round(rsi, 1),
            "ma_200": round(ma_200, 2) if ma_200 else None,
            "signal": signal,
            "fetched_at": _now_iso(),
        }
    except Exception as e:
        logger.error(f"{symbol} technical fetch failed: {e}")
        return {"symbol": symbol, "price": None, "rsi": None, "ma_200": None, "signal": "ERROR", "error": str(e)}


def fetch_rss_headlines() -> list:
    """Fetch top macro headlines from configured RSS feeds."""
    headlines = []
    for source, url in config.RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                headlines.append({
                    "source": source,
                    "title": entry.get("title", ""),
                    "published": entry.get("published", entry.get("updated", "")),
                })
            if not feed.entries:
                logger.warning(f"RSS feed empty: {source}")
        except Exception as e:
            logger.error(f"RSS fetch failed for {source}: {e}")
    return headlines[:15]


def fetch_reddit_sentiment() -> list:
    """Fetch top posts from financial subreddits."""
    if not config.REDDIT_CLIENT_ID or not config.REDDIT_CLIENT_SECRET:
        logger.warning("Reddit credentials not set — skipping sentiment fetch")
        return []
    posts = []
    try:
        reddit = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
        )
        for sub_name in config.REDDIT_SUBREDDITS:
            try:
                subreddit = reddit.subreddit(sub_name)
                for submission in subreddit.hot(limit=10):
                    posts.append({
                        "subreddit": sub_name,
                        "title": submission.title,
                        "score": submission.score,
                    })
            except Exception as e:
                logger.error(f"Reddit fetch failed for r/{sub_name}: {e}")
    except Exception as e:
        logger.error(f"Reddit init failed: {e}")
    return posts


def _check_ollama_health() -> str:
    try:
        r = requests.get(config.OLLAMA_ENDPOINT, timeout=5)
        return "ok" if r.status_code == 200 else "error"
    except Exception:
        return "error"


def _check_alpaca_health() -> str:
    if not config.ALPACA_API_KEY:
        return "error"
    try:
        r = requests.get(
            "https://paper-api.alpaca.markets/v2/account",
            headers={
                "APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            },
            timeout=10,
        )
        return "ok" if r.status_code == 200 else "error"
    except Exception:
        return "error"


def write_system_state(state: dict) -> None:
    """Atomically write state to system_state.json."""
    tmp = config.SYSTEM_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.SYSTEM_STATE_FILE)
    logger.info(f"system_state.json written ({len(state.get('etf_data', {}))} ETFs)")


def run() -> dict:
    """Run the full sensor pipeline and write system_state.json. Returns the state dict."""
    logger.info("Sensors: starting data collection")

    etf_data = {}
    yfinance_ok = True
    for symbol in config.TARGET_ETFS:
        data = fetch_technical_data(symbol)
        etf_data[symbol] = data
        if data.get("price") is None:
            yfinance_ok = False

    headlines = fetch_rss_headlines()
    rss_ok = "ok" if headlines else "partial"

    reddit_posts = fetch_reddit_sentiment()
    reddit_ok = "ok" if reddit_posts else "partial"

    state = {
        "last_sync": _now_iso(),
        "etf_data": etf_data,
        "rss_headlines": headlines,
        "reddit_posts": reddit_posts,
        "api_health": {
            "yfinance": "ok" if yfinance_ok else "partial",
            "rss": rss_ok,
            "reddit": reddit_ok,
            "ollama": _check_ollama_health(),
            "alpaca": _check_alpaca_health(),
        },
    }

    write_system_state(state)
    logger.info("Sensors: done")
    return state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_nan(val) -> bool:
    try:
        import math
        return math.isnan(val)
    except (TypeError, ValueError):
        return True
