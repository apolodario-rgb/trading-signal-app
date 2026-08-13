import difflib
import json
import yfinance as yf
import requests
import os
import csv
from datetime import datetime
import smtplib
from email.mime.text import MIMEText

api_key = os.environ.get("ANTHROPIC_API_KEY")
gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
my_email = "apolodario@gmail.com"

tickers = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA", "AMZN", "META", "AMD", "PLTR", "COIN"]

# ---- Timeframe setup ----
# The same script runs on 4 different schedules (5min, 30min, 1hr, 2hr), set via the
# TIMEFRAME env var in the workflow. Each timeframe gets its OWN portfolio/log files so
# we can compare, side by side later, which speed actually performs best.
TIMEFRAME = os.environ.get("TIMEFRAME", "30MIN")

# The 5-minute run skips the news/Reddit AI classification steps. Running the full
# Claude-powered pipeline (news + reddit sentiment) across 10 tickers every 5 minutes
# would mean 100+ API calls every single run, which gets expensive and risks rate limits
# fast. So the 5-minute bot trades on price/RSI/MACD/Bollinger/StockTwits only (all free,
# fast data), while the slower timeframes keep the full pipeline. This also mirrors how
# real fast trading works: quick reactions to price, slower reactions that digest news.
LITE_MODE = (TIMEFRAME == "5MIN")

# ---- Paper trading (simulated money) config ----
# The bot "invests" imaginary money whenever it gets a strong signal, so we can track
# over time whether the strategy actually would have made or lost money.
STARTING_CASH = 10000.0
POSITION_SIZE = 1000.0  # dollar amount the bot "spends" per buy signal
PORTFOLIO_STATE_FILE = "portfolio_state_" + TIMEFRAME + ".json"
PAPER_TRADES_FILE = "paper_trades_" + TIMEFRAME + ".csv"
PORTFOLIO_HISTORY_FILE = "portfolio_history_" + TIMEFRAME + ".csv"

if os.path.isfile(PORTFOLIO_STATE_FILE):
    with open(PORTFOLIO_STATE_FILE, "r") as f:
        portfolio = json.load(f)
else:
    portfolio = {"cash": STARTING_CASH, "positions": {}}  # positions: {symbol: {"shares": x, "avg_price": y}}

latest_prices = {}  # symbol -> latest price, collected for every ticker so we can value the whole portfolio later

# Reliability weight per news source. Well-established financial outlets get a boost,
# unrecognized/smaller sources get a slight discount. Default (unlisted source) is 1.0.
SOURCE_RELIABILITY = {
    "REUTERS": 1.3,
    "BLOOMBERG": 1.3,
    "WALL STREET JOURNAL": 1.3,
    "ASSOCIATED PRESS": 1.2,
    "AP NEWS": 1.2,
    "CNBC": 1.15,
    "MARKETWATCH": 1.1,
    "BARRON'S": 1.1,
    "YAHOO FINANCE": 1.0,
    "MOTLEY FOOL": 0.8,
    "SEEKING ALPHA": 0.8,
    "BENZINGA": 0.8,
    "ZACKS": 0.8,
}
DEFAULT_SOURCE_RELIABILITY = 0.9

alert_messages = []
stock_facts_for_email = []

