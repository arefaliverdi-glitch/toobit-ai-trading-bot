import os
import time
import requests
import logging
from datetime import datetime, timezone

# ================= CONFIG =================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_URL = "https://api.toobit.com"
TG_URL = f"https://api.telegram.org/bot{TOKEN}"

SYMBOLS = [
    "BTC-SWAP-USDT",
    "ETH-SWAP-USDT",
    "SOL-SWAP-USDT",
    "XRP-SWAP-USDT"
]

TIMEFRAMES = ["5m", "15m", "1h", "4h"]

MIN_SCORE = 50
SCAN_INTERVAL = 300

logging.basicConfig(level=logging.INFO)

session = requests.Session()

subscribers = set()
last_signals = {}

# ================= HTTP =================

def api_get(url, params=None):

    try:
        r = session.get(
            url,
            params=params,
            timeout=15
        )

        r.raise_for_status()

        return r.json()

    except Exception as e:

        logging.error(f"API error: {e}")

        return None


def telegram(method, data=None):

    try:

        r = session.post(
            f"{TG_URL}/{method}",
            json=data or {},
            timeout=20
        )

        return r.json()

    except Exception as e:

        logging.error(f"Telegram error: {e}")

        return {}


def send_message(chat_id, text):

    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
    )


# ================= MARKET DATA =================

def get_klines(symbol, interval, limit=300):

    data = api_get(
        f"{BASE_URL}/quote/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )

    if not isinstance(data, list) or len(data) < 100:
        return []

    candles = []

    for row in data:

        try:

            candles.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5])
            })

        except (ValueError, TypeError, IndexError):
            continue

    # حذف کندل در حال تشکیل
    if len(candles) > 1:
        candles = candles[:-1]

    return candles


def get_market_price(symbol):

    data = api_get(
        f"{BASE_URL}/quote/v1/contract/ticker/price",
        {"symbol": symbol}
    )

    if isinstance(data, list) and data:

        try:
            return float(data[0]["p"])
        except Exception:
            pass

    return None


# ================= INDICATORS =================

def ema(values, period):

    if len(values) < period:
        return None

    alpha = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:

        result = (
            value * alpha +
            result * (1 - alpha)
        )

    return result


def calculate_rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


# ================= PRICE ACTION =================

def candle_patterns(candles):

    c = candles[-1]
    p = candles[-2]

    body = abs(c["close"] - c["open"])

    candle_range = max(
        c["high"] - c["low"],
        0.00000001
    )

    upper_wick = c["high"] - max(
        c["open"], c["close"]
    )

    lower_wick = min(
        c["open"], c["close"]
    ) - c["low"]

    patterns = []

    # Bullish Engulfing

    if (
        p["close"] < p["open"]
        and c["close"] > c["open"]
        and c["close"] > p["open"]
        and c["open"] < p["close"]
    ):

        patterns.append("BULLISH_ENGULFING")

    # Bearish Engulfing

    if (
        p["close"] > p["open"]
        and c["close"] < c["open"]
        and c["close"] < p["open"]
        and c["open"] > p["close"]
    ):

        patterns.append("BEARISH_ENGULFING")

    # Bullish Pin Bar

    if (
        lower_wick > body * 2
        and lower_wick > candle_range * 0.55
        and c["close"] > c["open"]
    ):

        patterns.append("BULLISH_PINBAR")

    # Bearish Pin Bar

    if (
        upper_wick > body * 2
        and upper_wick > candle_range * 0.55
        and c["close"] < c["open"]
    ):

        patterns.append("BEARISH_PINBAR")

    return patterns


def liquidity_analysis(candles):

    recent = candles[-21:-1]

    if len(recent) < 10:
        return None

    previous_high = max(
        x["high"] for x in recent
    )

    previous_low = min(
        x["low"] for x in recent
    )

    current = candles[-1]

    # Sweep of lows followed by recovery

    if (
        current["low"] < previous_low
        and current["close"] > previous_low
    ):

        return "BULLISH_SWEEP"

    # Sweep of highs followed by rejection

    if (
        current["high"] > previous_high
        and current["close"] < previous_high
    ):

        return "BEARISH_SWEEP"

    return None


def structure_analysis(candles):

    recent = candles[-30:]

    if len(recent) < 20:
        return None

    previous_high = max(
        x["high"] for x in recent[:-1]
    )

    previous_low = min(
        x["low"] for x in recent[:-1]
    )

    current = recent[-1]

    if current["close"] > previous_high:
        return "BULLISH_BOS"

    if current["close"] < previous_low:
        return "BEARISH_BOS"

    return None


# ================= TIMEFRAME ANALYSIS =================

