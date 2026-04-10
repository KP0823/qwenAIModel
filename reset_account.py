"""Reset Alpaca paper account: cancel all orders, close all positions, wipe local data."""
import json
import os
import csv
from pathlib import Path

import config


def main():
    from alpaca.trading.client import TradingClient

    client = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=True,
    )

    # 1. Cancel all open orders
    cancelled = client.cancel_orders()
    print(f"Cancelled {len(cancelled) if cancelled else 0} open orders")

    # 2. Close all open positions
    positions = client.get_all_positions()
    for pos in positions:
        print(f"Closing: {pos.symbol} ({pos.qty} shares @ ${float(pos.current_price):.2f})")
        client.close_position(pos.symbol)
    if not positions:
        print("No open positions to close")

    # 3. Wipe local data files
    data_files = [
        config.TRADE_JOURNAL_FILE,
        config.PORTFOLIO_HISTORY_FILE,
        config.MACRO_TRENDS_FILE,
        config.SYSTEM_STATE_FILE,
        os.path.join(config.DATA_DIR, "seen_headlines.json"),
        os.path.join(config.DATA_DIR, "pending_stops.json"),
    ]
    for f in data_files:
        if os.path.exists(f):
            os.remove(f)
            print(f"Deleted: {os.path.basename(f)}")

    # 4. Re-seed empty data files
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    with open(config.SYSTEM_STATE_FILE, "w") as f:
        json.dump({}, f)
    with open(config.TRADE_JOURNAL_FILE, "w") as f:
        json.dump([], f)
    with open(config.PORTFOLIO_HISTORY_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["date", "total_value", "cash", "holdings"])
    print("Re-seeded empty data files")

    # 5. Show final account state
    acct = client.get_account()
    print(f"\nAccount reset complete:")
    print(f"  Cash:   ${float(acct.cash):,.2f}")
    print(f"  Equity: ${float(acct.equity):,.2f}")


if __name__ == "__main__":
    main()
