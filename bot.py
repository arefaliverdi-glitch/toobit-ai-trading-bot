import os
import time
import logging
import requests
import threading
from datetime import datetime, timezone

# ==========================================
# CONFIG
# ==========================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_URL = "https://api.toobit.com"
TG_URL = f"https://api.telegram.org/bot{TOKEN}"

TIMEFRAMES = {
    "1m": 1 * 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000
}

TOP_SYMBOLS = 100
SCALP_MIN_SCORE = 70
DAYTRADE_MIN_SCORE = 70

SCAN_INTERVAL = 60
KLINE_LIMIT = 250
SYMBOL_REFRESH = 1800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

subscribers = set()
last_signals = {}

symbols_cache = []
last_symbol_refresh = 0
scan_lock = threading.Lock()

# ==========================================
# HTTP
# ==========================================

def api_get(url, params=None):

    try:
        response = session.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        logging.error(f"API error: {e}")

        return None


def telegram(method, data=None):

    try:

        response = session.post(
            f"{TG_URL}/{method}",
            json=data or {},
            timeout=15
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        logging.error(f"Telegram error: {e}")

        return {}


def send_message(chat_id, message):

    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
    )


# ==========================================
# TOP 30 FUTURES SYMBOLS
# ==========================================

def get_top_symbols():

    global symbols_cache
    global last_symbol_refresh

    now = time.time()

    if (
        symbols_cache
        and now - last_symbol_refresh < SYMBOL_REFRESH
    ):
        return symbols_cache

    logging.info("Refreshing top symbols...")

    exchange = api_get(
        f"{BASE_URL}/api/v1/exchangeInfo"
    )

    tickers = api_get(
        f"{BASE_URL}/quote/v1/contract/ticker/24hr"
    )

    if not isinstance(exchange, dict):
        return symbols_cache

    if not isinstance(tickers, list):
        return symbols_cache

    contracts = exchange.get("contracts", [])

    if not isinstance(contracts, list):
        return symbols_cache

    tradable = set()

    for contract in contracts:

        symbol = contract.get("symbol")
        status = contract.get("status")

        if (
            symbol
            and symbol.endswith("-USDT")
            and status == "TRADING"
        ):
            tradable.add(symbol)

    ranked = []

    for ticker in tickers:

        symbol = ticker.get("s")

        if symbol not in tradable:
            continue

        try:

            volume = float(ticker.get("qv", 0))

            if volume > 0:
                ranked.append((symbol, volume))

        except (ValueError, TypeError):
            continue

    ranked.sort(
        key=lambda x: x[1],
        reverse=True
    )

    ranked_symbols = [
        item[0] for item in ranked
    ]

    new_symbols = ranked_symbols[:TOP_SYMBOLS]

    # اضافه کردن طلا و نقره حتی اگر جزو 100 نماد برتر نباشند
    for symbol in ranked_symbols:

        if (
            ("XAU" in symbol or "XAG" in symbol)
            and symbol not in new_symbols
        ):
            new_symbols.append(symbol)

    if new_symbols:

        symbols_cache = new_symbols
        last_symbol_refresh = now

    logging.info(
        f"Selected {len(symbols_cache)} symbols including metals"
    )

    return symbols_cache


# ==========================================
# KLINES
# ==========================================

def get_klines(symbol, interval):

    interval_ms = TIMEFRAMES[interval]

    now_ms = int(time.time() * 1000)

    start_time = (
        now_ms -
        (KLINE_LIMIT + 5) * interval_ms
    )

    data = api_get(
        f"{BASE_URL}/quote/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time,
            "endTime": now_ms,
            "limit": KLINE_LIMIT
        }
    )

    if not isinstance(data, list):
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

    candles.sort(
        key=lambda x: x["time"]
    )

    # حذف کندل در حال تشکیل
    now_ms = int(time.time() * 1000)

    candles = [
        c for c in candles
        if c["time"] + interval_ms <= now_ms
    ]

    return candles


