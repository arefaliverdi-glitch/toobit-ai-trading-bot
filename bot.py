import os
import time
import requests
import logging

from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_URL = "https://api.toobit.com"

SYMBOLS = [
    "BTC-SWAP-USDT",
    "ETH-SWAP-USDT",
    "SOL-SWAP-USDT",
    "XRP-SWAP-USDT"
]

INTERVAL = "15m"
SCAN_INTERVAL = 900

subscribers = set()
last_signals = {}

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=15
        )
    except Exception as e:
        logging.error(e)


def get_candles(symbol, interval):

    durations = {
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000
    }

    now = int(time.time() * 1000)

    duration = durations[interval]

    url = f"{BASE_URL}/quote/v1/klines"

    response = requests.get(
        url,
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": now - 100 * duration,
            "endTime": now,
            "limit": 100
        },
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    return [
        {
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5])
        }
        for c in data
    ]


def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def analyze(symbol):

    candles = get_candles(symbol, "15m")
    hourly = get_candles(symbol, "1h")

    if len(candles) < 60 or len(hourly) < 60:
        return None

    closes = [c["close"] for c in candles]
    hourly_closes = [c["close"] for c in hourly]

    price = closes[-2]

    fast = ema(closes[:-1], 9)
    slow = ema(closes[:-1], 20)

    trend_fast = ema(hourly_closes[:-1], 20)
    trend_slow = ema(hourly_closes[:-1], 50)

    if not all([
        fast, slow, trend_fast, trend_slow
    ]):
        return None

    recent = candles[-22:-2]

    atr = sum(
        c["high"] - c["low"] for c in recent
    ) / len(recent)

    if atr <= 0:
        return None

    volume_avg = sum(
        c["volume"] for c in candles[-22:-2]
    ) / 20

    volume = candles[-2]["volume"]

    direction = None

    if fast > slow and trend_fast > trend_slow:
        direction = "LONG"

    elif fast < slow and trend_fast < trend_slow:
        direction = "SHORT"

    if not direction:
        return None

    if volume < volume_avg * 1.2:
        return None

    if direction == "LONG":

        entry = price
        stop = price - atr * 1.5

        tp1 = price + atr * 3
        tp2 = price + atr * 4
        tp3 = price + atr * 5

    else:

        entry = price
        stop = price + atr * 1.5

        tp1 = price - atr * 3
        tp2 = price - atr * 4
        tp3 = price - atr * 5

    volatility = atr / price * 100

    leverage = (
        2 if volatility > 1
        else 3
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "leverage": leverage,
        "time": datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    }


def scan_market():

    results = []

    for symbol in SYMBOLS:

        try:
            signal = analyze(symbol)

            if signal:
                key = (
                    signal["symbol"],
                    signal["direction"]
                )

                if key not in last_signals:

                    last_signals[key] = time.time()
                    results.append(signal)

        except Exception as e:
            logging.error(f"{symbol}: {e}")

    return results


def format_signal(s):

    return f"""
🚨 TOOBIT FUTURES ALERT

COIN: {s['symbol']}

DIRECTION: {s['direction']}

ENTRY: {s['entry']:.6f}

STOP LOSS: {s['stop']:.6f}

TP1: {s['tp1']:.6f}
TP2: {s['tp2']:.6f}
TP3: {s['tp3']:.6f}

SUGGESTED LEVERAGE: {s['leverage']}x

TIMEFRAME: 15M / 1H

STATUS: POTENTIAL SETUP

DATA TIME: {s['time']}

⚠️ Confirm the setup before entering.
This is an automated technical alert, not a guaranteed trade.
"""


def main():

    if not TOKEN:
        raise RuntimeError("Missing Telegram token")

    offset = 0
    last_scan = 0

    logging.info("Toobit scanner started")

    while True:

        try:

            updates = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 10
                },
                timeout=20
            ).json()

            for update in updates.get("result", []):

                offset = update["update_id"] + 1

                message = update.get("message", {})
                chat_id = message.get(
                    "chat", {}
                ).get("id")

                text = message.get("text", "")

                if not chat_id:
                    continue

                if text == "/start":

                    subscribers.add(chat_id)

                    send_message(
                        chat_id,
                        "✅ Toobit AI Scanner connected!\n"
                        "Use /scan to scan the market.\n"
                        "Automatic scanning: Every 15 minutes."
                    )

                elif text == "/scan":

                    send_message(
                        chat_id,
                        "🔍 Scanning Toobit Futures..."
                    )

                    signals = scan_market()

                    if signals:
                        for signal in signals:
                            send_message(
                                chat_id,
                                format_signal(signal)
                            )
                    else:
                        send_message(
                            chat_id,
                            "NO TRADE - No confirmed setup."
                        )

            if time.time() - last_scan >= SCAN_INTERVAL:

                signals = scan_market()

                for signal in signals:

                    for chat_id in subscribers:

                        send_message(
                            chat_id,
                            format_signal(signal)
                        )

                last_scan = time.time()

            time.sleep(2)

        except Exception as e:

            logging.error(f"Main loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
