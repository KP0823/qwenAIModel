import csv
import logging
import os
import time
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from alpaca.trading.client import TradingClient
        _client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=True,
        )
    return _client


def get_portfolio_value() -> dict:
    """Return total equity, cash, and open positions from Alpaca."""
    client = _get_client()
    account = client.get_account()
    total_value = float(account.equity)
    cash = float(account.cash)

    raw_positions = client.get_all_positions()
    positions = {}
    for pos in raw_positions:
        positions[pos.symbol] = {
            "qty": float(pos.qty),
            "market_value": float(pos.market_value),
            "current_price": float(pos.current_price),
        }

    return {"total_value": total_value, "cash": cash, "positions": positions}


def get_positions() -> dict:
    """Return open positions dict only."""
    return get_portfolio_value()["positions"]


def place_order(ticker: str, side: str, amount_usd: float):
    """
    Place a notional (dollar-based) market order for fractional shares.

    side: "buy" or "sell"
    Returns the Alpaca order object.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _get_client()
    order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

    # Validate cash for BUY
    if order_side == OrderSide.BUY:
        portfolio = get_portfolio_value()
        available = portfolio["cash"]
        if amount_usd > available:
            amount_usd = round(available * 0.95, 2)
            logger.warning(f"Amount reduced to ${amount_usd:.2f} (available cash: ${available:.2f})")

    request = MarketOrderRequest(
        symbol=ticker,
        notional=amount_usd,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(request)
    logger.info(f"Order submitted: {side.upper()} {ticker} ${amount_usd:.2f} — order id: {order.id}")
    return order


def attach_trailing_stop(order_id: str, symbol: str, trail_percent: float = None) -> None:
    """
    Wait for the parent BUY order to fill, then attach a trailing stop-loss.
    Polls up to 30 seconds for fill confirmation.
    """
    if trail_percent is None:
        trail_percent = config.TRAILING_STOP_PCT

    from alpaca.trading.requests import TrailingStopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _get_client()

    # Poll for fill
    filled_qty = None
    for _ in range(15):  # 15 * 2s = 30s max
        order = client.get_order_by_id(order_id)
        if str(order.status) in ("filled", "OrderStatus.FILLED"):
            filled_qty = float(order.filled_qty)
            break
        time.sleep(2)

    if filled_qty is None or filled_qty <= 0:
        logger.warning(f"Order {order_id} did not fill within 30s — trailing stop not attached")
        return

    stop_request = TrailingStopOrderRequest(
        symbol=symbol,
        qty=filled_qty,
        side=OrderSide.SELL,
        trail_percent=trail_percent,
        time_in_force=TimeInForce.GTC,
    )
    stop_order = client.submit_order(stop_request)
    logger.info(f"Trailing stop attached for {symbol}: {trail_percent}% — stop order id: {stop_order.id}")


def update_portfolio_history() -> None:
    """Append today's portfolio snapshot to portfolio_history.csv."""
    portfolio = get_portfolio_value()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    holdings = ",".join(portfolio["positions"].keys()) if portfolio["positions"] else "CASH"

    file_exists = os.path.exists(config.PORTFOLIO_HISTORY_FILE)
    with open(config.PORTFOLIO_HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "total_value", "cash", "holdings"])
        writer.writerow([date_str, round(portfolio["total_value"], 2), round(portfolio["cash"], 2), holdings])

    logger.info(f"Portfolio history updated: ${portfolio['total_value']:.2f} total, ${portfolio['cash']:.2f} cash")