# ==========================================
# INDICATORS
# ==========================================

def ema(values, period):

    if len(values) < period:
        return None

    alpha = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:

        result = (
            alpha * value
            + (1 - alpha) * result
        )

    return result


def calculate_rsi(values, period=14):

    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [
        max(x, 0)
        for x in changes
    ]

    losses = [
        max(-x, 0)
        for x in changes
    ]

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

            abs(
                current["high"] - previous["close"]
            ),

            abs(
                current["low"] - previous["close"]
            )

        )

        trs.append(tr)

    return sum(trs[-period:]) / period


# ==========================================
# PRICE ACTION
# ==========================================

def candle_patterns(candles):

    current = candles[-1]
    previous = candles[-2]

    body = abs(
        current["close"] - current["open"]
    )

    candle_range = max(
        current["high"] - current["low"],
        1e-12
    )

    upper_wick = (
        current["high"]
        - max(current["open"], current["close"])
    )

    lower_wick = (
        min(current["open"], current["close"])
        - current["low"]
    )

    patterns = []

    # Bullish engulfing

    if (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["close"] > previous["open"]
        and current["open"] < previous["close"]
    ):

        patterns.append("BULLISH_ENGULFING")

    # Bearish engulfing

    if (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["close"] < previous["open"]
        and current["open"] > previous["close"]
    ):

        patterns.append("BEARISH_ENGULFING")

    # Bullish pin bar

    if (
        lower_wick > max(body, 1e-12) * 2
        and lower_wick > candle_range * 0.55
        and current["close"] > current["open"]
    ):

        patterns.append("BULLISH_PINBAR")

    # Bearish pin bar

    if (
        upper_wick > max(body, 1e-12) * 2
        and upper_wick > candle_range * 0.55
        and current["close"] < current["open"]
    ):

        patterns.append("BEARISH_PINBAR")

    return patterns


# ==========================================
# LIQUIDITY SWEEP
# ==========================================

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

    if (
        current["low"] < previous_low
        and current["close"] > previous_low
    ):

        return "BULLISH_SWEEP"

    if (
        current["high"] > previous_high
        and current["close"] < previous_high
    ):

        return "BEARISH_SWEEP"

    return None


# ==========================================
# MARKET STRUCTURE
# ==========================================

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


# ==========================================
# TIMEFRAME ANALYSIS
# ==========================================

def analyze_timeframe(candles):

    if len(candles) < 205:
        return None

    closes = [
        x["close"] for x in candles
    ]

    volumes = [
        x["volume"] for x in candles
    ]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)

    rsi = calculate_rsi(closes)

    atr = calculate_atr(candles)

    if any(
        x is None
        for x in [ema20, ema50, ema200, rsi, atr]
    ):
        return None

    avg_volume = (
        sum(volumes[-21:-1]) / 20
    )

    volume_ratio = (
        volumes[-1] / avg_volume
        if avg_volume > 0 else 0
    )

    trend = "NEUTRAL"

    if ema20 > ema50 > ema200:
        trend = "BULLISH"

    elif ema20 < ema50 < ema200:
        trend = "BEARISH"

    return {

        "price": closes[-1],

        "candle_time": candles[-1]["time"],

        "ema20": ema20,

        "ema50": ema50,

        "ema200": ema200,

        "rsi": rsi,

        "atr": atr,

        "volume_ratio": volume_ratio,

        "trend": trend,

        "patterns": candle_patterns(candles),

        "liquidity": liquidity_analysis(candles),

        "structure": structure_analysis(candles)

    }


# ==========================================
# SCORING
# ==========================================

