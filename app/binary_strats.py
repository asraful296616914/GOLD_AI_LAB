"""Extra strategies for the binary page: FVG retest and strong (multi-touch) support/resistance.

Both only read candles up to the last closed one (c[-1]), so the backtest has no look-ahead.
"""


def _ema(values, period):
    k = 2.0 / (period + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def fvg_retest(c, atr):
    """First retest of a fresh, still-open fair value gap, in the direction that created it."""
    n = len(c)
    if n < 60 or not atr:
        return None
    ema = _ema([x["close"] for x in c[-150:]], 50)[-1]
    last = c[-1]
    for k in range(n - 2, max(2, n - 61), -1):
        first, third = c[k - 2], c[k]
        if third["low"] > first["high"]:
            lo, hi, direction = first["high"], third["low"], "BUY"
        elif third["high"] < first["low"]:
            lo, hi, direction = third["high"], first["low"], "SELL"
        else:
            continue
        if hi - lo < 0.3 * atr:
            continue
        between = c[k + 1:n - 1]
        if direction == "BUY":
            if any(x["close"] < lo or x["low"] <= hi for x in between):
                continue  # already filled or already retested once
            if last["low"] <= hi and last["close"] > last["open"] and last["close"] > (lo + hi) / 2 and last["close"] > ema:
                return {"strategy": "FVG_RETEST", "direction": "BUY", "reason": "বুলিশ FVG-তে প্রথম রিটেস্ট"}
        else:
            if any(x["close"] > hi or x["high"] >= lo for x in between):
                continue
            if last["high"] >= lo and last["close"] < last["open"] and last["close"] < (lo + hi) / 2 and last["close"] < ema:
                return {"strategy": "FVG_RETEST", "direction": "SELL", "reason": "বেয়ারিশ FVG-তে প্রথম রিটেস্ট"}
    return None


def _levels(look, atr):
    """Price levels the market turned at least 3 times (fractal swing highs/lows clustered together)."""
    m = len(look)
    prices = []
    for j in range(3, m - 4):
        hj, lj = look[j]["high"], look[j]["low"]
        if all(hj >= look[j + d]["high"] and hj >= look[j - d]["high"] for d in (1, 2, 3)):
            prices.append(hj)
        if all(lj <= look[j + d]["low"] and lj <= look[j - d]["low"] for d in (1, 2, 3)):
            prices.append(lj)
    prices.sort()
    clusters, current = [], []
    for p in prices:
        if current and p - current[0] > 0.5 * atr:
            clusters.append(current)
            current = []
        current.append(p)
    if current:
        clusters.append(current)
    return sorted(((sum(g) / len(g), len(g)) for g in clusters if len(g) >= 3), key=lambda x: -x[1])


def strong_sr(c, atr):
    """Rejection (pin bar / engulfing) at a level that has held at least 3 times."""
    n = len(c)
    if n < 120 or not atr:
        return None
    look = c[-220:]
    last, prev = look[-1], look[-2]
    rng = last["high"] - last["low"]
    if rng <= 0:
        return None
    lower = min(last["open"], last["close"]) - last["low"]
    upper = last["high"] - max(last["open"], last["close"])
    bull = last["close"] > last["open"] and (
        lower >= 0.55 * rng or (prev["close"] < prev["open"] and last["close"] >= prev["open"] and last["open"] <= prev["close"]))
    bear = last["close"] < last["open"] and (
        upper >= 0.55 * rng or (prev["close"] > prev["open"] and last["close"] <= prev["open"] and last["open"] >= prev["close"]))
    if not (bull or bear):
        return None
    approach = look[-6]["close"]
    for level, touches in _levels(look[:-1], atr):
        if bull and level - 0.6 * atr <= last["low"] <= level + 0.2 * atr and last["close"] > level and approach > level:
            return {"strategy": "STRONG_SR", "direction": "BUY", "reason": f"{touches} বার ফেরা সাপোর্টে রিজেকশন"}
        if bear and level - 0.2 * atr <= last["high"] <= level + 0.6 * atr and last["close"] < level and approach < level:
            return {"strategy": "STRONG_SR", "direction": "SELL", "reason": f"{touches} বার ফেরা রেজিস্ট্যান্সে রিজেকশন"}
    return None


def analyze(window, atr):
    out = []
    for fn in (fvg_retest, strong_sr):
        try:
            item = fn(window, atr)
        except Exception:
            item = None
        if item:
            out.append(item)
    return out