def analyze_timeframe(candles):

    if len(candles) < 210:
        return None

    closes = [x["close"] for x in candles]

    volumes = [x["volume"] for x in candles]

    price = closes[-1]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    rsi = calculate_rsi(closes)

    atr = calculate_atr(candles)

    if None in (ema20, ema50, ema200, rsi, atr):
        return None

    avg_volume = sum(volumes[-21:-1]) / 20

    volume_ratio = (
        volumes[-1] / avg_volume
        if avg_volume > 0 else 0
    )

    patterns = candle_patterns(candles)

    liquidity = liquidity_analysis(candles)

    structure = structure_analysis(candles)

    trend = "NEUTRAL"

    if ema20 > ema50 > ema200:
        trend = "BULLISH"

    elif ema20 < ema50 < ema200:
        trend = "BEARISH"

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "atr": atr,
        "volume_ratio": volume_ratio,
        "patterns": patterns,
        "liquidity": liquidity,
        "structure": structure,
        "trend": trend
    }


# ================= SCORING ENGINE =================

def calculate_score(results):

    long_score = 0
    short_score = 0

    reasons_long = []
    reasons_short = []

    # Higher timeframe trend

    for tf in ["4h", "1h"]:

        data = results.get(tf)

        if not data:
            continue

        if data["trend"] == "BULLISH":

            long_score += 15
            reasons_long.append(f"{tf}: صعودی")

        elif data["trend"] == "BEARISH":

            short_score += 15
            reasons_short.append(f"{tf}: نزولی")

    # Medium timeframe

    data = results.get("15m")

    if data:

        if data["trend"] == "BULLISH":
            long_score += 10

        elif data["trend"] == "BEARISH":
            short_score += 10

    # Entry timeframe

    data = results.get("5m")

    if data:

        if data["trend"] == "BULLISH":
            long_score += 8

        elif data["trend"] == "BEARISH":
            short_score += 8

        rsi = data["rsi"]

        if 45 <= rsi <= 65:
            long_score += 5

        if 35 <= rsi <= 55:
            short_score += 5

        if rsi > 75:
            long_score -= 8

        if rsi < 25:
            short_score -= 8

        if data["volume_ratio"] >= 1.5:

            if data["price"] > data["ema20"]:
                long_score += 8

            elif data["price"] < data["ema20"]:
                short_score += 8

        if "BULLISH_ENGULFING" in data["patterns"]:
            long_score += 12

        if "BEARISH_ENGULFING" in data["patterns"]:
            short_score += 12

        if "BULLISH_PINBAR" in data["patterns"]:
            long_score += 8

        if "BEARISH_PINBAR" in data["patterns"]:
            short_score += 8

        if data["liquidity"] == "BULLISH_SWEEP":
            long_score += 15

        if data["liquidity"] == "BEARISH_SWEEP":
            short_score += 15

        if data["structure"] == "BULLISH_BOS":
            long_score += 12

        if data["structure"] == "BEARISH_BOS":
            short_score += 12

    long_score = max(0, min(100, long_score))
    short_score = max(0, min(100, short_score))

    if long_score >= MIN_SCORE and long_score > short_score:

        return "LONG", long_score

    if short_score >= MIN_SCORE and short_score > long_score:

        return "SHORT", short_score

    return None, max(long_score, short_score)


# ================= SIGNAL GENERATOR =================

def analyze_symbol(symbol):

    results = {}

    for tf in TIMEFRAMES:

        candles = get_klines(symbol, tf)

        if not candles:
            return None

        analysis = analyze_timeframe(candles)

        if not analysis:
            return None

        results[tf] = analysis

    direction, score = calculate_score(results)

    if not direction:
        return None

    data = results["5m"]

    entry = data["price"]

    atr = data["atr"]

    if direction == "LONG":

        sl = entry - atr * 1.5

        tp1 = entry + atr * 1.5
        tp2 = entry + atr * 2.5
        tp3 = entry + atr * 4

    else:

        sl = entry + atr * 1.5

        tp1 = entry - atr * 1.5
        tp2 = entry - atr * 2.5
        tp3 = entry - atr * 4

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "time": datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC"),
        "analysis": results
    }


# ================= SIGNAL MESSAGE =================