def calculate_score(results, mode="daytrade"):

    long_score = 0
    short_score = 0

    if mode == "scalp":

        trend_timeframes = ["15m"]
        entry_tf = "5m"
        min_score = SCALP_MIN_SCORE

        trend_weight = 20
        entry_trend_weight = 15

    else:

        trend_timeframes = ["4h", "1h"]
        entry_tf = "15m"
        min_score = DAYTRADE_MIN_SCORE

        trend_weight = 15
        entry_trend_weight = 10

    # بررسی روند تایم‌فریم‌های بالاتر

    for tf in trend_timeframes:

        data = results[tf]

        if data["trend"] == "BULLISH":
            long_score += trend_weight

        elif data["trend"] == "BEARISH":
            short_score += trend_weight

    # تحلیل تایم‌فریم ورود

    data = results[entry_tf]

    if data["trend"] == "BULLISH":
        long_score += entry_trend_weight

    elif data["trend"] == "BEARISH":
        short_score += entry_trend_weight

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

    patterns = data["patterns"]

    if "BULLISH_ENGULFING" in patterns:
        long_score += 12

    if "BEARISH_ENGULFING" in patterns:
        short_score += 12

    if "BULLISH_PINBAR" in patterns:
        long_score += 8

    if "BEARISH_PINBAR" in patterns:
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

    if long_score >= min_score and long_score > short_score:
        return "LONG", long_score

    if short_score >= min_score and short_score > long_score:
        return "SHORT", short_score

    return None, max(long_score, short_score)


# ==========================================
# SIGNAL GENERATOR
# ==========================================

def analyze_symbol(symbol):

    results = {}

    for tf in TIMEFRAMES:

        candles = get_klines(symbol, tf)

        if not candles:
            return None

        data = analyze_timeframe(candles)

        if not data:
            return None

        results[tf] = data

    direction, score = calculate_score(results)

    if direction is None:
        return None

    data = results["5m"]

    entry = data["price"]
    atr = data["atr"]

    if atr <= 0:
        return None

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

        "candle_time": data["candle_time"],

        "analysis": results

    }

def analyze_symbol_modes(symbol):

    results = {}

    # دریافت و تحلیل تایم‌فریم‌ها
    for tf in ["5m", "15m", "1h", "4h"]:

        candles = get_klines(symbol, tf)

        if not candles:
            continue

        data = analyze_timeframe(candles)

        if data:
            results[tf] = data

    signals = []

    # -------------------------
    # SCALP ANALYSIS
    # -------------------------

    if "5m" in results and "15m" in results:

    scalp_results = {
        "5m": results["5m"],
        "15m": results["15m"]
    }

        direction, score = calculate_score(
            scalp_results,
            mode="scalp"
        )

        if direction is not None:

            data = scalp_results["1m"]

            entry = data["price"]
            atr = data["atr"]

            if atr > 0:

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

                signals.append({
                    "symbol": symbol,
                    "type": "SCALP",
                    "direction": direction,
                    "score": score,
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "candle_time": data["candle_time"],
                    "analysis": scalp_results
                })

    # -------------------------
    # DAY TRADING ANALYSIS
    # -------------------------

    if all(
        tf in results
        for tf in ["15m", "1h", "4h"]
    ):

        day_results = {
            "15m": results["15m"],
            "1h": results["1h"],
            "4h": results["4h"]
        }

        direction, score = calculate_score(
            day_results,
            mode="daytrade"
        )

        if direction is not None:

            data = day_results["15m"]

            entry = data["price"]
            atr = data["atr"]

            if atr > 0:

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

                signals.append({
                    "symbol": symbol,
                    "type": "DAY TRADING",
                    "direction": direction,
                    "score": score,
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "candle_time": data["candle_time"],
                    "analysis": day_results
                })

    return signals

# ==========================================
# SIGNAL MESSAGE
# ==========================================
def calculate_leverage(score):
    if score < 70:
        return 10
    elif score < 75:
        return 15
    elif score < 80:
        return 20
    elif score < 85:
        return 25
    elif score < 90:
        return 30
    elif score < 95:
        return 50
    else:
        return 100
