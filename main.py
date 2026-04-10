import csv
import json
import logging
import subprocess
import sys
import time
import zoneinfo
from datetime import datetime
from pathlib import Path

import requests

import config


def _setup_logging():
    Path(config.LOGS_DIR).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


def initialize_data_files():
    """Create data/ directory and seed empty state files on first run."""
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)

    if not Path(config.SYSTEM_STATE_FILE).exists():
        with open(config.SYSTEM_STATE_FILE, "w") as f:
            json.dump({}, f)

    if not Path(config.TRADE_JOURNAL_FILE).exists():
        with open(config.TRADE_JOURNAL_FILE, "w") as f:
            json.dump([], f)

    if not Path(config.PORTFOLIO_HISTORY_FILE).exists():
        with open(config.PORTFOLIO_HISTORY_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["date", "total_value", "cash", "holdings"])


def ensure_ollama_running():
    """Check if Ollama is responsive. If not, restart it and wait for it to come back up."""
    logger = logging.getLogger("main")
    try:
        r = requests.get(config.OLLAMA_ENDPOINT, timeout=5)
        if r.status_code == 200:
            logger.info("Ollama: already running")
            return
    except Exception:
        pass

    logger.warning("Ollama not responding — restarting...")
    subprocess.run(["pkill", "-f", "ollama"], capture_output=True)
    time.sleep(2)
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait up to 30s for Ollama to come back
    for _ in range(15):
        time.sleep(2)
        try:
            r = requests.get(config.OLLAMA_ENDPOINT, timeout=3)
            if r.status_code == 200:
                logger.info("Ollama: restarted successfully")
                return
        except Exception:
            pass

    logger.error("Ollama failed to restart after 30s — agent will likely fail")


def is_market_hours() -> bool:
    """Check if current time is within US market hours (Mon-Fri, 9-16 ET)."""
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return et.weekday() < 5 and 9 <= et.hour < 16


def run_pipeline():
    _setup_logging()
    logger = logging.getLogger("main")
    logger.info("=== Pipeline starting ===")

    if not is_market_hours():
        logger.warning("Outside US market hours — running anyway (manual trigger)")

    initialize_data_files()
    ensure_ollama_running()

    try:
        import sensors
        logger.info("Step 1/2: Running market sensors...")
        sensors.run()
    except Exception as e:
        logger.error(f"Sensors failed: {e} — continuing with stale data")

    try:
        import agent
        logger.info("Step 2/2: Running AI decision engine...")
        agent.run()
    except Exception as e:
        logger.error(f"Agent failed: {e}")

    logger.info("=== Pipeline complete ===")


if __name__ == "__main__":
    run_pipeline()
