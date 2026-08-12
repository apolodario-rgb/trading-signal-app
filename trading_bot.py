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

tickers = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]

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

    news = stock.news
    positive_count = 0
    negative_count = 0
    headline_titles = []

    for item in news[:5]:
        content = item.get('content', item)
        title = content.get('title', 'No title')
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
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": "Headline: \"" + title + "\". Reply with ONLY one word: POSITIVE, NEGATIVE, or NEUTRAL for " + symbol + " stock."}
                ]
            }
        )
        result = response.json()
        if 'content' not in result:
            continue
        verdict = result['content'][0]['text'].strip().upper()
        if "POSITIVE" in verdict:
            positive_count += 1
        elif "NEGATIVE" in verdict:
            negative_count += 1

    if positive_count > negative_count:
        buy_votes += 1
    elif negative_count > positive_count:
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

    if buy_votes >= 2:
        final = "STRONG BUY"
    elif sell_votes >= 2:
        final = "STRONG SELL"
    else:
        final = "HOLD / mixed"

    regime_label = "TRENDING" if is_trending else "CHOPPY"
    print(symbol + ": " + final + " (price: $" + str(round(latest_price, 2)) + ", RSI: " + str(round(latest_rsi, 1)) + ", regime: " + regime_label + ", news: " + str(positive_count) + "+/" + str(negative_count) + "-, stocktwits: " + str(bullish_count) + "bull/" + str(bearish_count) + "bear, votes: " + str(buy_votes) + "buy/" + str(sell_votes) + "sell)")

    if "STRONG" in final:
        price_rounded = str(round(latest_price, 2))
        average_rounded = str(round(latest_average, 2))
        rsi_rounded = str(round(latest_rsi, 1))

        log_file = "signal_log.csv"
        file_exists = os.path.isfile(log_file)

        with open(log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "symbol", "signal", "price", "average_5day", "rsi", "regime", "macd", "macd_signal", "bb_upper", "bb_lower", "bullish", "bearish", "buy_votes", "sell_votes"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), symbol, final, round(latest_price, 2), round(latest_average, 2), round(latest_rsi, 1), regime_label, round(latest_macd, 3), round(latest_macd_signal, 3), round(latest_bb_upper, 2), round(latest_bb_lower, 2), bullish_count, bearish_count, buy_votes, sell_votes])

        stock_facts_for_email.append({
            "symbol": symbol,
            "final": final,
            "price": price_rounded,
            "average": average_rounded,
            "rsi": rsi_rounded,
            "regime": regime_label,
            "positive_news": positive_count,
            "negative_news": negative_count,
            "bullish": bullish_count,
            "bearish": bearish_count,
            "buy_votes": buy_votes,
            "sell_votes": sell_votes,
            "headlines": headline_titles
        })

if len(stock_facts_for_email) > 0:

    facts_text = ""
    for f in stock_facts_for_email:
        facts_text += "\n" + f["symbol"] + ":\n"
        facts_text += "- Signal: " + f["final"] + "\n"
        facts_text += "- Current price: $" + f["price"] + ", 5-day average: $" + f["average"] + "\n"
        facts_text += "- RSI: " + f["rsi"] + " (below 30 = oversold, above 70 = overbought)\n"
        facts_text += "- Market regime right now: " + f["regime"] + " (trending = strong directional move, choppy = bouncing sideways)\n"
        facts_text += "- News sentiment: " + str(f["positive_news"]) + " positive, " + str(f["negative_news"]) + " negative headlines\n"
        facts_text += "- StockTwits trader sentiment: " + str(f["bullish"]) + " bullish, " + str(f["bearish"]) + " bearish posts\n"
        facts_text += "- Votes: " + str(f["buy_votes"]) + " buy, " + str(f["sell_votes"]) + " sell (combining price trend, RSI, MACD or Bollinger Bands depending on market regime, news, and StockTwits)\n"
        facts_text += "- Sample headlines: " + " | ".join(f["headlines"][:3]) + "\n"

    prompt_text = "You are a friendly, upbeat trading analyst writing a daily email to a friend who is a complete beginner learning to trade. "
    prompt_text += "Here is today's raw data for stocks with strong signals:\n" + facts_text + "\n"
    prompt_text += "Write an engaging, warm, conversational email. For EACH stock, write a short paragraph (3-4 sentences) explaining in plain English what's happening and why, using the price/RSI/news/StockTwits data given. "
    prompt_text += "Avoid dry lists of numbers, weave the numbers naturally into sentences. Use light personality and enthusiasm, but stay accurate to the data, never invent facts not given. "
    prompt_text += "End the whole email with a section called 'Today's Trading Lesson' that picks ONE concept present in today's data (like RSI, news sentiment, moving averages, MACD, Bollinger Bands, or trending vs choppy markets) and explains it simply in 3-4 sentences, like a mini trading lesson for a beginner. "
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
    msg['Subject'] = "Trading Bot: " + str(len(stock_facts_for_email)) + " signal(s) today"
    msg['From'] = my_email
    msg['To'] = my_email

    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(my_email, gmail_password)
    server.sendmail(my_email, my_email, msg.as_string())
    server.quit()
    print("Email sent!")
else:
    print("No strong signals this run, no email sent.")
