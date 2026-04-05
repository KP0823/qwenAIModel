import csv
import json
import logging
import sys
from pathlib import Path

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


def run_pipeline():
    _setup_logging()
    logger = logging.getLogger("main")
    logger.info("=== Pipeline starting ===")

    initialize_data_files()

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
