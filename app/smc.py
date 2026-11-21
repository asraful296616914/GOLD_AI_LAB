K = 3
MAX_AGE = 90
RR = 2.0

KIND_BN = {"OB": "অর্ডার ব্লক", "FVG": "FVG", "SD": "সাপ্লাই/ডিমান্ড"}


def dir_bn(direction):
    return "বুলিশ" if direction == 1 else "বেয়ারিশ"


def find_swings(c, k=K):
    highs, lows = [], []
    for i in range(k, len(c) - k):
        high, low = c[i]["high"], c[i]["low"]
        if all(high > c[j]["high"] for j in range(i - k, i)) and all(high >= c[j]["high"] for j in range(i + 1, i + k + 1)):
            highs.append(i)
        if all(low < c[j]["low"] for j in range(i - k, i)) and all(low <= c[j]["low"] for j in range(i + 1, i + k + 1)):
            lows.append(i)
    return highs, lows


def find_structure(c, highs, lows, k=K):
    hi_set, lo_set = set(highs), set(lows)
    events = []
    trend = 0
    last_high = None
    last_low = None
    ref_high = None
    ref_low = None

    for i, bar in enumerate(c):
        j = i - k
        if j >= 0 and j in hi_set:
            last_high = (j, c[j]["high"])
            ref_high = last_high
        if j >= 0 and j in lo_set:
            last_low = (j, c[j]["low"])
            ref_low = last_low

        if last_high is not None and bar["close"] > last_high[1]:
            events.append({
                "index": i, "time": bar["time"],
                "kind": "CHoCH" if trend == -1 else "BOS",
                "direction": 1, "level": last_high[1],
                "ref": ref_low[1] if ref_low else None,
                "ref_index": ref_low[0] if ref_low else None,
            })
            trend = 1
            last_high = None
        elif last_low is not None and bar["close"] < last_low[1]:
            events.append({
                "index": i, "time": bar["time"],
                "kind": "CHoCH" if trend == 1 else "BOS",
                "direction": -1, "level": last_low[1],
                "ref": ref_high[1] if ref_high else None,
                "ref_index": ref_high[0] if ref_high else None,
            })
            trend = -1
            last_low = None
    return events


def make_zone(kind, direction, bar, index, from_index):
    return {
        "kind": kind, "direction": direction,
        "top": bar["high"], "bottom": bar["low"],
        "index": index, "time": bar["time"], "from_index": from_index,
    }


def find_order_blocks(c, events):
    zones = []
    for ev in events:
        i = ev["index"]
        start = ev["ref_index"] if ev["ref_index"] is not None else i - 10
        start = max(0, start, i - 15)
        for j in range(i - 1, start - 1, -1):
            bar = c[j]
            if ev["direction"] == 1 and bar["close"] < bar["open"]:
                zones.append(make_zone("OB", 1, bar, j, i))
                break
            if ev["direction"] == -1 and bar["close"] > bar["open"]:
                zones.append(make_zone("OB", -1, bar, j, i))
                break
    return zones


def find_fvgs(c, atr):
    zones = []
    for i in range(2, len(c)):
        a, b = c[i - 2], c[i]
        if a["high"] < b["low"] and (b["low"] - a["high"]) >= 0.3 * atr:
            zones.append({"kind": "FVG", "direction": 1, "top": b["low"], "bottom": a["high"],
                          "index": i - 1, "time": c[i - 1]["time"], "from_index": i})
        elif a["low"] > b["high"] and (a["low"] - b["high"]) >= 0.3 * atr:
            zones.append({"kind": "FVG", "direction": -1, "top": a["low"], "bottom": b["high"],
                          "index": i - 1, "time": c[i - 1]["time"], "from_index": i})
    return zones