def format_signal(signal):

    direction = signal["direction"]

    emoji = "🟢" if direction == "LONG" else "🔴"

    result = f"""
{emoji} <b>TOOBIT FUTURES SIGNAL</b>

━━━━━━━━━━━━━━━━━━

💎 <b>Symbol:</b> {signal['symbol']}

📊 <b>Direction:</b> {direction}

⭐ <b>Analysis Score:</b> {signal['score']}/100

━━━━━━━━━━━━━━━━━━

🎯 <b>Entry:</b> {signal['entry']:.8g}

🛑 <b>Stop Loss:</b> {signal['sl']:.8g}

🥇 <b>Take Profit 1:</b> {signal['tp1']:.8g}

🥈 <b>Take Profit 2:</b> {signal['tp2']:.8g}

🥉 <b>Take Profit 3:</b> {signal['tp3']:.8g}

━━━━━━━━━━━━━━━━━━

📈 <b>Multi-Timeframe Analysis</b>

"""

    for tf, data in signal["analysis"].items():

        result += (
            f"\n<b>{tf}</b>"
            f"\nTrend: {data['trend']}"
            f"\nRSI: {data['rsi']:.1f}"
            f"\nVolume: {data['volume_ratio']:.2f}x"
            f"\n"
        )

    result += f"""
━━━━━━━━━━━━━━━━━━

🕒 {signal['time']}

⚠️ امتیاز، احتمال برد نیست.
قیمت ورود و سطوح باید پیش از معامله
با قیمت زنده و شرایط بازار بررسی شوند.

🤖 Automated Market Scanner
"""

    return result


# ================= SCANNER =================

def scan_market():

    logging.info("Starting market scan...")

    for symbol in SYMBOLS:

        try:

            signal = analyze_symbol(symbol)

            if not signal:
                continue

            key = (
                symbol,
                signal["direction"]
            )

            old = last_signals.get(key)

            # جلوگیری از ارسال تکراری
            if old == signal["score"]:
                continue

            last_signals[key] = signal["score"]

            message = format_signal(signal)

            for chat_id in list(subscribers):

                send_message(chat_id, message)

            logging.info(
                f"{symbol}: {signal['direction']} "
                f"Score={signal['score']}"
            )

        except Exception as e:

            logging.error(
                f"Error analyzing {symbol}: {e}"
            )


# ================= TELEGRAM COMMANDS =================

def handle_command(chat_id, text):

    if text.startswith("/start"):

        subscribers.add(chat_id)

        send_message(
            chat_id,
            """
🤖 <b>Toobit AI Market Scanner</b>

ربات تحلیل بازار فیوچرز فعال شد.

امکانات:

✅ تحلیل 5m
✅ تحلیل 15m
✅ تحلیل 1h
✅ تحلیل 4h
✅ Price Action
✅ Liquidity Sweep
✅ Break of Structure
✅ RSI / EMA / ATR
✅ Entry / SL / TP

حداقل امتیاز سیگنال: 50

دستورات:

/scan - اسکن فوری
/status - وضعیت ربات
/help - راهنما

ربات فقط هشدار می‌دهد و معامله باز نمی‌کند.
"""
        )

    elif text.startswith("/scan"):

        send_message(
            chat_id,
            "🔍 در حال تحلیل بازار... لطفاً صبر کن."
        )

        signal = False

        for symbol in SYMBOLS:

            result = analyze_symbol(symbol)

            if result:

                send_message(
                    chat_id,
                    format_signal(result)
                )

                signal = True

        if not signal:

            send_message(
                chat_id,
                "فعلاً سیگنال معتبر با امتیاز 50 یا بیشتر پیدا نشد."
            )

    elif text.startswith("/status"):

        send_message(
            chat_id,
            f"""
🟢 ربات فعال است.

Symbols: {len(SYMBOLS)}

Timeframes: 5m / 15m / 1h / 4h

Minimum Score: {MIN_SCORE}

Scan Interval: {SCAN_INTERVAL} seconds

Subscribers: {len(subscribers)}
"""
        )

    elif text.startswith("/help"):

        send_message(
            chat_id,
            """
/start - شروع ربات
/scan - اسکن فوری بازار
/status - وضعیت ربات
/help - راهنما
"""
        )


# ================= POLLING =================

def telegram_loop():

    offset = None

    while True:

        try:

            params = {
                "timeout": 25
            }

            if offset is not None:
                params["offset"] = offset

            response = session.get(
                f"{TG_URL}/getUpdates",
                params=params,
                timeout=35
            ).json()

            if not response.get("ok"):
                time.sleep(3)
                continue

            for update in response.get("result", []):

                offset = update["update_id"] + 1

                message = update.get("message")

                if not message:
                    continue

                chat_id = message["chat"]["id"]

                text = message.get("text", "")

                if text:
                    handle_command(chat_id, text)

        except Exception as e:

            logging.error(f"Polling error: {e}")

            time.sleep(5)


# ================= MAIN =================

def main():

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    logging.info("Toobit AI Scanner started")

    last_scan = 0

    while True:

        try:

            now = time.time()

            if now - last_scan >= SCAN_INTERVAL:

                scan_market()

                last_scan = time.time()

            telegram_loop()

        except KeyboardInterrupt:

            break

        except Exception as e:

            logging.error(f"Main error: {e}")

            time.sleep(10)


if __name__ == "__main__":

    main()
