from app import smc


def _bands(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    deviation = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
    return mean - mult * deviation, mean, mean + mult * deviation


def analyze(m, c, atr):
    if len(c) < 60 or not atr:
        return []

    closes = [x["close"] for x in c]
    prev_closes = closes[:-1]
    price = closes[-1]
    last = c[-1]
    out = []

    def add(name, direction, sl, reason, confidence=60):
        signal = smc.make_signal(name, direction, price, sl, atr, reason, confidence)
        if signal:
            out.append(signal)

    ema20 = m.calculate_ema(closes, 20)
    ema50 = m.calculate_ema(closes, 50)
    prev20 = m.calculate_ema(prev_closes, 20)
    prev50 = m.calculate_ema(prev_closes, 50)
    have_ema = None not in (ema20, ema50, prev20, prev50)

    # 1. EMA crossover (EMA20 / EMA50)
    if have_ema:
        if prev20 <= prev50 and ema20 > ema50:
            add("EMA_CROSS", 1, price - 1.5 * atr, "EMA20 ওপরে কেটে EMA50 পার করেছে (বুলিশ ক্রস)")
        elif prev20 >= prev50 and ema20 < ema50:
            add("EMA_CROSS", -1, price + 1.5 * atr, "EMA20 নিচে কেটে EMA50 পার করেছে (বেয়ারিশ ক্রস)")

    # 2. MACD cross with EMA50 trend filter
    macd = m.calculate_macd(closes)
    prev_macd = m.calculate_macd(prev_closes)
    if macd and prev_macd and ema50:
        if prev_macd["histogram"] <= 0 < macd["histogram"] and price > ema50:
            add("MACD_CROSS", 1, price - 1.5 * atr, "MACD সিগন্যাল লাইন ওপরে কেটেছে, দাম EMA50-এর ওপরে")
        elif prev_macd["histogram"] >= 0 > macd["histogram"] and price < ema50:
            add("MACD_CROSS", -1, price + 1.5 * atr, "MACD সিগন্যাল লাইন নিচে কেটেছে, দাম EMA50-এর নিচে")

    # 3. Donchian 20-bar breakout
    channel = c[-21:-1]
    high20 = max(x["high"] for x in channel)
    low20 = min(x["low"] for x in channel)
    if price > high20 and closes[-2] <= high20:
        add("DONCHIAN", 1, price - 2 * atr, f"২০ ক্যান্ডেলের সর্বোচ্চ {high20:.2f} ভেঙেছে (ব্রেকআউট)")
    elif price < low20 and closes[-2] >= low20:
        add("DONCHIAN", -1, price + 2 * atr, f"২০ ক্যান্ডেলের সর্বনিম্ন {low20:.2f} ভেঙেছে (ব্রেকডাউন)")

    # 4. Session breakout: Asian range (00:00-07:00 UTC) broken during London hours
    hour = (last["time"] // 3600) % 24
    day_start = last["time"] - last["time"] % 86400
    if 7 <= hour < 12:
        asian = [x for x in c if day_start <= x["time"] < day_start + 7 * 3600]
        since = [x for x in c if day_start + 7 * 3600 <= x["time"] < last["time"]]
        if len(asian) >= 5:
            asian_high = max(x["high"] for x in asian)
            asian_low = min(x["low"] for x in asian)
            mid = (asian_high + asian_low) / 2
            if price > asian_high and all(x["close"] <= asian_high for x in since):
                add("SESSION_BREAKOUT", 1, mid, f"লন্ডন সেশনে এশিয়ান রেঞ্জের হাই {asian_high:.2f} ভেঙেছে")
            elif price < asian_low and all(x["close"] >= asian_low for x in since):
                add("SESSION_BREAKOUT", -1, mid, f"লন্ডন সেশনে এশিয়ান রেঞ্জের লো {asian_low:.2f} ভেঙেছে")

    # 5. RSI reversal (confirmation: RSI crosses back out of the extreme zone)
    rsi = m.calculate_rsi(closes)
    prev_rsi = m.calculate_rsi(prev_closes)
    if rsi is not None and prev_rsi is not None:
        if prev_rsi < 30 <= rsi:
            add("RSI_REVERSAL", 1, min(x["low"] for x in c[-3:]) - 0.25 * atr, f"RSI ওভারসোল্ড (৩০) থেকে ফিরেছে ({rsi:.0f})")
        elif prev_rsi > 70 >= rsi:
            add("RSI_REVERSAL", -1, max(x["high"] for x in c[-3:]) + 0.25 * atr, f"RSI ওভারবট (৭০) থেকে ফিরেছে ({rsi:.0f})")

    # 6. Bollinger band bounce
    bands = _bands(closes)
    prev_bands = _bands(prev_closes)
    if bands and prev_bands:
        if closes[-2] < prev_bands[0] and price >= bands[0]:
            add("BB_BOUNCE", 1, min(x["low"] for x in c[-2:]) - 0.25 * atr, "দাম নিচের বলিঙ্গার ব্যান্ড থেকে ভেতরে ফিরেছে")
        elif closes[-2] > prev_bands[2] and price <= bands[2]:
            add("BB_BOUNCE", -1, max(x["high"] for x in c[-2:]) + 0.25 * atr, "দাম ওপরের বলিঙ্গার ব্যান্ড থেকে ভেতরে ফিরেছে")

    # 7. Support/resistance + candle pattern (pin bar / engulfing)
    highs, lows = smc.find_swings(c)
    supports = [c[i]["low"] for i in lows[-8:]]
    resistances = [c[i]["high"] for i in highs[-8:]]
    candle_range = last["high"] - last["low"]
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    prev = c[-2]
    if candle_range > 0:
        bull_pin = lower_wick >= 2 * max(body, 0.1 * candle_range) and lower_wick >= 0.5 * candle_range
        bear_pin = upper_wick >= 2 * max(body, 0.1 * candle_range) and upper_wick >= 0.5 * candle_range
        bull_engulf = prev["close"] < prev["open"] and last["close"] > last["open"] and last["close"] > prev["open"] and last["open"] <= prev["close"]
        bear_engulf = prev["close"] > prev["open"] and last["close"] < last["open"] and last["close"] < prev["open"] and last["open"] >= prev["close"]
        near_support = any(abs(last["low"] - level) <= 0.3 * atr for level in supports)
        near_resistance = any(abs(last["high"] - level) <= 0.3 * atr for level in resistances)
        if (bull_pin or bull_engulf) and near_support:
            add("SR_CANDLE", 1, last["low"] - 0.25 * atr, "সাপোর্ট লেভেলে বুলিশ " + ("পিন বার" if bull_pin else "এনগাল্ফিং"))
        elif (bear_pin or bear_engulf) and near_resistance:
            add("SR_CANDLE", -1, last["high"] + 0.25 * atr, "রেজিস্ট্যান্স লেভেলে বেয়ারিশ " + ("পিন বার" if bear_pin else "এনগাল্ফিং"))

    # 8. Trend pullback to EMA20
    if ema20 is not None and ema50 is not None:
        recent = c[-3:]
        if ema20 > ema50 and price > ema50 and min(x["low"] for x in recent) <= ema20 and price > ema20 and last["close"] > last["open"]:
            add("TREND_PULLBACK", 1, min(x["low"] for x in recent) - 0.25 * atr, "আপট্রেন্ডে EMA20-তে পুলব্যাক করে দাম ফিরেছে", 70)
        elif ema20 < ema50 and price < ema50 and max(x["high"] for x in recent) >= ema20 and price < ema20 and last["close"] < last["open"]:
            add("TREND_PULLBACK", -1, max(x["high"] for x in recent) + 0.25 * atr, "ডাউনট্রেন্ডে EMA20-তে পুলব্যাক করে দাম নেমেছে", 70)

    return out
