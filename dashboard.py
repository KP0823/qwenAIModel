import json
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config

st.set_page_config(page_title="AI Macro Agent", page_icon="📈", layout="wide")
st.title("Autonomous AI Macro-Strategy Agent")

tab1, tab2, tab3 = st.tabs(["Portfolio", "Brain Feed", "Market Sensors"])


# ---------------------------------------------------------------------------
# Tab 1: Portfolio
# ---------------------------------------------------------------------------
with tab1:
    st.subheader("Equity Curve")
    if Path(config.PORTFOLIO_HISTORY_FILE).exists():
        df = pd.read_csv(config.PORTFOLIO_HISTORY_FILE)
        if not df.empty:
            fig_equity = px.line(
                df, x="date", y="total_value",
                labels={"total_value": "Portfolio Value ($)", "date": "Date"},
                title="Portfolio Equity Over Time",
            )
            fig_equity.update_traces(line_color="#00c896")
            fig_equity.add_hline(y=50.0, line_dash="dot", line_color="gray",
                                 annotation_text="Starting $50")
            st.plotly_chart(fig_equity, use_container_width=True)

            latest = df.iloc[-1]
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Value", f"${float(latest['total_value']):.2f}")
            col2.metric("Cash", f"${float(latest['cash']):.2f}")
            pnl = float(latest["total_value"]) - 50.0
            col3.metric("P&L vs $50 Start", f"${pnl:+.2f}")
        else:
            st.info("No portfolio history yet. Run the pipeline first.")
    else:
        st.info("portfolio_history.csv not found. Run main.py to generate data.")

    st.subheader("Current Allocation")
    try:
        import broker
        positions = broker.get_positions()
        if positions:
            labels = list(positions.keys())
            values = [p["market_value"] for p in positions.values()]
            fig_donut = go.Figure(data=[go.Pie(
                labels=labels, values=values, hole=0.45,
                textinfo="label+percent",
            )])
            fig_donut.update_layout(title="ETF Allocation (Live)", showlegend=True)
            st.plotly_chart(fig_donut, use_container_width=True)
        else:
            st.info("No open positions — fully in cash.")
    except Exception as e:
        st.warning(f"Could not fetch live positions from Alpaca: {e}")
        st.info("Showing last known state from portfolio_history.csv")


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
            for trade in reversed(trades):
                ts = trade.get("timestamp", "")[:19].replace("T", " ")
                action = trade.get("action", "?")
                ticker = trade.get("ticker", "?")
                amount = trade.get("amount_usd", 0.0)
                icon = action_colors.get(action, "⚪")
                label = f"{icon} {ts} | **{action}** {ticker} ${amount:.2f}"

                with st.expander(label):
                    st.markdown(f"**Reasoning:** {trade.get('reasoning', 'N/A')}")
                    safety = trade.get("safety_applied", "none")
                    if safety != "none":
                        st.warning(f"Safety guardrail applied: `{safety}`")
                    think = trade.get("think_reasoning", "")
                    if think:
                        st.markdown("**Internal Monologue (`<think>`):**")
                        st.text_area("", value=think, height=200, key=trade.get("id", ts), disabled=True)
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
                    "200-Day MA": ma,
                    "Signal": data.get("signal", "N/A"),
                })
            if etf_rows:
                st.dataframe(pd.DataFrame(etf_rows), use_container_width=True, hide_index=True)

            # Headlines
            col_l, col_r = st.columns(2)
            with col_l:
                st.subheader("RSS Headlines")
                for h in state.get("rss_headlines", [])[:8]:
                    published = h.get("published", "")[:16] if h.get("published") else ""
                    processed = h.get("processed_at", "")[:10] if h.get("processed_at") else ""
                    date_info = f" `pub: {published}`" if published else ""
                    date_info += f" `fetched: {processed}`" if processed else ""
                    st.markdown(f"- **[{h['source']}]**{date_info} {h['title']}")

            with col_r:
                st.subheader("Reddit Pulse")
                posts = sorted(state.get("reddit_posts", []), key=lambda x: x.get("score", 0), reverse=True)[:8]
                for p in posts:
                    st.markdown(f"- **r/{p['subreddit']}** (↑{p['score']}) {p['title']}")
        else:
            st.info("No sensor data yet. Run main.py to populate.")
    else:
        st.info("system_state.json not found.")

# ---------------------------------------------------------------------------
# Auto-refresh every 60 seconds
# ---------------------------------------------------------------------------
time.sleep(60)
st.rerun()