for symbol in tickers:
    stock = yf.Ticker(symbol)
    data = stock.history(period="6mo")
    data['Average_5day'] = data['Close'].rolling(window=5).mean()

    delta = data['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    data['RSI'] = 100 - (100 / (1 + rs))

    # MACD: momentum/trend-strength indicator (12-day EMA vs 26-day EMA, plus a 9-day signal line)
    data['EMA12'] = data['Close'].ewm(span=12, adjust=False).mean()
    data['EMA26'] = data['Close'].ewm(span=26, adjust=False).mean()
    data['MACD'] = data['EMA12'] - data['EMA26']
    data['MACD_signal'] = data['MACD'].ewm(span=9, adjust=False).mean()

    # Bollinger Bands: 20-day average with upper/lower bands 2 standard deviations away
    data['BB_middle'] = data['Close'].rolling(window=20).mean()
    data['BB_std'] = data['Close'].rolling(window=20).std()
    data['BB_upper'] = data['BB_middle'] + (2 * data['BB_std'])
    data['BB_lower'] = data['BB_middle'] - (2 * data['BB_std'])

    latest_price = data['Close'].iloc[-1]
    latest_average = data['Average_5day'].iloc[-1]
    latest_rsi = data['RSI'].iloc[-1]
    latest_macd = data['MACD'].iloc[-1]
    latest_macd_signal = data['MACD_signal'].iloc[-1]
    latest_bb_upper = data['BB_upper'].iloc[-1]
    latest_bb_lower = data['BB_lower'].iloc[-1]
    latest_bb_middle = data['BB_middle'].iloc[-1]
    latest_prices[symbol] = latest_price

    # Regime check: is the market trending or choppy/ranging right now?
    # Compare the 20-day average now vs 10 trading days ago. A big % move = trending, a small move = choppy.
    bb_middle_10ago = data['BB_middle'].iloc[-10]
    trend_strength = abs(latest_bb_middle - bb_middle_10ago) / bb_middle_10ago
    is_trending = trend_strength > 0.03

    buy_votes = 0
    sell_votes = 0

    if latest_price > latest_average:
        buy_votes += 1
    else:
        sell_votes += 1

    if latest_rsi < 30:
        buy_votes += 1
    elif latest_rsi > 70:
        sell_votes += 1

    # MACD only gets a vote when the market is trending (it's a trend-following signal,
    # it gives false signals in choppy sideways markets)
    if is_trending:
        if latest_macd > latest_macd_signal:
            buy_votes += 1
        else:
            sell_votes += 1

    # Bollinger Bands only get a vote when the market is choppy/ranging (it's a mean-reversion
    # signal: price near the edge of its normal range, betting it snaps back toward the middle.
    # In a strong trend, price can "walk the band" for a long time, so this would be misleading then)
    if not is_trending:
        if latest_price <= latest_bb_lower:
            buy_votes += 1
        elif latest_price >= latest_bb_upper:
            sell_votes += 1

    positive_count = 0
    negative_count = 0
    positive_score = 0
    negative_score = 0
    headline_titles = []
    duplicate_count = 0
    tagged_headlines = []

    news = stock.news if not LITE_MODE else []
    for item in news[:5]:
        content = item.get('content', item)
        title = content.get('title', 'No title')
        publisher = content.get('provider', {}).get('displayName', 'Unknown source') if isinstance(content.get('provider'), dict) else content.get('publisher', 'Unknown source')
        reliability = SOURCE_RELIABILITY.get(publisher.upper(), DEFAULT_SOURCE_RELIABILITY)

        # Skip this headline if it's basically the same story as one we already scored
        # (different outlets often report the exact same news with slightly different wording)
        is_duplicate = False
        for seen_title in headline_titles:
            similarity = difflib.SequenceMatcher(None, title.lower(), seen_title.lower()).ratio()
            if similarity > 0.6:
                is_duplicate = True
                break

        if is_duplicate:
            duplicate_count += 1
            continue

        headline_titles.append(title)

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 20,
                "messages": [
                    {"role": "user", "content": "Headline: \"" + title + "\". For " + symbol + " stock, reply with ONLY three things separated by spaces: the sentiment (POSITIVE, NEGATIVE, or NEUTRAL), a strength number from 1 to 3 (1=mildly relevant/minor, 3=major market-moving news), and an event category (one of: EARNINGS, ACQUISITION, REGULATION, PRODUCT, LEADERSHIP, LEGAL, OTHER). Example reply: NEGATIVE 3 REGULATION"}
                ]
            }
        )
        result = response.json()
        if 'content' not in result:
            continue
        verdict = result['content'][0]['text'].strip().upper()
        parts = verdict.split()
        sentiment_word = parts[0] if len(parts) > 0 else "NEUTRAL"
        try:
            strength = int(parts[1]) if len(parts) > 1 else 1
        except ValueError:
            strength = 1
        strength = max(1, min(3, strength))
        event_category = parts[2] if len(parts) > 2 else "OTHER"

        # Earnings, acquisitions, and regulatory news tend to move prices more than routine coverage,
        # so nudge the strength up a notch for those categories if it wasn't already rated as major
        if event_category in ("EARNINGS", "ACQUISITION", "REGULATION"):
            strength = max(strength, 2)

        # Weight the strength by how reliable this news source is
        weighted_strength = strength * reliability

        tagged_headlines.append(title + " [" + event_category + ", " + publisher + "]")

        if "POSITIVE" in sentiment_word:
            positive_count += 1
            positive_score += weighted_strength
        elif "NEGATIVE" in sentiment_word:
            negative_count += 1
            negative_score += weighted_strength

    if positive_score > negative_score:
        buy_votes += 1
    elif negative_score > positive_score:
        sell_votes += 1

    bullish_count = 0
    bearish_count = 0

    try:
        st_response = requests.get(
            "https://api.stocktwits.com/api/2/streams/symbol/" + symbol + ".json",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        st_data = st_response.json()
        messages = st_data.get("messages", [])
        for msg_item in messages[:30]:
            sentiment_obj = msg_item.get("entities", {}).get("sentiment")
            if sentiment_obj:
                basic = sentiment_obj.get("basic")
                if basic == "Bullish":
                    bullish_count += 1
                elif basic == "Bearish":
                    bearish_count += 1
    except Exception as e:
        print("StockTwits fetch failed for " + symbol + ": " + str(e))

    if bullish_count > bearish_count:
        buy_votes += 1
    elif bearish_count > bullish_count:
        sell_votes += 1

    # Reddit sentiment: pull recent post titles mentioning this stock from r/stocks, have Claude
    # classify them the same way as news headlines. Uses Reddit's public read-only JSON endpoint,
    # no login/API key needed, same as StockTwits above.
    reddit_positive = 0
    reddit_negative = 0

    if not LITE_MODE:
        try:
            reddit_response = requests.get(
                "https://www.reddit.com/r/stocks/search.json",
                params={"q": symbol, "restrict_sr": "on", "sort": "new", "limit": 10, "t": "week"},
                timeout=10,
                headers={"User-Agent": "trading-signal-bot/1.0"}
            )
            reddit_data = reddit_response.json()
            posts = reddit_data.get("data", {}).get("children", [])
            for post in posts[:8]:
                post_title = post.get("data", {}).get("title", "")
                if not post_title:
                    continue
                reddit_verdict_response = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": "claude-sonnet-4-5",
                        "max_tokens": 10,
                        "messages": [
                            {"role": "user", "content": "Reddit post title: \"" + post_title + "\". For " + symbol + " stock, reply with ONLY one word: POSITIVE, NEGATIVE, or NEUTRAL."}
                        ]
                    }
                )
                reddit_result = reddit_verdict_response.json()
                if 'content' not in reddit_result:
                    continue
                reddit_verdict = reddit_result['content'][0]['text'].strip().upper()
                if "POSITIVE" in reddit_verdict:
                    reddit_positive += 1
                elif "NEGATIVE" in reddit_verdict:
                    reddit_negative += 1
        except Exception as e:
            print("Reddit fetch failed for " + symbol + ": " + str(e))

    if reddit_positive > reddit_negative:
        buy_votes += 1
    elif reddit_negative > reddit_positive:
        sell_votes += 1

    # Macro calendar check: is a Fed interest rate decision coming up soon? We can't predict which
    # way the Fed will go, so this doesn't cast a vote, it just adds a caution flag so the email
    # can warn that a big rate announcement could overwhelm the technical/news signals this week.
    fomc_dates_2026 = [
        datetime(2026, 1, 28), datetime(2026, 3, 18), datetime(2026, 4, 29),
        datetime(2026, 6, 17), datetime(2026, 7, 29), datetime(2026, 9, 16),
        datetime(2026, 10, 28), datetime(2026, 12, 9)
    ]
    days_to_next_fomc = min([(d - datetime.now()).days for d in fomc_dates_2026 if d >= datetime.now()], default=None)
    fomc_soon = days_to_next_fomc is not None and days_to_next_fomc <= 5

    if buy_votes >= 2:
        final = "STRONG BUY"
    elif sell_votes >= 2:
        final = "STRONG SELL"
    else:
        final = "HOLD / mixed"

    regime_label = "TRENDING" if is_trending else "CHOPPY"
    fomc_note = (" | FOMC in " + str(days_to_next_fomc) + "d") if fomc_soon else ""
    print("[" + TIMEFRAME + "] " + symbol + ": " + final + " (price: $" + str(round(latest_price, 2)) + ", RSI: " + str(round(latest_rsi, 1)) + ", regime: " + regime_label + ", news: " + str(positive_count) + "+/" + str(negative_count) + "- (score " + str(round(positive_score, 1)) + "/" + str(round(negative_score, 1)) + ", " + str(duplicate_count) + " duplicates skipped), stocktwits: " + str(bullish_count) + "bull/" + str(bearish_count) + "bear, reddit: " + str(reddit_positive) + "+/" + str(reddit_negative) + "-, votes: " + str(buy_votes) + "buy/" + str(sell_votes) + "sell" + fomc_note + (" [LITE MODE]" if LITE_MODE else "") + ")")

    paper_trade_note = None

    if final == "STRONG BUY":
        already_holding = symbol in portfolio["positions"] and portfolio["positions"][symbol]["shares"] > 0
        if not already_holding and portfolio["cash"] >= POSITION_SIZE:
            shares_bought = POSITION_SIZE / latest_price
            portfolio["cash"] -= POSITION_SIZE
            portfolio["positions"][symbol] = {"shares": shares_bought, "avg_price": latest_price}
            paper_trade_note = "BUY " + str(round(shares_bought, 4)) + " shares at $" + str(round(latest_price, 2))
        elif already_holding:
            paper_trade_note = "already holding, no new buy"
        else:
            paper_trade_note = "not enough paper cash to buy"

    elif final == "STRONG SELL":
        if symbol in portfolio["positions"] and portfolio["positions"][symbol]["shares"] > 0:
            held = portfolio["positions"][symbol]
            proceeds = held["shares"] * latest_price
            profit = proceeds - (held["shares"] * held["avg_price"])
            portfolio["cash"] += proceeds
            paper_trade_note = "SELL " + str(round(held["shares"], 4)) + " shares at $" + str(round(latest_price, 2)) + " (P/L: $" + str(round(profit, 2)) + ")"
            del portfolio["positions"][symbol]
        else:
            paper_trade_note = "no position to sell"

    if paper_trade_note and ("BUY " in paper_trade_note or "SELL " in paper_trade_note):
        trades_file_exists = os.path.isfile(PAPER_TRADES_FILE)
        with open(PAPER_TRADES_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not trades_file_exists:
                writer.writerow(["date", "symbol", "action", "price", "note", "cash_after"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), symbol, final, round(latest_price, 2), paper_trade_note, round(portfolio["cash"], 2)])

    if "STRONG" in final:
        price_rounded = str(round(latest_price, 2))
        average_rounded = str(round(latest_average, 2))
        rsi_rounded = str(round(latest_rsi, 1))

        log_file = "signal_log_" + TIMEFRAME + ".csv"
        file_exists = os.path.isfile(log_file)

        with open(log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "symbol", "signal", "price", "average_5day", "rsi", "regime", "macd", "macd_signal", "bb_upper", "bb_lower", "positive_news", "negative_news", "positive_score", "negative_score", "duplicates_skipped", "bullish", "bearish", "reddit_positive", "reddit_negative", "days_to_fomc", "buy_votes", "sell_votes"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), symbol, final, round(latest_price, 2), round(latest_average, 2), round(latest_rsi, 1), regime_label, round(latest_macd, 3), round(latest_macd_signal, 3), round(latest_bb_upper, 2), round(latest_bb_lower, 2), positive_count, negative_count, round(positive_score, 1), round(negative_score, 1), duplicate_count, bullish_count, bearish_count, reddit_positive, reddit_negative, days_to_next_fomc, buy_votes, sell_votes])

        stock_facts_for_email.append({
            "symbol": symbol,
            "final": final,
            "price": price_rounded,
            "average": average_rounded,
            "rsi": rsi_rounded,
            "regime": regime_label,
            "positive_news": positive_count,
            "negative_news": negative_count,
            "positive_score": positive_score,
            "negative_score": negative_score,
            "duplicates": duplicate_count,
            "bullish": bullish_count,
            "bearish": bearish_count,
            "reddit_positive": reddit_positive,
            "reddit_negative": reddit_negative,
            "days_to_fomc": days_to_next_fomc,
            "buy_votes": buy_votes,
            "sell_votes": sell_votes,
            "headlines": tagged_headlines,
            "paper_trade": paper_trade_note
        })

