import math

from app import smc, classic2

MIN_RR = 1.0
MIN_RISK_ATR = 0.6
MAX_RISK_ATR = 2.5


def _targets(c, direction, entry):
    d = 1 if direction == "BUY" else -1
    found = []
    highs, lows = smc.find_swings(c)
    for i in (highs if d == 1 else lows)[-12:]:
        level = c[i]["high"] if d == 1 else c[i]["low"]
        if d * (level - entry) > 0:
            found.append((d * (level - entry), "swing", level))

    day = classic2.previous_day(c)
    if len(day) >= 5:
        high = max(x["high"] for x in day)
        low = min(x["low"] for x in day)
        pivot = (high + low + day[-1]["close"]) / 3
        spread = high - low
        for level, kind in (
            (high, "prev_day"), (low, "prev_day"), (pivot, "pivot"), (2 * pivot - low, "pivot"),
            (2 * pivot - high, "pivot"), (pivot + spread, "pivot"), (pivot - spread, "pivot"),
        ):
            if d * (level - entry) > 0:
                found.append((d * (level - entry), kind, level))

    round_level = (math.floor(entry / 50) + 1) * 50 if d == 1 else (math.ceil(entry / 50) - 1) * 50
    found.append((d * (round_level - entry), "round", round_level))
    found.sort(key=lambda item: item[0])
    return found


def adaptive(c, direction, entry, sl_hint, atr, now_ts, hold_bars):
    d = 1 if direction == "BUY" else -1
    if (d == 1 and sl_hint >= entry) or (d == -1 and sl_hint <= entry):
        risk = 1.2 * atr
    else:
        risk = abs(entry - sl_hint)
    risk = max(risk, MIN_RISK_ATR * atr)
    if risk > MAX_RISK_ATR * atr:
        return None

    ranges = [x["high"] - x["low"] for x in c[-50:]]
    vol_ratio = 1.0
    if len(ranges) >= 50:
        vol_ratio = min(1.3, max(0.7, (sum(ranges[-14:]) / 14) / (sum(ranges) / 50)))
    hour = (now_ts // 3600) % 24
    session = 0.7 if (hour < 7 or hour >= 21) else (1.1 if 12 <= hour < 16 else 1.0)
    reach = 0.8 * atr * math.sqrt(max(1, hold_bars)) * vol_ratio * session
    minimum = MIN_RR * risk
    if reach < minimum:
        return None

    tp_dist, kind = None, None
    for dist, level_kind, _ in _targets(c, direction, entry):
        candidate = dist - 0.1 * atr
        if candidate < minimum:
            continue
        if candidate > reach:
            break
        tp_dist, kind = candidate, level_kind
        break
    if tp_dist is None:
        tp_dist, kind = min(reach, 1.5 * risk), "volatility"
        if tp_dist < minimum:
            return None

    return {
        "stop_loss": entry - d * risk, "take_profit": entry + d * tp_dist,
        "rr": tp_dist / risk, "target": kind,
    }
