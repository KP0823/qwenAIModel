import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import feedparser
import praw
import requests
import yfinance as yf

import config

logger = logging.getLogger(__name__)

SEEN_HEADLINES_FILE = os.path.join(config.DATA_DIR, "seen_headlines.json")
HEADLINE_RETENTION_DAYS = 7
HEADLINE_MAX_AGE_DAYS = 3  # reject articles older than this


def fetch_technical_data(symbol: str) -> dict:
    """Fetch price, RSI(14), and 200-day MA for a single ETF symbol."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1y")

        if hist.empty or len(hist) < 15:
            logger.warning(f"{symbol}: insufficient history")
            return {"symbol": symbol, "price": None, "rsi": None, "ma_50": None, "ma_200": None, "macd": None, "ma_cross": None, "signal": "INSUFFICIENT_DATA", "error": "insufficient history"}

        price = float(hist["Close"].iloc[-1])

        # Wilder's Smoothing RSI (14-period, no TA library)
        delta = hist["Close"].diff()
        gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
        rs = gain / loss
        rsi_series = 100 - (100 / (1 + rs))
        rsi = float(rsi_series.iloc[-1])

        # 50-day MA
        ma_50_raw = hist["Close"].rolling(50).mean().iloc[-1]
        ma_50 = float(ma_50_raw) if not _is_nan(ma_50_raw) else None

        # 200-day MA
        ma_raw = hist["Close"].rolling(200).mean().iloc[-1]
        ma_200 = float(ma_raw) if not _is_nan(ma_raw) else None

        # MACD (12/26/9 — standard, no TA library)
        ema_12 = hist["Close"].ewm(span=12, adjust=False).mean()
        ema_26 = hist["Close"].ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_val = float(macd_line.iloc[-1])
        macd_signal_val = float(signal_line.iloc[-1])
        macd_label = "BULLISH" if macd_val > macd_signal_val else "BEARISH"

        # Golden/Death cross (50MA vs 200MA)
        if ma_50 and ma_200:
            cross = "GOLDEN_CROSS" if ma_50 > ma_200 else "DEATH_CROSS"
        else:
            cross = None

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

        signal = f"{momentum}|{trend}|{macd_label}"
        if cross:
            signal += f"|{cross}"

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "rsi": round(rsi, 1),
            "ma_50": round(ma_50, 2) if ma_50 else None,
            "ma_200": round(ma_200, 2) if ma_200 else None,
            "macd": macd_label,
            "ma_cross": cross,
            "signal": signal,
            "fetched_at": _now_iso(),
        }
    except Exception as e:
        logger.error(f"{symbol} technical fetch failed: {e}")
        return {"symbol": symbol, "price": None, "rsi": None, "ma_50": None, "ma_200": None, "macd": None, "ma_cross": None, "signal": "ERROR", "error": str(e)}


def fetch_rss_headlines() -> list:
    """Fetch new macro headlines, skipping any already seen in the last 7 days."""
    seen = _load_seen_headlines()
    now = _now_iso()
    headlines = []
    new_seen = {}

    for source, url in config.RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                logger.warning(f"RSS feed empty: {source}")
                continue
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                key = hashlib.md5(title.encode()).hexdigest()
                if key in seen:
                    continue  # already fed to the model
                published = entry.get("published", entry.get("updated", ""))
                pub_dt = _parse_published(published)
                cutoff = datetime.now(timezone.utc) - timedelta(days=HEADLINE_MAX_AGE_DAYS)
                if pub_dt and pub_dt < cutoff:
                    continue  # article too old — skip
                headlines.append({
                    "source": source,
                    "title": title,
                    "published": published,
                    "processed_at": now,
                })
                new_seen[key] = {"title": title, "processed_at": now}
        except Exception as e:
            logger.error(f"RSS fetch failed for {source}: {e}")

    _save_seen_headlines(seen, new_seen)
    logger.info(f"RSS: {len(headlines)} new headlines (skipped already-seen)")
    return headlines[:30]


def fetch_alphavantage_news() -> list:
    """Fetch ticker-specific news sentiment from Alpha Vantage (free tier: 25 req/day)."""
    if not config.ALPHA_VANTAGE_API_KEY:
        logger.warning("Alpha Vantage API key not set — skipping")
        return []

    seen = _load_seen_headlines()
    now = _now_iso()
    articles = []
    new_seen = {}

    tickers = ",".join(config.TARGET_ETFS)
    try:
        r = requests.get(
            config.ALPHA_VANTAGE_NEWS_URL,
            params={"function": "NEWS_SENTIMENT", "tickers": tickers, "apikey": config.ALPHA_VANTAGE_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("feed", [])[:20]:
            title = item.get("title", "").strip()
            if not title:
                continue
            key = hashlib.md5(title.encode()).hexdigest()
            if key in seen:
                continue
            pub = item.get("time_published", "")
            pub_dt = _parse_av_date(pub)
            cutoff = datetime.now(timezone.utc) - timedelta(days=HEADLINE_MAX_AGE_DAYS)
            if pub_dt and pub_dt < cutoff:
                continue
            sentiment = item.get("overall_sentiment_label", "Neutral")
            relevant_tickers = [t["ticker"] for t in item.get("ticker_sentiment", [])
                                if t.get("ticker") in config.TARGET_ETFS]
            articles.append({
                "source": item.get("source", "AlphaVantage"),
                "title": title,
                "published": pub,
                "sentiment": sentiment,
                "tickers": relevant_tickers,
                "processed_at": now,
            })
            new_seen[key] = {"title": title, "processed_at": now}
    except Exception as e:
        logger.error(f"Alpha Vantage news fetch failed: {e}")

    _save_seen_headlines(_load_seen_headlines(), new_seen)
    logger.info(f"Alpha Vantage: {len(articles)} new articles")
    return articles[:15]


def _parse_av_date(date_str: str):
    """Parse Alpha Vantage date format (20260406T120000) to datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def fetch_newsapi_headlines() -> list:
    """Fetch top business headlines from NewsAPI (free tier: 100 req/day)."""
    if not config.NEWS_API_KEY:
        logger.warning("NewsAPI key not set — skipping")
        return []

    seen = _load_seen_headlines()
    now = _now_iso()
    headlines = []
    new_seen = {}

    try:
        r = requests.get(
            config.NEWSAPI_URL,
            params={"category": "business", "country": "us", "pageSize": 15, "apiKey": config.NEWS_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("articles", []):
            title = item.get("title", "").strip()
            if not title or title == "[Removed]":
                continue
            key = hashlib.md5(title.encode()).hexdigest()
            if key in seen:
                continue
            published = item.get("publishedAt", "")
            pub_dt = _parse_published(published)
            cutoff = datetime.now(timezone.utc) - timedelta(days=HEADLINE_MAX_AGE_DAYS)
            if pub_dt and pub_dt < cutoff:
                continue
            headlines.append({
                "source": item.get("source", {}).get("name", "NewsAPI"),
                "title": title,
                "published": published,
                "processed_at": now,
            })
            new_seen[key] = {"title": title, "processed_at": now}
    except Exception as e:
        logger.error(f"NewsAPI fetch failed: {e}")

    _save_seen_headlines(_load_seen_headlines(), new_seen)
    logger.info(f"NewsAPI: {len(headlines)} new headlines")
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
                for submission in subreddit.hot(limit=20):
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


def _parse_published(date_str: str):
    """Try to parse an RSS date string into a UTC datetime. Returns None on failure."""
    if not date_str:
        return None
    import email.utils
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _load_seen_headlines() -> dict:
    """Load seen headline hashes, pruning entries older than HEADLINE_RETENTION_DAYS."""
    try:
        with open(SEEN_HEADLINES_FILE) as f:
            seen = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HEADLINE_RETENTION_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if v.get("processed_at", "") >= cutoff}


def _save_seen_headlines(existing: dict, new_entries: dict) -> None:
    existing.update(new_entries)
    tmp = SEEN_HEADLINES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, SEEN_HEADLINES_FILE)


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

    # Load previous state to carry forward headlines if no new ones arrive
    prev_state = {}
    try:
        with open(config.SYSTEM_STATE_FILE) as f:
            prev_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    etf_data = {}
    yfinance_ok = True
    for symbol in config.TARGET_ETFS:
        data = fetch_technical_data(symbol)
        etf_data[symbol] = data
        if data.get("price") is None:
            yfinance_ok = False

    headlines = fetch_rss_headlines()
    rss_ok = "ok" if headlines else "partial"

    av_news = fetch_alphavantage_news()
    av_ok = "ok" if av_news else "partial"

    newsapi_headlines = fetch_newsapi_headlines()
    newsapi_ok = "ok" if newsapi_headlines else "partial"

    reddit_posts = fetch_reddit_sentiment()
    reddit_ok = "ok" if reddit_posts else "partial"

    state = {
        "last_sync": _now_iso(),
        "etf_data": etf_data,
        "rss_headlines": headlines if headlines else prev_state.get("rss_headlines", []),
        "alphavantage_news": av_news if av_news else prev_state.get("alphavantage_news", []),
        "newsapi_headlines": newsapi_headlines if newsapi_headlines else prev_state.get("newsapi_headlines", []),
        "reddit_posts": reddit_posts,
        "api_health": {
            "yfinance": "ok" if yfinance_ok else "partial",
            "rss": rss_ok,
            "alphavantage": av_ok,
            "newsapi": newsapi_ok,
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