def format_signal(signal):

    emoji = (
        "🟢"
        if signal["direction"] == "LONG"
        else "🔴"
    )

    leverage = calculate_leverage(signal["score"])

    signal_type = signal.get("type", "DAY TRADING")

    message = f"""
{emoji} <b>TOOBIT FUTURES SIGNAL</b>

━━━━━━━━━━━━━━━━

📌 <b>Type:</b> {signal_type}

💎 <b>Symbol:</b> {signal['symbol']}

📊 <b>Direction:</b> {signal['direction']}

⭐ <b>Score:</b> {signal['score']}/100

⚡ <b>Suggested Leverage:</b> {leverage}x

━━━━━━━━━━━━━━━━

🎯 <b>Entry:</b> {signal['entry']:.8g}

🛑 <b>Stop Loss:</b> {signal['sl']:.8g}

🥇 <b>TP1:</b> {signal['tp1']:.8g}

🥈 <b>TP2:</b> {signal['tp2']:.8g}

🥉 <b>TP3:</b> {signal['tp3']:.8g}

━━━━━━━━━━━━━━━━

📈 <b>TIMEFRAME ANALYSIS</b>
"""

    for tf, data in signal["analysis"].items():

        message += f"""

<b>{tf}</b>

Trend: {data['trend']}

RSI: {data['rsi']:.1f}

Volume: {data['volume_ratio']:.2f}x

Liquidity: {data['liquidity'] or 'None'}

Structure: {data['structure'] or 'None'}

Patterns: {', '.join(data['patterns']) or 'None'}
"""

    message += """

━━━━━━━━━━━━━━━━

⚠️ امتیاز، احتمال برد نیست.
اهرم پیشنهادی صرفاً نمایشی است.
سطوح قیمتی تخمینی هستند.
قبل از معامله، قیمت زنده، کارمزد،
اسپرد و اسلیپیج را بررسی کن.

🤖 Toobit Market Scanner
"""

    return message


# ==========================================
# MARKET SCAN
# ==========================================

def scan_market(target_chat=None):

    logging.info("Market scan started")

    symbols = get_top_symbols()

    if not symbols:

        logging.error("No symbols available")

        if target_chat is not None:
            send_message(
                target_chat,
                "خطا در دریافت فهرست نمادهای Toobit."
            )

        return

    found = 0

    for index, symbol in enumerate(symbols, 1):

        try:

            logging.info(
                f"Scanning {index}/{len(symbols)}: {symbol}"
            )

            signals = analyze_symbol_modes(symbol)

            if not signals:
                continue

            for signal in signals:

                signal_type = signal["type"]
                direction = signal["direction"]
                candle_time = signal["candle_time"]

                # جلوگیری از تکرار هر نوع سیگنال
                key = (
                    symbol,
                    signal_type,
                    direction
                )

                if target_chat is not None:

                    send_message(
                        target_chat,
                        format_signal(signal)
                    )

                    found += 1

                else:

                    if last_signals.get(key) == candle_time:
                        continue

                    for chat_id in list(subscribers):

                        try:

                            send_message(
                                chat_id,
                                format_signal(signal)
                            )

                            logging.info(
                                f"{signal_type} signal sent to {chat_id}"
                            )

                        except Exception as e:

                            logging.error(
                                f"Failed to send signal to {chat_id}: {e}"
                            )

                    last_signals[key] = candle_time
                    found += 1

        except Exception as e:

            logging.error(
                f"{symbol} analysis error: {e}"
            )

    logging.info(
        f"Scan finished. Signals sent: {found}"
    )

    if target_chat is not None:

        if found == 0:

            send_message(
                target_chat,
                "در این اسکن سیگنال جدیدی پیدا نشد."
            )
def run_scan_in_background(target_chat=None):

    if not scan_lock.acquire(blocking=False):

        logging.warning(
            "Scan skipped: another scan is running"
        )

        if target_chat is not None:
            send_message(
                target_chat,
                "یک اسکن دیگر در حال اجراست؛ کمی بعد دوباره تلاش کن."
            )

        return

    try:

        scan_market(target_chat=target_chat)

    except Exception as e:

        logging.error(
            "Background scan error: %s",
            e
        )

    finally:

        scan_lock.release()

