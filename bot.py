import os
import time
import json
import logging
import requests

from datetime import datetime, timezone

# ==========================================
# SETTINGS
# ==========================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_URL = "https://api.toobit.com"

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "20"))

TIMEFRAME = "15m"
TREND_TIMEFRAME = "1h"

KLINE_LIMIT = 180

SUBSCRIBERS_FILE = "subscribers.json"

TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()

subscribers = set()
last_signals = set()

# ==========================================
# SUBSCRIBERS
# ==========================================

def load_subscribers():

    global subscribers

    try:
        with open(SUBSCRIBERS_FILE, "r") as f:
            subscribers = set(json.load(f))

    except Exception:
        subscribers = set()


def save_subscribers():

    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump(list(subscribers), f)

    except Exception as e:
        logging.error(f"Subscriber save error: {e}")


def add_subscriber(chat_id):

    subscribers.add(chat_id)

    save_subscribers()


def remove_subscriber(chat_id):

    subscribers.discard(chat_id)

    save_subscribers()


# ==========================================
# TELEGRAM
# ==========================================

def send_message(chat_id, text):

    try:

        response = session.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        response.raise_for_status()

    except Exception as e:

        logging.error(f"Telegram send error: {e}")


def broadcast(text):

    for chat_id in list(subscribers):

        send_message(chat_id, text)

        time.sleep(0.05)


def telegram_updates(offset):

    try:

        response = session.get(
            f"{TELEGRAM_URL}/getUpdates",
            params={
                "offset": offset,
                "timeout": 25
            },
            timeout=35
        )

        response.raise_for_status()

        return response.json().get("result", [])

    except Exception as e:

        logging.error(f"Telegram update error: {e}")

        return []


# ==========================================
# TOOBIT API
# ==========================================

def api_get(path, params=None):

    url = BASE_URL + path

    response = session.get(
        url,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    return response.json()


# ==========================================
# GET FUTURES SYMBOLS
# ==========================================

def get_symbols():

    try:

        info = api_get("/api/v1/exchangeInfo")

        contracts = info.get("contracts", [])

        valid_symbols = []

        for contract in contracts:

            symbol = contract.get("symbol")

            status = contract.get("status")

            if not symbol:
                continue

            if status != "TRADING":
                continue

            if not symbol.endswith("-SWAP-USDT"):
                continue

            valid_symbols.append(symbol)

        # دریافت حجم معاملات ۲۴ ساعته
        tickers = api_get(
            "/quote/v1/contract/ticker/24hr"
        )

        volume_map = {}

        for ticker in tickers:

            symbol = ticker.get("s")

            try:
                volume = float(ticker.get("qv", 0))
            except Exception:
                volume = 0

            if symbol:
                volume_map[symbol] = volume

        # مرتب‌سازی بر اساس حجم معاملات
        valid_symbols.sort(
            key=lambda s: volume_map.get(s, 0),
            reverse=True
        )

        return valid_symbols[:MAX_SYMBOLS]

    except Exception as e:

        logging.error(f"Symbol error: {e}")

        return []


# ==========================================
# GET CLOSED CANDLES
# ==========================================

def get_klines(symbol, interval):

    try:

        interval_ms = {
            "15m": 15 * 60 * 1000,
            "1h": 60 * 60 * 1000
        }[interval]

        now = int(time.time() * 1000)

        # فقط کندل‌های بسته‌شده
        end_time = (now // interval_ms) * interval_ms - 1

        start_time = end_time - (
            interval_ms * KLINE_LIMIT
        )

        data = api_get(
            "/quote/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
                "limit": KLINE_LIMIT
            }
        )

        candles = []

        for row in data:

            if len(row) < 6:
                continue

            candles.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5])
            })

        candles.sort(key=lambda x: x["time"])

        return candles

    except Exception as e:

        logging.error(
            f"Kline error {symbol} {interval}: {e}"
        )

        return []


# ==========================================
# TECHNICAL INDICATORS
# ==========================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:

        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


# ==========================================
# CANDLE PATTERNS
# ==========================================

def bullish_engulfing(previous, current):

    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):

    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bullish_pinbar(candle):

    body = abs(candle["close"] - candle["open"])

    lower_wick = (
        min(candle["open"], candle["close"])
        - candle["low"]
    )

    upper_wick = (
        candle["high"]
        - max(candle["open"], candle["close"])
    )

    return (
        lower_wick > body * 2
        and lower_wick > upper_wick
    )


