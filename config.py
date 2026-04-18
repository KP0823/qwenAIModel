import os
from dotenv import load_dotenv

load_dotenv()

# --- Ollama ---
OLLAMA_MODEL = "StockAI:latest"
OLLAMA_ENDPOINT = "http://localhost:11434"
OLLAMA_GENERATE_URL = f"{OLLAMA_ENDPOINT}/api/generate"
OLLAMA_TIMEOUT = 300  # seconds — full context prompt can take 2-3 min on GPU

# --- Alpaca Paper Trading ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"

# --- Reddit ---
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "MacroAgent/1.0")

# --- Target ETFs ---
TARGET_ETFS = ["XLK", "XLE", "XLU", "XLF", "XLV", "XLI", "GAL", "TOTL", "SRLN", "RLY"]

# --- RSS Feeds ---
RSS_FEEDS = {
    "bloomberg_markets": "https://feeds.bloomberg.com/markets/news.rss",
    "yahoo_finance": "https://finance.yahoo.com/rss/",
    "cnbc_markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "cnbc_finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
}

# --- Alpha Vantage ---
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
ALPHA_VANTAGE_NEWS_URL = "https://www.alphavantage.co/query"

# --- NewsAPI ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
NEWSAPI_URL = "https://newsapi.org/v2/top-headlines"

# --- Reddit Subreddits ---
REDDIT_SUBREDDITS = ["investing", "stocks", "economics"]

# --- Risk Parameters ---
CIRCUIT_BREAKER_PCT = 0.05     # halt trading if portfolio drops >5% in one day
MAX_TRADES_ROLLING = 6         # max BUY/SELL actions in rolling window (rate limiter, not PDT)
ROLLING_TRADE_DAYS = 5         # rolling window size in calendar days
TRAILING_STOP_PCT = 10.0       # trailing stop percentage attached to every BUY (GTC)
MAX_PORTFOLIO_USD = 100_000.0  # paper account default — change to 50.0 when switching to live $50 account

# --- Market Hours (ET) ---
MARKET_RUN_HOURS = [10, 11, 12, 13, 14, 15]  # ET hours to run pipeline

# --- News Triage ---
TRIAGE_MIN_RELEVANCE = 6     # articles below this score are filtered before the trading prompt
TRIAGE_OLLAMA_TIMEOUT = 90   # timeout for Reddit sentiment batch call — fail fast, fall back to keywords
ALPACA_NEWS_LIMIT = 20       # max articles per Alpaca News API call

# --- File Paths ---
_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_BASE, "data")
LOGS_DIR = os.path.join(_BASE, "logs")
SYSTEM_STATE_FILE = os.path.join(DATA_DIR, "system_state.json")
MACRO_TRENDS_FILE = os.path.join(DATA_DIR, "macro_trends.md")
TRADE_JOURNAL_FILE = os.path.join(DATA_DIR, "trade_journal.json")
PORTFOLIO_HISTORY_FILE = os.path.join(DATA_DIR, "portfolio_history.csv")
ENRICHED_NEWS_FILE = os.path.join(DATA_DIR, "enriched_news.json")
LOG_FILE = os.path.join(LOGS_DIR, "agent.log")
