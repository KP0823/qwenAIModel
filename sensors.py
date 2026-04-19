import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import feedparser
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
            return {"symbol": symbol, "price": None, "rsi": None, "ma_50": None, "ma_200": None, "macd": None, "ema_cross_fast": None, "ma_cross": None, "donchian_high": None, "donchian_low": None, "volume": None, "avg_volume_20": None, "volume_ratio": None, "signal": "INSUFFICIENT_DATA", "error": "insufficient history"}

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

        # Fast EMA cross (8/21) — more responsive than MACD for early momentum reads
        ema_8 = hist["Close"].ewm(span=8, adjust=False).mean()
        ema_21 = hist["Close"].ewm(span=21, adjust=False).mean()
        ema_cross_fast = "BULL" if float(ema_8.iloc[-1]) > float(ema_21.iloc[-1]) else "BEAR"

        # Golden/Death cross (50MA vs 200MA)
        if ma_50 and ma_200:
            cross = "GOLDEN_CROSS" if ma_50 > ma_200 else "DEATH_CROSS"
        else:
            cross = None

        # Donchian Channels (20-period high/low) — breakout reference
        donchian_high = round(float(hist["High"].tail(20).max()), 2)
        donchian_low = round(float(hist["Low"].tail(20).min()), 2)

        # Volume vs 20-day average
        try:
            vol = int(hist["Volume"].iloc[-1])
            avg_vol_20 = int(hist["Volume"].tail(20).mean())
            vol_ratio = round(vol / avg_vol_20, 2) if avg_vol_20 > 0 else None
        except Exception:
            vol = avg_vol_20 = None
            vol_ratio = None

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
            "ema_cross_fast": ema_cross_fast,
            "ma_cross": cross,
            "donchian_high": donchian_high,
            "donchian_low": donchian_low,
            "volume": vol,
            "avg_volume_20": avg_vol_20,
            "volume_ratio": vol_ratio,
            "signal": signal,
            "fetched_at": _now_iso(),
        }
    except Exception as e:
        logger.error(f"{symbol} technical fetch failed: {e}")
        return {"symbol": symbol, "price": None, "rsi": None, "ma_50": None, "ma_200": None, "macd": None, "ema_cross_fast": None, "ma_cross": None, "donchian_high": None, "donchian_low": None, "volume": None, "avg_volume_20": None, "volume_ratio": None, "signal": "ERROR", "error": str(e)}


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

    try:
        r = requests.get(
            config.ALPHA_VANTAGE_NEWS_URL,
            params={"function": "NEWS_SENTIMENT", "apikey": config.ALPHA_VANTAGE_API_KEY, "limit": 20},
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

    _save_seen_headlines(seen, new_seen)
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

    _save_seen_headlines(seen, new_seen)
    logger.info(f"NewsAPI: {len(headlines)} new headlines")
    return headlines[:15]


def fetch_reddit_posts() -> list:
    """Fetch top posts from financial subreddits via the public JSON API (no credentials needed)."""
    seen = _load_seen_headlines()
    now = _now_iso()
    posts = []
    new_seen = {}

    for sub in config.REDDIT_SUBREDDITS:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}.json?limit=25&sort=top&t=day",
                headers={"User-Agent": config.REDDIT_USER_AGENT},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(f"Reddit r/{sub}: HTTP {r.status_code}")
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(days=HEADLINE_MAX_AGE_DAYS)
            for child in r.json()["data"]["children"]:
                d = child["data"]
                title = d.get("title", "").strip()
                if not title:
                    continue
                key = hashlib.md5(title.encode()).hexdigest()
                if key in seen:
                    continue
                try:
                    created = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    continue
                if created < cutoff:
                    continue
                body = d.get("selftext", "").strip() if d.get("is_self") else ""
                posts.append({
                    "subreddit": sub,
                    "title": title,
                    "body": body[:600],      # text-post body (empty for link posts)
                    "num_comments": int(d.get("num_comments", 0)),
                    "score": int(d.get("score", 0)),
                    "url": d.get("url", ""),
                    "processed_at": now,
                })
                new_seen[key] = {"title": title, "processed_at": now}
        except Exception as e:
            logger.error(f"Reddit r/{sub} fetch failed: {e}")

    _save_seen_headlines(seen, new_seen)
    posts_sorted = sorted(posts, key=lambda x: x["score"], reverse=True)
    logger.info(f"Reddit: {len(posts_sorted)} new posts")
    return posts_sorted[:30]


def fetch_alpaca_news() -> list:
    """Fetch ETF-relevant news from the Alpaca News API."""
    if not config.ALPACA_API_KEY:
        logger.warning("Alpaca API key not set — skipping Alpaca news")
        return []
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError:
        logger.warning("alpaca.data.historical.news unavailable — skipping")
        return []

    seen = _load_seen_headlines()
    now = _now_iso()
    articles = []
    new_seen = {}

    try:
        client = NewsClient(api_key=config.ALPACA_API_KEY, secret_key=config.ALPACA_SECRET_KEY)
        req = NewsRequest(symbols=",".join(config.TARGET_ETFS), limit=config.ALPACA_NEWS_LIMIT, sort="desc")
        response = client.get_news(req)
        items = response.data.get("news", [])

        cutoff = datetime.now(timezone.utc) - timedelta(days=HEADLINE_MAX_AGE_DAYS)
        for article in items:
            title = (article.headline or "").strip()
            if not title:
                continue
            key = hashlib.md5(title.encode()).hexdigest()
            if key in seen:
                continue
            created = article.created_at
            if created:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    continue
            articles.append({
                "source": f"Alpaca/{article.source}" if article.source else "Alpaca",
                "title": title,
                "summary": article.summary or "",
                "tickers": list(article.symbols or []),
                "published": created.isoformat() if created else "",
                "processed_at": now,
            })
            new_seen[key] = {"title": title, "processed_at": now}
    except Exception as e:
        logger.error(f"Alpaca news fetch failed: {e}")

    _save_seen_headlines(seen, new_seen)
    logger.info(f"Alpaca News: {len(articles)} new articles")
    return articles


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

    alpaca_news = fetch_alpaca_news()
    alpaca_news_ok = "ok" if alpaca_news else "partial"

    reddit_posts = fetch_reddit_posts()
    reddit_ok = "ok" if reddit_posts else "partial"

    state = {
        "last_sync": _now_iso(),
        "etf_data": etf_data,
        "rss_headlines": headlines if headlines else prev_state.get("rss_headlines", []),
        "alphavantage_news": av_news if av_news else prev_state.get("alphavantage_news", []),
        "newsapi_headlines": newsapi_headlines if newsapi_headlines else prev_state.get("newsapi_headlines", []),
        "alpaca_news": alpaca_news if alpaca_news else prev_state.get("alpaca_news", []),
        "reddit_posts": reddit_posts if reddit_posts else prev_state.get("reddit_posts", []),
        "api_health": {
            "yfinance": "ok" if yfinance_ok else "partial",
            "rss": rss_ok,
            "alphavantage": av_ok,
            "newsapi": newsapi_ok,
            "alpaca_news": alpaca_news_ok,
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
