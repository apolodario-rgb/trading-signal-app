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

for symbol in tickers:
    stock = yf.Ticker(symbol)
    data = stock.history(period="2mo")
    data['Average_5day'] = data['Close'].rolling(window=5).mean()

    delta = data['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    data['RSI'] = 100 - (100 / (1 + rs))

    latest_price = data['Close'].iloc[-1]
    latest_average = data['Average_5day'].iloc[-1]
    latest_rsi = data['RSI'].iloc[-1]

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

    news = stock.news
    positive_count = 0
    negative_count = 0

    for item in news[:5]:
        content = item.get('content', item)
        title = content.get('title', 'No title')

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

    if buy_votes >= 2:
        final = "STRONG BUY"
    elif sell_votes >= 2:
        final = "STRONG SELL"
    else:
        final = "HOLD / mixed"

    print(symbol + ": " + final + " (price: $" + str(round(latest_price, 2)) + ", RSI: " + str(round(latest_rsi, 1)) + ", news: " + str(positive_count) + "+/" + str(negative_count) + "-, votes: " + str(buy_votes) + "buy/" + str(sell_votes) + "sell)")

    if "STRONG" in final:
        price_rounded = str(round(latest_price, 2))
        average_rounded = str(round(latest_average, 2))
        rsi_rounded = str(round(latest_rsi, 1))

        if final == "STRONG BUY":
            action_text = "Consider BUYING now. Watch to SELL if price rises well above the 5-day average ($" + average_rounded + "), or if it drops back below it."
        else:
            action_text = "Consider SELLING now if holding. Watch to BUY again once price rises back above the 5-day average ($" + average_rounded + ")."

        message = symbol + ": " + final + "\n"
        message += "Current price: $" + price_rounded + "\n"
        message += "5-day average: $" + average_rounded + "\n"
        message += "RSI: " + rsi_rounded + "\n"
        message += "News: " + str(positive_count) + " positive, " + str(negative_count) + " negative headlines\n"
        message += "Votes: " + str(buy_votes) + " buy, " + str(sell_votes) + " sell (out of 3 signals)\n"
        message += action_text
        alert_messages.append(message)

        log_file = "signal_log.csv"
        file_exists = os.path.isfile(log_file)

        with open(log_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "symbol", "signal", "price", "average_5day", "rsi", "buy_votes", "sell_votes"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), symbol, final, round(latest_price, 2), round(latest_average, 2), round(latest_rsi, 1), buy_votes, sell_votes])

if len(alert_messages) > 0:
    body = "\n\n".join(alert_messages)
    msg = MIMEText(body)
    msg['Subject'] = "Trading Bot Alert: " + str(len(alert_messages)) + " strong signal(s)"
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
