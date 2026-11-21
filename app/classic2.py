from app import smc


def _mean(values):
    return sum(values) / len(values)


def _true_ranges(c):
    out = [c[0]["high"] - c[0]["low"]]
    for i in range(1, len(c)):
        out.append(max(
            c[i]["high"] - c[i]["low"],
            abs(c[i]["high"] - c[i - 1]["close"]),
            abs(c[i]["low"] - c[i - 1]["close"]),
        ))
    return out


def supertrend(c, period=10, mult=3.0):
    n = len(c)
    tr = _true_ranges(c)
    upper = [0.0] * n
    lower = [0.0] * n
    trend = [1] * n
    for i in range(period, n):
        a = _mean(tr[i - period + 1:i + 1])
        mid = (c[i]["high"] + c[i]["low"]) / 2
        basic_upper, basic_lower = mid + mult * a, mid - mult * a
        if i == period:
            upper[i], lower[i] = basic_upper, basic_lower
            continue
        upper[i] = basic_upper if basic_upper < upper[i - 1] or c[i - 1]["close"] > upper[i - 1] else upper[i - 1]
        lower[i] = basic_lower if basic_lower > lower[i - 1] or c[i - 1]["close"] < lower[i - 1] else lower[i - 1]
        trend[i] = trend[i - 1]
        if trend[i] == 1 and c[i]["close"] < lower[i]:
            trend[i] = -1
        elif trend[i] == -1 and c[i]["close"] > upper[i]:
            trend[i] = 1
    return trend, upper, lower


def stochastic(c, k_period=14, d_period=3):
    ks = []
    for i in range(k_period - 1, len(c)):
        window = c[i - k_period + 1:i + 1]
        high = max(x["high"] for x in window)
        low = min(x["low"] for x in window)
        ks.append(50.0 if high == low else (c[i]["close"] - low) / (high - low) * 100)
    ds = [None] * (d_period - 1) + [_mean(ks[j - d_period + 1:j + 1]) for j in range(d_period - 1, len(ks))]
    return ks, ds


def adx(c, period=14):
    n = len(c)
    if n < period * 2 + 2:
        return None
    plus, minus, tr = [0.0], [0.0], [c[0]["high"] - c[0]["low"]]
    for i in range(1, n):
        up = c[i]["high"] - c[i - 1]["high"]
        down = c[i - 1]["low"] - c[i]["low"]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        tr.append(max(
            c[i]["high"] - c[i]["low"],
            abs(c[i]["high"] - c[i - 1]["close"]),
            abs(c[i]["low"] - c[i - 1]["close"]),
        ))

    def wilder(values):
        out = {}
        total = sum(values[1:period + 1])
        out[period] = total
        for i in range(period + 1, n):
            total = total - total / period + values[i]
            out[i] = total
        return out

    st, sp, sm = wilder(tr), wilder(plus), wilder(minus)
    pdi, mdi, dx = {}, {}, {}
    for i in range(period, n):
        if st[i] == 0:
            pdi[i] = mdi[i] = dx[i] = 0.0
            continue
        pdi[i] = 100 * sp[i] / st[i]
        mdi[i] = 100 * sm[i] / st[i]
        total = pdi[i] + mdi[i]
        dx[i] = 0.0 if total == 0 else 100 * abs(pdi[i] - mdi[i]) / total
    first = 2 * period - 1
    value = _mean([dx[i] for i in range(period, first + 1)])
    for i in range(first + 1, n):
        value = (value * (period - 1) + dx[i]) / period
    return value, pdi[n - 1], mdi[n - 1], pdi[n - 2], mdi[n - 2]


def ichimoku(c):
    n = len(c)
    if n < 80:
        return None

    def mid(i, period):
        window = c[i - period + 1:i + 1]
        return (max(x["high"] for x in window) + min(x["low"] for x in window)) / 2

    k = n - 27
    return (
        mid(n - 1, 9), mid(n - 1, 26), mid(n - 2, 9), mid(n - 2, 26),
        (mid(k, 9) + mid(k, 26)) / 2, mid(k, 52),
    )


def heikin_ashi(c):
    out = []
    for i, x in enumerate(c):
        ha_close = (x["open"] + x["high"] + x["low"] + x["close"]) / 4
        ha_open = (x["open"] + x["close"]) / 2 if i == 0 else (out[-1][0] + out[-1][1]) / 2
        out.append((ha_open, ha_close))
    return out


def previous_day(c):
    day_start = c[-1]["time"] - c[-1]["time"] % 86400
    earlier = [x for x in c if x["time"] < day_start]
    if not earlier:
        return []
    start = earlier[-1]["time"] - earlier[-1]["time"] % 86400
    return [x for x in earlier if x["time"] >= start]