def find_supply_demand(c, atr):
    zones = []
    for i in range(1, len(c)):
        bar = c[i]
        base = c[i - 1]
        body = abs(bar["close"] - bar["open"])
        if body >= 1.5 * atr and (base["high"] - base["low"]) <= atr:
            direction = 1 if bar["close"] > bar["open"] else -1
            zones.append({"kind": "SD", "direction": direction, "top": base["high"], "bottom": base["low"],
                          "index": i - 1, "time": base["time"], "from_index": i})
    return zones


def zone_alive(c, zone):
    if len(c) - zone["index"] > MAX_AGE:
        return False
    for bar in c[zone["from_index"] + 1:]:
        if zone["direction"] == 1 and bar["close"] < zone["bottom"]:
            return False
        if zone["direction"] == -1 and bar["close"] > zone["top"]:
            return False
    return True


def zone_retest(c, zone):
    bar = c[-1]
    if zone["from_index"] >= len(c) - 2:
        return False
    if zone["direction"] == 1:
        return bar["low"] <= zone["top"] and bar["close"] >= zone["bottom"] and bar["close"] > bar["open"]
    return bar["high"] >= zone["bottom"] and bar["close"] <= zone["top"] and bar["close"] < bar["open"]


def find_sweeps(c, highs, lows, k=K, lookback=20, max_age=60):
    n = len(c)
    out = []
    for i in range(max(k + 2, n - lookback), n):
        bar = c[i]
        for s in reversed(highs):
            if s > i - 2 or i - s > max_age:
                continue
            level = c[s]["high"]
            if any(c[x]["close"] > level for x in range(s + 1, i)):
                continue
            if bar["high"] > level and bar["close"] < level:
                out.append({"index": i, "time": bar["time"], "direction": -1, "level": level})
                break
        for s in reversed(lows):
            if s > i - 2 or i - s > max_age:
                continue
            level = c[s]["low"]
            if any(c[x]["close"] < level for x in range(s + 1, i)):
                continue
            if bar["low"] < level and bar["close"] > level:
                out.append({"index": i, "time": bar["time"], "direction": 1, "level": level})
                break
    return out


def range_position(c, highs, lows):
    if not highs or not lows:
        return None
    high = c[highs[-1]]["high"]
    low = c[lows[-1]]["low"]
    if high <= low:
        return None
    eq = (high + low) / 2
    return {
        "high": high, "low": low, "eq": eq,
        "position": "discount" if c[-1]["close"] < eq else "premium",
    }


def confidence_score(direction, ctx, base=50):
    score = base
    if ctx["trend"] == direction:
        score += 15
    rng = ctx["range"]
    if rng and ((direction == 1 and rng["position"] == "discount") or (direction == -1 and rng["position"] == "premium")):
        score += 15
    if any(s["direction"] == direction for s in ctx["recent_sweeps"]):
        score += 10
    if ctx["h4_trend"] == direction:
        score += 10
    return score


def make_signal(strategy, direction, price, sl, atr, reason, confidence):
    if (direction == 1 and sl >= price) or (direction == -1 and sl <= price):
        sl = price - direction * 1.5 * atr
    risk = abs(price - sl)
    floor = 0.5 * atr
    if risk < floor:
        sl = price - direction * floor
        risk = floor
    if risk > 4 * atr:
        return None
    tp = price + direction * RR * risk
    return {
        "strategy": strategy,
        "direction": "BUY" if direction == 1 else "SELL",
        "entry": price, "stop_loss": sl, "take_profit": tp,
        "confidence": int(max(0, min(100, confidence))),
        "reason": reason,
    }


