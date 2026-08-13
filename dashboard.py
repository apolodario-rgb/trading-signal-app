import json
import os
import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="Trading Signal Bot Dashboard", layout="wide")

TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA", "AMZN", "META", "AMD", "PLTR", "COIN"]
TIMEFRAMES = ["5MIN", "15MIN", "30MIN", "45MIN", "1HR", "2HR"]
STARTING_CASH = 10000.0


def load_portfolio(timeframe):
    """Read the saved portfolio state for one timeframe. Returns None if the bot hasn't
    run yet for this timeframe (file doesn't exist)."""
    path = "portfolio_state_" + timeframe + ".json"
    if not os.path.isfile(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def load_trades(timeframe, limit=10):
    """Read the most recent trades for one timeframe. Returns an empty DataFrame if the
    bot hasn't logged any trades yet."""
    path = "paper_trades_" + timeframe + ".csv"
    if not os.path.isfile(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    return df.tail(limit).iloc[::-1]  # most recent first


@st.cache_data(ttl=60)
def get_live_prices():
    """Pull the current price for every tracked ticker. Cached for 60 seconds so we're not
    hitting Yahoo Finance on every single page interaction."""
    prices = {}
    for symbol in TICKERS:
        try:
            hist = yf.Ticker(symbol).history(period="1d", interval="1m")
            if not hist.empty:
                prices[symbol] = round(float(hist["Close"].iloc[-1]), 2)
            else:
                prices[symbol] = None
        except Exception:
            prices[symbol] = None
    return prices


st.title("📈 Trading Signal Bot Dashboard")
st.caption("Simulated (paper) money only — this tracks whether the strategy would have made money, nothing here is real trading.")

if st.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

# ---- Section 1: how the money is moving, per timeframe ----
st.header("💰 Portfolio value by timeframe")

portfolios = {tf: load_portfolio(tf) for tf in TIMEFRAMES}
live_prices = get_live_prices()

cols = st.columns(len(TIMEFRAMES))
for i, tf in enumerate(TIMEFRAMES):
    with cols[i]:
        portfolio = portfolios[tf]
        if portfolio is None:
            st.metric(tf, "no data yet")
            continue
        positions_value = 0.0
        for symbol, pos in portfolio["positions"].items():
            price = live_prices.get(symbol)
            if price:
                positions_value += pos["shares"] * price
            else:
                positions_value += pos["shares"] * pos["avg_price"]
        total_value = portfolio["cash"] + positions_value
        return_pct = ((total_value - STARTING_CASH) / STARTING_CASH) * 100
        st.metric(
            tf,
            "$" + f"{total_value:,.2f}",
            f"{return_pct:+.2f}%",
        )

st.divider()

# ---- Section 2: current live stock prices ----
st.header("📊 Live stock prices")
price_rows = [{"Ticker": symbol, "Price": ("$" + f"{p:,.2f}") if p else "unavailable"} for symbol, p in live_prices.items()]
st.dataframe(pd.DataFrame(price_rows), hide_index=True, use_container_width=True)

st.divider()

# ---- Section 3: trades happening right now, per timeframe ----
st.header("⚡ Current trades")

for tf in TIMEFRAMES:
    portfolio = portfolios[tf]
    with st.expander(tf + " — open positions & recent trades", expanded=True):
        if portfolio is None:
            st.write("No data yet — this timeframe hasn't run on GitHub Actions yet.")
            continue

        left, right = st.columns(2)

        with left:
            st.subheader("Open positions")
            if portfolio["positions"]:
                rows = []
                for symbol, pos in portfolio["positions"].items():
                    price = live_prices.get(symbol)
                    current_value = pos["shares"] * price if price else None
                    cost_basis = pos["shares"] * pos["avg_price"]
                    unrealized_pct = ((price - pos["avg_price"]) / pos["avg_price"] * 100) if price else None
                    rows.append({
                        "Ticker": symbol,
                        "Shares": round(pos["shares"], 4),
                        "Bought at": "$" + f"{pos['avg_price']:,.2f}",
                        "Current value": ("$" + f"{current_value:,.2f}") if current_value else "n/a",
                        "Unrealized": (f"{unrealized_pct:+.2f}%") if unrealized_pct is not None else "n/a",
                    })
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.write("No open positions right now.")

        with right:
            st.subheader("Recent trades")
            trades_df = load_trades(tf)
            if not trades_df.empty:
                st.dataframe(trades_df, hide_index=True, use_container_width=True)
            else:
                st.write("No trades logged yet.")

st.divider()
st.caption("Data refreshes every 60 seconds when you hit Refresh, or reload the page. Portfolio/trade data comes from the bot's own GitHub Actions runs — this dashboard doesn't run the bot itself.")