def analyze(m, c, atr, strict=False):
    if len(c) < 80 or not atr:
        return []

    closes = [x["close"] for x in c]
    price = closes[-1]
    last = c[-1]
    out = []

    def add(name, direction, sl, reason, confidence=60):
        signal = smc.make_signal(name, direction, price, sl, atr, reason, confidence)
        if signal:
            out.append(signal)

    def low3():
        return min(x["low"] for x in c[-3:]) - 0.25 * atr

    def high3():
        return max(x["high"] for x in c[-3:]) + 0.25 * atr

    def s_supertrend():
        trend, upper, lower = supertrend(c)
        if trend[-1] != trend[-2]:
            if trend[-1] == 1:
                add("SUPERTREND", 1, lower[-1], "সুপারট্রেন্ড বুলিশে ঘুরেছে")
            else:
                add("SUPERTREND", -1, upper[-1], "সুপারট্রেন্ড বেয়ারিশে ঘুরেছে")

    def s_stoch():
        ks, ds = stochastic(c)
        if len(ks) < 2 or ds[-1] is None or ds[-2] is None:
            return
        if ks[-2] <= ds[-2] and ks[-1] > ds[-1] and ks[-1] < 30:
            add("STOCH_CROSS", 1, low3(), f"স্টোকাস্টিক ওভারসোল্ডে ওপরে কেটেছে ({ks[-1]:.0f})")
        elif ks[-2] >= ds[-2] and ks[-1] < ds[-1] and ks[-1] > 70:
            add("STOCH_CROSS", -1, high3(), f"স্টোকাস্টিক ওভারবটে নিচে কেটেছে ({ks[-1]:.0f})")

    def s_adx():
        result = adx(c)
        if not result:
            return
        strength, plus, minus, prev_plus, prev_minus = result
        if strength > 20 and prev_plus <= prev_minus and plus > minus:
            add("ADX_TREND", 1, price - 2 * atr, f"+DI ওপরে কেটেছে, ADX {strength:.0f} (শক্তিশালী ট্রেন্ড)")
        elif strength > 20 and prev_plus >= prev_minus and plus < minus:
            add("ADX_TREND", -1, price + 2 * atr, f"-DI ওপরে কেটেছে, ADX {strength:.0f} (শক্তিশালী ট্রেন্ড)")

    def s_ichimoku():
        result = ichimoku(c)
        if not result:
            return
        tenkan, kijun, prev_tenkan, prev_kijun, span_a, span_b = result
        if prev_tenkan <= prev_kijun and tenkan > kijun and price > max(span_a, span_b):
            add("ICHIMOKU_TK", 1, min(kijun, price - atr), "ইচিমোকু: তেনকান কিজুনের ওপরে, দাম মেঘের ওপরে")
        elif prev_tenkan >= prev_kijun and tenkan < kijun and price < min(span_a, span_b):
            add("ICHIMOKU_TK", -1, max(kijun, price + atr), "ইচিমোকু: তেনকান কিজুনের নিচে, দাম মেঘের নিচে")

    def s_fib():
        highs, lows = smc.find_swings(c)
        if not highs or not lows:
            return
        hi, lo = highs[-1], lows[-1]
        top, bottom = c[hi]["high"], c[lo]["low"]
        rng = top - bottom
        if rng < 2 * atr:
            return
        if lo < hi:
            zone_high, zone_low = top - 0.5 * rng, top - 0.705 * rng
            if last["low"] <= zone_high and last["close"] > zone_low and last["close"] > last["open"]:
                add("FIB_PULLBACK", 1, top - 0.786 * rng - 0.25 * atr, "ফিবোনাচ্চি ৫০-৭০.৫% পুলব্যাক জোনে বুলিশ রিজেকশন")
        else:
            zone_low, zone_high = bottom + 0.5 * rng, bottom + 0.705 * rng
            if last["high"] >= zone_low and last["close"] < zone_high and last["close"] < last["open"]:
                add("FIB_PULLBACK", -1, bottom + 0.786 * rng + 0.25 * atr, "ফিবোনাচ্চি ৫০-৭০.৫% পুলব্যাক জোনে বেয়ারিশ রিজেকশন")

    def s_inside():
        mother, inside = c[-3], c[-2]
        if inside["high"] <= mother["high"] and inside["low"] >= mother["low"]:
            if price > mother["high"]:
                add("INSIDE_BAR", 1, inside["low"] - 0.25 * atr, "ইনসাইড বার ওপরে ভেঙেছে")
            elif price < mother["low"]:
                add("INSIDE_BAR", -1, inside["high"] + 0.25 * atr, "ইনসাইড বার নিচে ভেঙেছে")

    def s_prev_day():
        day = previous_day(c)
        if len(day) < 5:
            return
        high = max(x["high"] for x in day)
        low = min(x["low"] for x in day)
        if price > high and closes[-2] <= high:
            add("PREV_DAY_BREAK", 1, high - atr, f"আগের দিনের হাই {high:.2f} ভেঙেছে")
        elif price < low and closes[-2] >= low:
            add("PREV_DAY_BREAK", -1, low + atr, f"আগের দিনের লো {low:.2f} ভেঙেছে")

    def s_pivot():
        day = previous_day(c)
        if len(day) < 5:
            return
        high = max(x["high"] for x in day)
        low = min(x["low"] for x in day)
        pivot = (high + low + day[-1]["close"]) / 3
        rng = high - low
        supports = [2 * pivot - high, pivot - rng]
        resistances = [2 * pivot - low, pivot + rng]
        if last["close"] > last["open"] and any(
            last["low"] <= s + 0.2 * atr and last["close"] > s and last["low"] >= s - 0.8 * atr for s in supports
        ):
            add("PIVOT_BOUNCE", 1, last["low"] - 0.25 * atr, "ডেইলি পিভট সাপোর্ট থেকে বুলিশ বাউন্স")
        elif last["close"] < last["open"] and any(
            last["high"] >= r - 0.2 * atr and last["close"] < r and last["high"] <= r + 0.8 * atr for r in resistances
        ):
            add("PIVOT_BOUNCE", -1, last["high"] + 0.25 * atr, "ডেইলি পিভট রেজিস্ট্যান্স থেকে বেয়ারিশ বাউন্স")

    def s_ny_open():
        hour = (last["time"] // 3600) % 24
        if not 14 <= hour < 18:
            return
        day_start = last["time"] - last["time"] % 86400
        opening = [x for x in c if x["time"] == day_start + 13 * 3600]
        if not opening:
            return
        first = opening[0]
        since = [x for x in c if day_start + 14 * 3600 <= x["time"] < last["time"]]
        mid = (first["high"] + first["low"]) / 2
        if price > first["high"] and all(x["close"] <= first["high"] for x in since):
            add("NY_OPEN_BREAK", 1, mid, "নিউ ইয়র্ক ওপেনের প্রথম ঘণ্টার হাই ভেঙেছে")
        elif price < first["low"] and all(x["close"] >= first["low"] for x in since):
            add("NY_OPEN_BREAK", -1, mid, "নিউ ইয়র্ক ওপেনের প্রথম ঘণ্টার লো ভেঙেছে")

    def s_keltner():
        ema = m.calculate_ema(closes, 20)
        prev_ema = m.calculate_ema(closes[:-1], 20)
        if ema is None or prev_ema is None:
            return
        if price > ema + 2 * atr and closes[-2] <= prev_ema + 2 * atr:
            add("KELTNER_BREAK", 1, ema, "কেল্টনার চ্যানেলের ওপরের সীমা ভেঙেছে")
        elif price < ema - 2 * atr and closes[-2] >= prev_ema - 2 * atr:
            add("KELTNER_BREAK", -1, ema, "কেল্টনার চ্যানেলের নিচের সীমা ভেঙেছে")

    def s_heikin():
        ha = heikin_ashi(c[-40:])
        bull = [candle[1] > candle[0] for candle in ha]
        if bull[-1] and bull[-2] and not bull[-3]:
            add("HA_TREND", 1, low3(), "হেইকিন-আশি বুলিশে ঘুরেছে")
        elif not bull[-1] and not bull[-2] and bull[-3]:
            add("HA_TREND", -1, high3(), "হেইকিন-আশি বেয়ারিশে ঘুরেছে")

    def s_round():
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        upper_wick = last["high"] - max(last["open"], last["close"])
        below = round(last["low"] / 50) * 50
        above = round(last["high"] / 50) * 50
        if abs(last["low"] - below) <= 0.3 * atr and last["close"] > below and last["close"] > last["open"] and lower_wick >= body:
            add("ROUND_LEVEL", 1, last["low"] - 0.25 * atr, f"রাউন্ড লেভেল {below} থেকে বুলিশ রিজেকশন")
        elif abs(last["high"] - above) <= 0.3 * atr and last["close"] < above and last["close"] < last["open"] and upper_wick >= body:
            add("ROUND_LEVEL", -1, last["high"] + 0.25 * atr, f"রাউন্ড লেভেল {above} থেকে বেয়ারিশ রিজেকশন")

    for strategy in (s_supertrend, s_stoch, s_adx, s_ichimoku, s_fib, s_inside, s_prev_day,
                     s_pivot, s_ny_open, s_keltner, s_heikin, s_round):
        try:
            strategy()
        except Exception:
            if strict:
                raise
    return out