def bearish_pinbar(candle):

    body = abs(candle["close"] - candle["open"])

    upper_wick = (
        candle["high"]
        - max(candle["open"], candle["close"])
    )

    lower_wick = (
        min(candle["open"], candle["close"])
        - candle["low"]
    )

    return (
        upper_wick > body * 2
        and upper_wick > lower_wick
    )


# ==========================================
# MARKET STRUCTURE
# ==========================================

def analyze_market(symbol):

    candles = get_klines(symbol, TIMEFRAME)

    trend_candles = get_klines(
        symbol,
        TREND_TIMEFRAME
    )

    if len(candles) < 60:
        return None

    if len(trend_candles) < 60:
        return None

    closes = [c["close"] for c in candles]

    trend_closes = [
        c["close"] for c in trend_candles
    ]

    current = candles[-1]
    previous = candles[-2]

    # روند یک‌ساعته
    ema20 = ema(trend_closes, 20)
    ema50 = ema(trend_closes, 50)

    if ema20 is None or ema50 is None:
        return None

    bullish_trend = (
        ema20 > ema50
        and trend_closes[-1] > ema20
    )

    bearish_trend = (
        ema20 < ema50
        and trend_closes[-1] < ema20
    )

    # ATR
    atr = calculate_atr(candles)

    if not atr or atr <= 0:
        return None

    entry = current["close"]

    # محدوده نقدینگی ۲۰ کندل قبل
    lookback = candles[-22:-2]

    if len(lookback) < 20:
        return None

    previous_high = max(
        c["high"] for c in lookback
    )

    previous_low = min(
        c["low"] for c in lookback
    )

    # حجم
    avg_volume = sum(
        c["volume"] for c in candles[-21:-1]
    ) / 20

    volume_confirmed = (
        current["volume"] > avg_volume * 1.2
    )

    # بررسی نقدینگی
    bullish_sweep = (
        current["low"] < previous_low
        and current["close"] > previous_low
    )

    bearish_sweep = (
        current["high"] > previous_high
        and current["close"] < previous_high
    )

    # تایید کندلی
    bullish_candle = (
        bullish_engulfing(previous, current)
        or bullish_pinbar(current)
    )

    bearish_candle = (
        bearish_engulfing(previous, current)
        or bearish_pinbar(current)
    )

    # ساختار و امتیاز
    long_score = 0
    short_score = 0

    if bullish_trend:
        long_score += 30

    if bearish_trend:
        short_score += 30

    if bullish_sweep:
        long_score += 30

    if bearish_sweep:
        short_score += 30

    if bullish_candle:
        long_score += 25

    if bearish_candle:
        short_score += 25

    if volume_confirmed:
        long_score += 15
        short_score += 15

    # حداقل امتیاز ورود
    minimum_score = 85

    # ======================================
    # LONG SIGNAL
    # ======================================

    if (
        long_score >= minimum_score
        and bullish_trend
        and bullish_sweep
        and bullish_candle
    ):

        stop_loss = min(
            current["low"] - atr * 0.3,
            entry - atr * 1.5
        )

        risk = entry - stop_loss

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 2.5

        return {
            "symbol": symbol,
            "side": "LONG",
            "entry": entry,
            "sl": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "score": long_score,
            "time": current["time"]
        }

    # ======================================
    # SHORT SIGNAL
    # ======================================

    if (
        short_score >= minimum_score
        and bearish_trend
        and bearish_sweep
        and bearish_candle
    ):

        stop_loss = max(
            current["high"] + atr * 0.3,
            entry + atr * 1.5
        )

        risk = stop_loss - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 2.5

        return {
            "symbol": symbol,
            "side": "SHORT",
            "entry": entry,
            "sl": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "score": short_score,
            "time": current["time"]
        }

    return None


# ==========================================
# FORMAT SIGNAL
# ==========================================

