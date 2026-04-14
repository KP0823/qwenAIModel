import json
import time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import config

# ---------------------------------------------------------------------------
# Benchmark data — cached 30 min for ~live delayed prices
# ---------------------------------------------------------------------------
BENCHMARKS = {
    "S&P 500":      "^GSPC",
    "NASDAQ":       "^IXIC",
    "Russell 2000": "^RUT",
}
BENCH_COLORS = {
    "S&P 500":      "#4361ee",   # royal blue
    "NASDAQ":       "#f72585",   # hot pink
    "Russell 2000": "#ff9f1c",   # amber
}


@st.cache_data(ttl=1800)
def _fetch_benchmarks(start_date: str) -> pd.DataFrame:
    """Download daily closes for all benchmarks from start_date to today (15-20 min delayed)."""
    frames = {}
    for name, ticker in BENCHMARKS.items():
        try:
            hist = yf.Ticker(ticker).history(start=start_date)
            if not hist.empty:
                frames[name] = hist["Close"]
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index).normalize()
    return df

st.set_page_config(page_title="AI Macro Agent", page_icon="📈", layout="wide")
st.title("Autonomous AI Macro-Strategy Agent")

tab1, tab2, tab3 = st.tabs(["Portfolio", "Brain Feed", "Market Sensors"])