# ---- Save paper portfolio state and record a snapshot of its total value this run ----
positions_value = 0.0
for held_symbol, held_info in portfolio["positions"].items():
    price_now = latest_prices.get(held_symbol, held_info["avg_price"])  # fall back to buy price if we didn't refresh it this run
    positions_value += held_info["shares"] * price_now

total_portfolio_value = portfolio["cash"] + positions_value
total_return_pct = ((total_portfolio_value - STARTING_CASH) / STARTING_CASH) * 100

with open(PORTFOLIO_STATE_FILE, "w") as f:
    json.dump(portfolio, f, indent=2)

history_file_exists = os.path.isfile(PORTFOLIO_HISTORY_FILE)
with open(PORTFOLIO_HISTORY_FILE, "a", newline="") as f:
    writer = csv.writer(f)
    if not history_file_exists:
        writer.writerow(["date", "cash", "positions_value", "total_value", "return_pct", "num_positions"])
    writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), round(portfolio["cash"], 2), round(positions_value, 2), round(total_portfolio_value, 2), round(total_return_pct, 2), len(portfolio["positions"])])

print("[" + TIMEFRAME + "] Paper portfolio: $" + str(round(total_portfolio_value, 2)) + " total (" + str(round(total_return_pct, 2)) + "% return), cash: $" + str(round(portfolio["cash"], 2)) + ", " + str(len(portfolio["positions"])) + " open position(s)")