def format_signal(signal):

    side = signal["side"]

    emoji = "🟢" if side == "LONG" else "🔴"

    symbol = signal["symbol"]

    entry = signal["entry"]
    sl = signal["sl"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]

    candle_time = datetime.fromtimestamp(
        signal["time"] / 1000,
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    return f"""
{emoji} <b>NEW FUTURES SIGNAL</b>

━━━━━━━━━━━━━━━━━━

<b>Symbol:</b> {symbol}

<b>Direction:</b> {side}

<b>Timeframe:</b> 15m

━━━━━━━━━━━━━━━━━━

🎯 <b>Entry:</b> {entry:.8g}

🛑 <b>Stop Loss:</b> {sl:.8g}

✅ <b>Take Profit 1:</b> {tp1:.8g}

🚀 <b>Take Profit 2:</b> {tp2:.8g}

━━━━━━━━━━━━━━━━━━

📊 <b>Setup Score:</b> {signal["score"]}/100

🕒 <b>Candle:</b> {candle_time}

<b>Analysis:</b>
• 1H trend confirmation
• Liquidity sweep
• Candle confirmation
• ATR-based risk levels

⚠️ This is an analytical alert, not a guaranteed trade.
Check live price, spread, fees and slippage before acting.

━━━━━━━━━━━━━━━━━━
"""


# ==========================================
# SCAN MARKET
# ==========================================

def scan_market():

    logging.info("Starting market scan...")

    symbols = get_symbols()

    if not symbols:

        logging.warning("No symbols found.")

        return

    logging.info(
        f"Scanning {len(symbols)} futures markets"
    )

    found = 0

    for symbol in symbols:

        try:

            signal = analyze_market(symbol)

            if not signal:
                continue

            # جلوگیری از ارسال سیگنال تکراری
            signal_id = (
                f"{signal['symbol']}_"
                f"{signal['side']}_"
                f"{signal['time']}"
            )

            if signal_id in last_signals:
                continue

            last_signals.add(signal_id)

            # محدود کردن حافظه
            if len(last_signals) > 5000:

                last_signals.clear()

            message = format_signal(signal)

            logging.info(
                f"NEW SIGNAL: {symbol} {signal['side']}"
            )

            broadcast(message)

            found += 1

        except Exception as e:

            logging.error(
                f"Scan error {symbol}: {e}"
            )

        time.sleep(0.2)

    logging.info(
        f"Scan finished. Signals found: {found}"
    )


# ==========================================
# COMMAND HANDLER
# ==========================================

def handle_update(update):

    message = update.get("message")

    if not message:
        return

    chat_id = message["chat"]["id"]

    text = message.get("text", "").strip()

    if text == "/start":

        add_subscriber(chat_id)

        send_message(
            chat_id,
            """
🤖 <b>Toobit Futures AI Scanner</b>

ربات با موفقیت فعال شد.

از این به بعد هر سیگنال معتبر LONG یا SHORT
به‌صورت خودکار برایت ارسال می‌شود.

دستورات:

/scan - اسکن فوری بازار
/status - وضعیت ربات
/stop - توقف دریافت سیگنال

⚠️ ربات فقط هشدار می‌دهد و معامله واقعی باز نمی‌کند.
"""
        )

    elif text == "/stop":

        remove_subscriber(chat_id)

        send_message(
            chat_id,
            "دریافت سیگنال برای شما متوقف شد."
        )

    elif text == "/scan":

        send_message(
            chat_id,
            "🔍 اسکن بازار شروع شد..."
        )

        scan_market()

        send_message(
            chat_id,
            "✅ اسکن بازار تمام شد."
        )

    elif text == "/status":

        send_message(
            chat_id,
            f"""
🤖 <b>Bot Status</b>

✅ Running

👥 Subscribers: {len(subscribers)}

⏱ Scan interval: {SCAN_INTERVAL} seconds

📊 Max markets: {MAX_SYMBOLS}

📈 Timeframes: 15m / 1h
"""
        )


# ==========================================
# MAIN LOOP
# ==========================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing!"
        )

    load_subscribers()

    logging.info("Bot starting...")

    # حذف webhook برای استفاده از polling
    try:

        session.post(
            f"{TELEGRAM_URL}/deleteWebhook",
            timeout=15
        )

    except Exception:
        pass

    # جلوگیری از دریافت آپدیت‌های قدیمی
    offset = 0

    try:

        updates = telegram_updates(-1)

        if updates:

            offset = updates[-1]["update_id"] + 1

    except Exception:
        pass

    last_scan = 0

    while True:

        try:

            # دریافت آپدیت تلگرام فقط یک بار در هر دور
            updates = telegram_updates(offset)

            for update in updates:

                offset = update["update_id"] + 1

                handle_update(update)

            # اسکن خودکار
            now = time.time()

            if now - last_scan >= SCAN_INTERVAL:

                scan_market()

                last_scan = time.time()

        except Exception as e:

            logging.error(
                f"Main loop error: {e}"
            )

        time.sleep(1)


# ==========================================
# START
# ==========================================

if __name__ == "__main__":

    main()