# ---------------------------------------------------------------------------
# Tab 1: Portfolio
# ---------------------------------------------------------------------------
with tab1:

    # ── Load data once ──────────────────────────────────────────────────────
    start = config.MAX_PORTFOLIO_USD
    port_df = None
    if Path(config.PORTFOLIO_HISTORY_FILE).exists():
        _raw = pd.read_csv(config.PORTFOLIO_HISTORY_FILE)
        if not _raw.empty:
            port_df = _raw

    try:
        import broker
        portfolio   = broker.get_portfolio_value()
        positions   = portfolio["positions"]
        cash        = portfolio["cash"]
        total_etf   = sum(p["market_value"] for p in positions.values())
        broker_ok   = True
    except Exception:
        portfolio = positions = None
        cash = total_etf = 0.0
        broker_ok = False

    daily_port  = None
    bench_df    = pd.DataFrame()
    ai_pct      = None
    if port_df is not None:
        port_df["datetime"] = pd.to_datetime(port_df["date"])
        port_df["day"]      = port_df["datetime"].dt.normalize()
        daily_port          = port_df.groupby("day")["total_value"].last().reset_index()
        daily_port.columns  = ["date", "portfolio_value"]
        daily_port["pct_return"] = (daily_port["portfolio_value"] / start - 1) * 100
        bench_df   = _fetch_benchmarks(daily_port["date"].iloc[0].strftime("%Y-%m-%d"))
        ai_pct     = daily_port["pct_return"].iloc[-1]

    # ── KPI strip ───────────────────────────────────────────────────────────
    if port_df is not None:
        latest  = port_df.iloc[-1]
        tv      = float(latest["total_value"])
        c       = float(latest["cash"])
        pnl     = tv - start
        pnl_pct = (tv / start - 1) * 100
    else:
        tv = c = pnl = pnl_pct = None

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Portfolio Value",  f"${tv:,.2f}"          if tv   is not None else "—",
              f"${pnl:+,.2f} all-time"                   if pnl  is not None else None)
    k2.metric("All-Time P&L",     f"{pnl_pct:+.2f}%"     if pnl_pct is not None else "—",
              f"${pnl:+,.2f}"                             if pnl  is not None else None)
    k3.metric("Cash",             f"${c:,.2f}"            if c    is not None else "—")
    k4.metric("In Market",        f"${tv - c:,.2f}"       if tv is not None and c is not None else "—")

    st.divider()

    # ── Equity curve (left) + Allocation donut (right) ──────────────────────
    col_equity, col_alloc = st.columns([3, 2], gap="large")

    with col_equity:
        st.markdown("#### Equity Curve")
        if port_df is not None:
            _up      = port_df["total_value"].iloc[-1] >= start
            _lc      = "#00c896" if _up else "#ff5050"
            _fill    = "rgba(0,200,150,0.10)" if _up else "rgba(255,80,80,0.10)"

            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(
                x=port_df["date"], y=port_df["total_value"],
                fill="tozeroy", fillcolor=_fill,
                line=dict(color=_lc, width=2.5),
                mode="lines",
                hovertemplate="$%{y:,.2f}<extra></extra>",
            ))
            fig_eq.add_hline(
                y=start, line_dash="dot", line_color="rgba(160,160,160,0.5)",
                annotation_text=f"Baseline ${start:,.0f}",
                annotation_position="bottom right",
            )
            fig_eq.update_layout(
                height=350, showlegend=False, hovermode="x unified",
                margin=dict(l=0, r=10, t=10, b=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(tickprefix="$", tickformat=",.0f",
                           showgrid=True, gridcolor="rgba(128,128,128,0.10)"),
                xaxis=dict(showgrid=False),
            )
            st.plotly_chart(fig_eq, use_container_width=True)
        else:
            st.info("Run main.py to generate portfolio history.")

    with col_alloc:
        st.markdown("#### Portfolio Allocation")
        if broker_ok:
            _etf_palette = [
                "#00c896", "#ff9f1c", "#f72585", "#4361ee", "#7209b7",
                "#3a0ca3", "#4cc9f0", "#f3722c", "#90be6d", "#43aa8b",
            ]
            _labels = (["Cash"] if cash > 0 else []) + list((positions or {}).keys())
            _values = ([cash]   if cash > 0 else []) + [p["market_value"] for p in (positions or {}).values()]
            _colors = (["#636EFA"] if cash > 0 else []) + [
                _etf_palette[i % len(_etf_palette)] for i in range(len(positions or {}))
            ]
            if _values:
                fig_alloc = go.Figure(data=[go.Pie(
                    labels=_labels, values=_values,
                    hole=0.52, textinfo="label+percent",
                    marker_colors=_colors, textfont_size=12,
                    hovertemplate="%{label}: $%{value:,.2f}<extra></extra>",
                )])
                fig_alloc.update_layout(
                    height=350, showlegend=False,
                    margin=dict(l=0, r=0, t=10, b=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_alloc, use_container_width=True)
            else:
                st.info("No positions or cash data.")
        else:
            st.warning("Alpaca unavailable — live allocation not shown.")

    st.divider()

    # ── Benchmark comparison ─────────────────────────────────────────────────
    st.markdown("#### Performance vs Benchmarks")
    st.caption("Index prices ~15-20 min delayed · Normalized to portfolio inception date")

    if daily_port is not None:
        fig_bench = go.Figure()
        fig_bench.add_trace(go.Scatter(
            x=daily_port["date"], y=daily_port["pct_return"],
            name="AI Agent",
            line=dict(color="#00c896", width=3),
            mode="lines+markers", marker=dict(size=7, symbol="circle"),
            hovertemplate="AI Agent: %{y:+.2f}%<extra></extra>",
        ))

        _bench_styles = [
            ("S&P 500",      "#4361ee", "diamond"),
            ("NASDAQ",       "#f72585", "square"),
            ("Russell 2000", "#ff9f1c", "triangle-up"),
        ]
        if not bench_df.empty:
            for bname, bcolor, bsymbol in _bench_styles:
                if bname not in bench_df.columns:
                    continue
                _s = bench_df[bname].dropna()
                if _s.empty:
                    continue
                _pct = (_s / _s.iloc[0] - 1) * 100
                fig_bench.add_trace(go.Scatter(
                    x=_s.index, y=_pct, name=bname,
                    line=dict(color=bcolor, width=1.8, dash="dash"),
                    mode="lines+markers", marker=dict(size=5, symbol=bsymbol),
                    hovertemplate=f"{bname}: %{{y:+.2f}}%<extra></extra>",
                ))

        fig_bench.add_hline(y=0, line_dash="dot", line_color="rgba(150,150,150,0.35)")
        fig_bench.update_layout(
            height=370, hovermode="x unified",
            margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.12)", ticksuffix="%"),
            xaxis=dict(showgrid=False),
        )
        st.plotly_chart(fig_bench, use_container_width=True)

        if not bench_df.empty and ai_pct is not None:
            b1, b2, b3 = st.columns(3)
            for _col, bname in zip([b1, b2, b3], ["S&P 500", "NASDAQ", "Russell 2000"]):
                if bname not in bench_df.columns:
                    _col.metric(f"AI vs {bname}", "N/A"); continue
                _s = bench_df[bname].dropna()
                if _s.empty:
                    _col.metric(f"AI vs {bname}", "N/A"); continue
                _bp    = (_s.iloc[-1] / _s.iloc[0] - 1) * 100
                _delta = ai_pct - _bp
                _col.metric(f"AI vs {bname}", f"{ai_pct:+.2f}%", f"{_delta:+.2f}% vs index")
    else:
        st.info("No portfolio history. Run main.py first.")

    # ── Open Positions table ─────────────────────────────────────────────────
    if broker_ok and positions:
        st.divider()
        st.markdown("#### Open Positions")
        _rows = []
        for sym, p in positions.items():
            _pl_icon = "🟢" if p["unrealized_pl"] >= 0 else "🔴"
            _rows.append({
                "ETF":           sym,
                "Shares":        round(p["qty"], 6),
                "Avg Entry":     f"${p['avg_entry_price']:.2f}",
                "Current Price": f"${p['current_price']:.2f}",
                "Cost Basis":    f"${p['cost_basis']:.2f}",
                "Market Value":  f"${p['market_value']:.2f}",
                "Unrealized P&L": f"{_pl_icon} ${p['unrealized_pl']:+.2f}",
                "% Gain/Loss":   f"{p['unrealized_plpc']:+.2f}%",
            })
        st.dataframe(pd.DataFrame(_rows), width="stretch", hide_index=True)
    elif not broker_ok:
        st.caption("⚠️ Could not connect to Alpaca — live positions unavailable.")


# ---------------------------------------------------------------------------
# Tab 2: Brain Feed
# ---------------------------------------------------------------------------
with tab2:
    st.subheader("AI Decision Timeline")

    if Path(config.TRADE_JOURNAL_FILE).exists():
        try:
            with open(config.TRADE_JOURNAL_FILE) as f:
                trades = json.load(f)
        except Exception:
            trades = []

        if trades:
            action_colors = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}
            # Group by batch_id for display
            seen_batches = set()
            for trade in reversed(trades):
                ts = trade.get("timestamp", "")[:19].replace("T", " ")
                action = trade.get("action", "?")
                ticker = trade.get("ticker", "?")
                amount = trade.get("amount_usd", 0.0)
                batch = trade.get("batch_id", "")
                icon = action_colors.get(action, "⚪")

                # Show batch indicator for multi-action batches
                batch_tag = ""
                if batch and batch not in seen_batches:
                    batch_trades = [t for t in trades if t.get("batch_id") == batch]
                    if len(batch_trades) > 1:
                        batch_tag = f" [batch {batch}: {len(batch_trades)} actions]"
                    seen_batches.add(batch)

                label = f"{icon} {ts} | **{action}** {ticker} ${amount:.2f}{batch_tag}"

                with st.expander(label):
                    st.markdown(f"**Reasoning:** {trade.get('reasoning', 'N/A')}")
                    safety = trade.get("safety_applied", "none")
                    if safety != "none":
                        st.warning(f"Safety guardrail applied: `{safety}`")
                    think = trade.get("think_reasoning", "")
                    if think:
                        st.markdown("**Internal Monologue (`<think>`):**")
                        st.text_area("AI Reasoning", value=think, height=200, key=trade.get("id", ts), disabled=True, label_visibility="collapsed")
                    meta = {k: v for k, v in trade.items() if k not in ("think_reasoning", "reasoning")}
                    st.json(meta)
        else:
            st.info("No trades recorded yet.")
    else:
        st.info("trade_journal.json not found.")