def analyze(c, atr, h4=None):
    empty = {"signals": [], "zones": [], "events": [], "sweeps": [],
             "context": {"h1_trend": None, "h4_trend": None, "range": None}}
    if not c or len(c) < 30 or not atr:
        return empty

    n = len(c)
    price = c[-1]["close"]
    highs, lows = find_swings(c)
    events = find_structure(c, highs, lows)
    sweeps = find_sweeps(c, highs, lows)
    rng = range_position(c, highs, lows)
    trend = events[-1]["direction"] if events else 0

    h4_trend = 0
    if h4 and len(h4) >= 30:
        h4_highs, h4_lows = find_swings(h4)
        h4_events = find_structure(h4, h4_highs, h4_lows)
        h4_trend = h4_events[-1]["direction"] if h4_events else 0

    ctx = {"trend": trend, "h4_trend": h4_trend, "range": rng, "recent_sweeps": sweeps}

    all_zones = find_order_blocks(c, events) + find_fvgs(c, atr) + find_supply_demand(c, atr)
    zones = [z for z in all_zones if zone_alive(c, z)]
    signals = []

    def add(strategy, direction, sl, reason):
        sig = make_signal(strategy, direction, price, sl, atr, reason, confidence_score(direction, ctx, 50))
        if sig:
            signals.append(sig)
        return sig

    if events and events[-1]["index"] >= n - 3:
        ev = events[-1]
        d = ev["direction"]
        ref = ev["ref"] if ev["ref"] is not None else price - d * 1.5 * atr
        add("SMC_STRUCTURE", d, ref - d * 0.25 * atr, f"{ev['kind']} {dir_bn(d)} (স্ট্রাকচার ব্রেক)")

    recent_sweeps = [s for s in sweeps if s["index"] >= n - 2]
    if recent_sweeps:
        sw = recent_sweeps[-1]
        d = sw["direction"]
        segment = c[sw["index"]:]
        extreme = max(x["high"] for x in segment) if d == -1 else min(x["low"] for x in segment)
        add("SMC_SWEEP", d, extreme - d * 0.25 * atr, f"লিকুইডিটি সুইপ ({dir_bn(d)} রিভার্সাল), লেভেল {sw['level']:.2f}")

    zone_hits = {}
    for kind in ("OB", "FVG", "SD"):
        hits = [z for z in zones if z["kind"] == kind and zone_retest(c, z)]
        if not hits:
            continue
        z = max(hits, key=lambda item: item["index"])
        d = z["direction"]
        far = z["bottom"] if d == 1 else z["top"]
        sig = add(f"SMC_{kind}", d, far - d * 0.25 * atr,
                  f"{KIND_BN[kind]} জোনে রিটেস্ট ({dir_bn(d)}), জোন {z['bottom']:.2f}-{z['top']:.2f}")
        if sig:
            zone_hits[kind] = (d, sig)

    pos = rng["position"] if rng else None
    for kind in ("OB", "FVG"):
        if kind not in zone_hits:
            continue
        d, base_sig = zone_hits[kind]
        pd_ok = (d == 1 and pos == "discount") or (d == -1 and pos == "premium")
        if trend == d and pd_ok and h4_trend != -d:
            pd_text = "ডিসকাউন্ট" if d == 1 else "প্রিমিয়াম"
            sig = make_signal("SMC_V1", d, price, base_sig["stop_loss"], atr,
                              f"ট্রেন্ড {dir_bn(d)} + {pd_text} জোন + {KIND_BN[kind]} রিটেস্ট",
                              confidence_score(d, ctx, 60))
            if sig:
                signals.append(sig)
            break

    zone_out = []
    for kind in ("OB", "FVG", "SD"):
        items = sorted((z for z in zones if z["kind"] == kind), key=lambda item: item["index"], reverse=True)[:3]
        for z in items:
            zone_out.append({
                "kind": z["kind"], "direction": "bull" if z["direction"] == 1 else "bear",
                "top": z["top"], "bottom": z["bottom"], "time": z["time"], "index": z["index"],
            })

    return {
        "signals": signals,
        "zones": zone_out,
        "events": [{"time": e["time"], "kind": e["kind"], "direction": e["direction"], "level": e["level"]} for e in events[-6:]],
        "sweeps": [{"time": s["time"], "direction": s["direction"], "level": s["level"]} for s in sweeps[-4:]],
        "context": {
            "h1_trend": "bull" if trend == 1 else "bear" if trend == -1 else None,
            "h4_trend": "bull" if h4_trend == 1 else "bear" if h4_trend == -1 else None,
            "range": rng,
        },
    }
