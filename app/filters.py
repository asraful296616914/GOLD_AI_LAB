COMBOS = ["NONE", "SESSION", "TREND", "SESSION+TREND", "TREND+MOM", "SESSION+TREND+VOL"]
LABELS = {
    "NONE": "ফিল্টার ছাড়া",
    "SESSION": "শুধু লন্ডন/নিউ ইয়র্ক সেশন",
    "TREND": "শুধু বড় ট্রেন্ডের দিকে",
    "SESSION+TREND": "সেশন + ট্রেন্ড",
    "TREND+MOM": "ট্রেন্ড + মোমেন্টাম",
    "SESSION+TREND+VOL": "সেশন + ট্রেন্ড + ভোলাটিলিটি",
}


def ids(combo):
    return [] if combo == "NONE" else combo.split("+")


def flags(m, h1, h4, atr, timestamp):
    closes = [c["close"] for c in h1]
    price = closes[-1]
    ema50 = m.calculate_ema(closes, 50)
    rsi = m.calculate_rsi(closes)

    hour = (timestamp // 3600) % 24
    weekday = (timestamp // 86400 + 3) % 7
    session = weekday < 5 and 7 <= hour < 20

    h4_up = h4_down = None
    if h4 and len(h4) >= 50:
        h4_closes = [c["close"] for c in h4]
        fast = m.calculate_ema(h4_closes, 20)
        slow = m.calculate_ema(h4_closes, 50)
        if fast is not None and slow is not None:
            h4_up, h4_down = fast > slow, fast < slow

    trend_up = ema50 is not None and price > ema50 and (h4_up is None or h4_up)
    trend_down = ema50 is not None and price < ema50 and (h4_down is None or h4_down)

    ranges = [c["high"] - c["low"] for c in h1[-50:]]
    vol_ok = len(ranges) >= 50 and (sum(ranges[-14:]) / 14) >= 0.9 * (sum(ranges) / 50)

    return {
        "SESSION": session,
        "TREND": {"BUY": bool(trend_up), "SELL": bool(trend_down)},
        "MOM": {"BUY": rsi is not None and rsi > 50, "SELL": rsi is not None and rsi < 50},
        "VOL": bool(vol_ok),
    }


def passes(filter_ids, fl, direction):
    for fid in filter_ids:
        if fid == "SESSION" and not fl["SESSION"]:
            return False
        if fid in ("TREND", "MOM") and not fl[fid][direction]:
            return False
        if fid == "VOL" and not fl["VOL"]:
            return False
    return True