# ==========================================
# TELEGRAM COMMANDS
# ==========================================

def handle_command(chat_id, text):

    command = text.split()[0].lower()

    if command == "/start":

        subscribers.add(chat_id)

        send_message(
            chat_id,
            """
🤖 <b>Toobit AI Scanner فعال شد</b>

✅ بررسی 100 نماد برتر
✅ تایم‌فریم 1m
✅ تایم‌فریم 5m
✅ تایم‌فریم 15m
✅ تایم‌فریم 1h
✅ تایم‌فریم 4h

✅ EMA / RSI / ATR
✅ Price Action
✅ Liquidity Sweep
✅ Break of Structure

⭐ حداقل امتیاز اسکالپ: 75
⭐ حداقل امتیاز دی‌ترید: 70

⏱ اسکن خودکار هر ۶۰ ثانیه

دستورات:

/scan - اسکن فوری
/status - وضعیت ربات
/help - راهنما

ربات فقط هشدار می‌دهد و سفارش واقعی باز نمی‌کند.
"""
        )

    elif command == "/scan":

        send_message(
            chat_id,
            "🔍 درخواست اسکن ثبت شد. در حال بررسی..."
        )

        threading.Thread(
            target=run_scan_in_background,
            kwargs={"target_chat": chat_id},
            daemon=True
        ).start()

    elif command == "/status":

        send_message(
            chat_id,
            f"""
🟢 ربات فعال است.

تعداد نمادها: {TOP_SYMBOLS}

تایم‌فریم‌ها: 1m / 5m / 15m / 1h / 4h

حداقل امتیاز اسکالپ: {SCALP_MIN_SCORE}

حداقل امتیاز دی‌ترید: {DAYTRADE_MIN_SCORE}

فاصله اسکن: ۶۰ ثانیه

تعداد مشترکان فعلی: {len(subscribers)}
"""
        )

    elif command == "/help":

        send_message(
            chat_id,
            """
/start - فعال‌سازی دریافت سیگنال
/scan - اسکن فوری
/status - وضعیت ربات
/help - راهنما
"""
        )


# ==========================================
# TELEGRAM POLLING
# ==========================================

def process_updates(offset):

    try:

        params = {
            "timeout": 1
        }

        if offset is not None:
            params["offset"] = offset

        response = session.get(
            f"{TG_URL}/getUpdates",
            params=params,
            timeout=5
        ).json()

        if not response.get("ok"):
            return offset

        for update in response.get("result", []):

            offset = update["update_id"] + 1

            message = update.get("message")

            if not message:
                continue

            chat_id = message["chat"]["id"]

            text = message.get("text", "")

            if text.startswith("/"):

                try:
                    handle_command(chat_id, text)

                except Exception as e:

                    logging.error(
                        f"Command error: {e}"
                    )

                    send_message(
                        chat_id,
                        "خطا در اجرای دستور. دوباره تلاش کن."
                    )

        return offset

    except Exception as e:

        logging.error(
            f"Telegram polling error: {e}"
        )

        return offset

# ==========================================
# MAIN LOOP
# ==========================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing in Railway Variables"
        )

    logging.info("Toobit AI Scanner started")

    offset = None
    next_scan = 0

    while True:

        try:

            # دریافت پیام های تلگرام
            offset = process_updates(offset)

            # بررسی زمان اسکن خودکار
            now = time.time()

            if now >= next_scan:

                logging.info(
                    "Automatic market scan requested"
                )

                threading.Thread(
                    target=run_scan_in_background,
                    daemon=True
                ).start()

                next_scan = now + SCAN_INTERVAL

        except Exception as e:

            logging.error(
                "Main loop error: %s",
                e
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
