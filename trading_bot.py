import yfinance as yf
import requests
import os

apple = yf.Ticker("AAPL")
data = apple.history(period="1mo")
data['Average_5day'] = data['Close'].rolling(window=5).mean()

latest_price = data['Close'].iloc[-1]
latest_average = data['Average_5day'].iloc[-1]

if latest_price > latest_average:
    signal = "BUY"
else:
    signal = "SELL"

print("Price signal: " + signal)
print("Latest price: $" + str(round(latest_price, 2)))
print("5-day average: $" + str(round(latest_average, 2)))

api_key = os.environ.get("ANTHROPIC_API_KEY")

news = apple.news
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
                {"role": "user", "content": "Headline: \"" + title + "\". Reply with ONLY one word: POSITIVE, NEGATIVE, or NEUTRAL for Apple stock."}
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

print("News sentiment: " + str(positive_count) + " positive, " + str(negative_count) + " negative")

if signal == "BUY" and positive_count > negative_count:
    final = "STRONG BUY"
elif signal == "SELL" and negative_count > positive_count:
    final = "STRONG SELL"
elif signal == "BUY" and negative_count > positive_count:
    final = "CAUTION - mixed signals"
elif signal == "SELL" and positive_count > negative_count:
    final = "CAUTION - mixed signals"
else:
    final = "HOLD"

print("FINAL RECOMMENDATION: " + final)