# Only the 30-minute run sends an email digest, so the inbox doesn't get hit every 5
# minutes by four parallel bots. The other timeframes still log every trade and track
# their own portfolio silently - check the CSV files in the repo to compare them.
SEND_EMAIL = (TIMEFRAME == "30MIN")

if SEND_EMAIL and len(stock_facts_for_email) > 0:

    facts_text = "PAPER PORTFOLIO (simulated money, started at $" + str(int(STARTING_CASH)) + "):\n"
    facts_text += "- Total value right now: $" + str(round(total_portfolio_value, 2)) + " (" + ("+" if total_return_pct >= 0 else "") + str(round(total_return_pct, 2)) + "% since start)\n"
    facts_text += "- Cash on hand: $" + str(round(portfolio["cash"], 2)) + ", open positions: " + str(len(portfolio["positions"])) + "\n"
    for f in stock_facts_for_email:
        facts_text += "\n" + f["symbol"] + ":\n"
        facts_text += "- Signal: " + f["final"] + "\n"
        facts_text += "- Current price: $" + f["price"] + ", 5-day average: $" + f["average"] + "\n"
        facts_text += "- RSI: " + f["rsi"] + " (below 30 = oversold, above 70 = overbought)\n"
        facts_text += "- Market regime right now: " + f["regime"] + " (trending = strong directional move, choppy = bouncing sideways)\n"
        facts_text += "- News sentiment: " + str(f["positive_news"]) + " positive, " + str(f["negative_news"]) + " negative headlines (weighted by how major each story is AND how reliable its source is: positive strength total " + str(round(f["positive_score"], 1)) + ", negative strength total " + str(round(f["negative_score"], 1)) + "; " + str(f["duplicates"]) + " duplicate/re-reported stories were filtered out so they didn't get double-counted)\n"
        facts_text += "- StockTwits trader sentiment: " + str(f["bullish"]) + " bullish, " + str(f["bearish"]) + " bearish posts\n"
        facts_text += "- Reddit sentiment (r/stocks, past week): " + str(f["reddit_positive"]) + " positive, " + str(f["reddit_negative"]) + " negative posts\n"
        if f["days_to_fomc"] is not None and f["days_to_fomc"] <= 5:
            facts_text += "- HEADS UP: a Federal Reserve interest rate decision is coming in " + str(f["days_to_fomc"]) + " day(s). This can move the whole market regardless of this stock's own signals.\n"
        facts_text += "- Votes: " + str(f["buy_votes"]) + " buy, " + str(f["sell_votes"]) + " sell (combining price trend, RSI, MACD or Bollinger Bands depending on market regime, news, StockTwits, and Reddit)\n"
        facts_text += "- Sample headlines (tagged with news category and source): " + " | ".join(f["headlines"][:3]) + "\n"
        if f["paper_trade"]:
            facts_text += "- Paper trading action taken: " + f["paper_trade"] + "\n"

    prompt_text = "You are a friendly, upbeat trading analyst writing a daily email to a friend who is a complete beginner learning to trade. "
    prompt_text += "Here is today's raw data for stocks with strong signals:\n" + facts_text + "\n"
    prompt_text += "Start with a brief 1-2 sentence mention of the paper portfolio's total value and return since starting, using the PAPER PORTFOLIO data given, framed clearly as simulated/imaginary money, not real trading results. "
    prompt_text += "Write an engaging, warm, conversational email. For EACH stock, write a short paragraph (3-4 sentences) explaining in plain English what's happening and why, using the price/RSI/news/StockTwits/Reddit data given, and mention the paper trading action taken if one was made. If there's a Fed meeting heads-up, mention it briefly as a reason for extra caution. "
    prompt_text += "Avoid dry lists of numbers, weave the numbers naturally into sentences. Use light personality and enthusiasm, but stay accurate to the data, never invent facts not given. "
    prompt_text += "End the whole email with a section called 'Today's Trading Lesson' that picks ONE concept present in today's data (like RSI, news sentiment, moving averages, MACD, Bollinger Bands, trending vs choppy markets, why news source reliability matters, social media sentiment like Reddit, or how Fed interest rate decisions affect markets) and explains it simply in 3-4 sentences, like a mini trading lesson for a beginner. "
    prompt_text += "Do not give direct financial advice like 'you should buy this'. Keep the total email under 350 words. Do not use markdown formatting, this is a plain text email."

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 1000,
            "messages": [
                {"role": "user", "content": prompt_text}
            ]
        }
    )
    result = response.json()

    if 'content' in result:
        email_body = result['content'][0]['text']
    else:
        email_body = "\n\n".join([f["symbol"] + ": " + f["final"] + " at $" + f["price"] for f in stock_facts_for_email])

    msg = MIMEText(email_body)
    msg['Subject'] = "Trading Bot (" + TIMEFRAME + "): " + str(len(stock_facts_for_email)) + " signal(s) today"
    msg['From'] = my_email
    msg['To'] = my_email

    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(my_email, gmail_password)
    server.sendmail(my_email, my_email, msg.as_string())
    server.quit()
    print("Email sent!")
elif len(stock_facts_for_email) == 0:
    print("[" + TIMEFRAME + "] No strong signals this run, no email sent.")
else:
    print("[" + TIMEFRAME + "] " + str(len(stock_facts_for_email)) + " signal(s) logged silently (only the 30-minute run emails).")