# ---------------------------------------------------------------------------
# Tab 3: Market Sensors
# ---------------------------------------------------------------------------
with tab3:
    if Path(config.SYSTEM_STATE_FILE).exists():
        try:
            with open(config.SYSTEM_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {}

        if state:
            last_sync = state.get("last_sync", "never")
            st.caption(f"Last sync: {last_sync}")

            # API health
            st.subheader("System Health")
            health = state.get("api_health", {})
            cols = st.columns(len(health))
            status_icon = {"ok": "✅", "partial": "⚠️", "error": "❌"}
            for col, (service, status) in zip(cols, health.items()):
                icon = status_icon.get(status, "❓")
                col.metric(service.capitalize(), f"{icon} {status}")

            # ETF table
            st.subheader("ETF Technical Snapshot")
            etf_rows = []
            for symbol, data in state.get("etf_data", {}).items():
                price = f"${data['price']:.2f}" if data.get("price") else "N/A"
                rsi = f"{data['rsi']:.1f}" if data.get("rsi") is not None else "N/A"
                ma = f"${data['ma_200']:.2f}" if data.get("ma_200") else "N/A"
                etf_rows.append({
                    "ETF": symbol,
                    "Price": price,
                    "RSI (14)": rsi,
                    "MACD": data.get("macd", "N/A"),
                    "50-Day MA": f"${data['ma_50']:.2f}" if data.get("ma_50") else "N/A",
                    "200-Day MA": ma,
                    "MA Cross": data.get("ma_cross", "N/A"),
                    "Signal": data.get("signal", "N/A"),
                })
            if etf_rows:
                st.dataframe(pd.DataFrame(etf_rows), width="stretch", hide_index=True)

            # Headlines — 2x2 grid
            col_tl, col_tr = st.columns(2)
            with col_tl:
                st.subheader("RSS Headlines")
                for h in state.get("rss_headlines", [])[:8]:
                    published = h.get("published", "")[:16] if h.get("published") else ""
                    processed = h.get("processed_at", "")[:10] if h.get("processed_at") else ""
                    date_info = f" `pub: {published}`" if published else ""
                    date_info += f" `fetched: {processed}`" if processed else ""
                    st.markdown(f"- **[{h['source']}]**{date_info} {h['title']}")

            with col_tr:
                st.subheader("Alpha Vantage News")
                for a in state.get("alphavantage_news", [])[:8]:
                    sentiment = a.get("sentiment", "")
                    s_icon = {"Bullish": "🟢", "Somewhat-Bullish": "🟢", "Bearish": "🔴",
                              "Somewhat-Bearish": "🔴"}.get(sentiment, "⚪")
                    tickers = ", ".join(a.get("tickers", [])) if a.get("tickers") else ""
                    ticker_tag = f" `{tickers}`" if tickers else ""
                    st.markdown(f"- {s_icon} **[{a['source']}]**{ticker_tag} {a['title']}")
                if not state.get("alphavantage_news"):
                    st.info("No Alpha Vantage data. Set ALPHA_VANTAGE_API_KEY in .env")

            col_bl, col_br = st.columns(2)
            with col_bl:
                st.subheader("NewsAPI Headlines")
                for n in state.get("newsapi_headlines", [])[:8]:
                    published = n.get("published", "")[:16] if n.get("published") else ""
                    date_info = f" `{published}`" if published else ""
                    st.markdown(f"- **[{n['source']}]**{date_info} {n['title']}")
                if not state.get("newsapi_headlines"):
                    st.info("No NewsAPI data. Set NEWS_API_KEY in .env")

            with col_br:
                st.subheader("Reddit Pulse")
                posts = sorted(state.get("reddit_posts", []), key=lambda x: x.get("score", 0), reverse=True)[:8]
                for p in posts:
                    st.markdown(f"- **r/{p['subreddit']}** (↑{p['score']}) {p['title']}")
                if not posts:
                    st.info("No Reddit data. Awaiting API approval.")
        else:
            st.info("No sensor data yet. Run main.py to populate.")
    else:
        st.info("system_state.json not found.")

# ---------------------------------------------------------------------------
# Auto-refresh every 60 seconds
# ---------------------------------------------------------------------------
time.sleep(60)
st.rerun()
