from app import smc


def analyze(m, c, atr, strict=False):
    if len(c) < 40 or not atr:
        return []

    last = c[-1]
    price = last["close"]
    out = []

    def add(name, direction, sl, reason, confidence=60):
        signal = smc.make_signal(name, direction, price, sl, atr, reason, confidence)
        if signal:
            out.append(signal)

    def body(x):
        return abs(x["close"] - x["open"])

    def span(x):
        return x["high"] - x["low"]

    def bull(x):
        return x["close"] > x["open"]

    def bear(x):
        return x["close"] < x["open"]

    prior = c[-9:-1]
    prior_low = min(x["low"] for x in prior)
    prior_high = max(x["high"] for x in prior)
    dropped = max(x["close"] for x in prior) - c[-2]["close"] >= atr
    rallied = c[-2]["close"] - min(x["close"] for x in prior) >= atr

    def engulfing():
        prev = c[-2]
        if dropped and bear(prev) and bull(last) and last["close"] > prev["open"] and last["open"] <= prev["close"] \
                and body(last) >= 1.1 * body(prev) and span(last) >= 0.8 * atr:
            add("ENGULFING", 1, min(last["low"], prev["low"]) - 0.15 * atr, "ডাউনমুভের পর বুলিশ এনগাল্ফিং")
        elif rallied and bull(prev) and bear(last) and last["close"] < prev["open"] and last["open"] >= prev["close"] \
                and body(last) >= 1.1 * body(prev) and span(last) >= 0.8 * atr:
            add("ENGULFING", -1, max(last["high"], prev["high"]) + 0.15 * atr, "র‍্যালির পর বেয়ারিশ এনগাল্ফিং")

    def star():
        first, middle = c[-3], c[-2]
        if body(first) < 0.8 * atr or body(middle) > max(0.3 * atr, 0.35 * body(first)):
            return
        first_mid = (first["open"] + first["close"]) / 2
        if bear(first) and bull(last) and last["close"] > first_mid and body(last) >= 0.5 * atr:
            add("STAR_REVERSAL", 1, min(middle["low"], last["low"]) - 0.15 * atr, "মর্নিং স্টার (বুলিশ রিভার্সাল)")
        elif bull(first) and bear(last) and last["close"] < first_mid and body(last) >= 0.5 * atr:
            add("STAR_REVERSAL", -1, max(middle["high"], last["high"]) + 0.15 * atr, "ইভনিং স্টার (বেয়ারিশ রিভার্সাল)")

    def hammer():
        rng = span(last)
        if rng <= 0:
            return
        lower = min(last["open"], last["close"]) - last["low"]
        upper = last["high"] - max(last["open"], last["close"])
        small = body(last) <= 0.35 * rng
        if small and lower >= 2 * body(last) and lower >= 0.5 * rng and upper <= 0.25 * rng \
                and last["low"] <= prior_low + 0.2 * atr:
            add("HAMMER_STAR", 1, last["low"] - 0.15 * atr, "লোয়ে হ্যামার (নিচ থেকে প্রত্যাখ্যান)")
        elif small and upper >= 2 * body(last) and upper >= 0.5 * rng and lower <= 0.25 * rng \
                and last["high"] >= prior_high - 0.2 * atr:
            add("HAMMER_STAR", -1, last["high"] + 0.15 * atr, "হাইয়ে শুটিং স্টার (ওপর থেকে প্রত্যাখ্যান)")

    def soldiers():
        a, b, d = c[-3], c[-2], c[-1]
        if all(bull(x) and body(x) >= 0.4 * atr and (x["high"] - x["close"]) <= 0.35 * body(x) for x in (a, b, d)) \
                and b["close"] > a["close"] and d["close"] > b["close"] and d["close"] - a["open"] <= 3.5 * atr:
            add("THREE_SOLDIERS", 1, a["low"] - 0.15 * atr, "থ্রি হোয়াইট সোলজার্স (শক্তিশালী ঊর্ধ্বমুখী ধারা)")
        elif all(bear(x) and body(x) >= 0.4 * atr and (x["close"] - x["low"]) <= 0.35 * body(x) for x in (a, b, d)) \
                and b["close"] < a["close"] and d["close"] < b["close"] and a["open"] - d["close"] <= 3.5 * atr:
            add("THREE_SOLDIERS", -1, a["high"] + 0.15 * atr, "থ্রি ব্ল্যাক ক্রো (শক্তিশালী নিম্নমুখী ধারা)")

    def piercing():
        prev = c[-2]
        mid = (prev["open"] + prev["close"]) / 2
        if dropped and bear(prev) and body(prev) >= 0.7 * atr and bull(last) and last["open"] <= prev["close"] \
                and mid < last["close"] < prev["open"]:
            add("PIERCING_CLOUD", 1, last["low"] - 0.15 * atr, "পিয়ার্সিং লাইন (বুলিশ)")
        elif rallied and bull(prev) and body(prev) >= 0.7 * atr and bear(last) and last["open"] >= prev["close"] \
                and prev["open"] < last["close"] < mid:
            add("PIERCING_CLOUD", -1, last["high"] + 0.15 * atr, "ডার্ক ক্লাউড কভার (বেয়ারিশ)")

    def tweezer():
        prev = c[-2]
        if abs(last["low"] - prev["low"]) <= 0.1 * atr and bear(prev) and bull(last) \
                and last["low"] <= prior_low + 0.2 * atr:
            add("TWEEZER", 1, min(last["low"], prev["low"]) - 0.15 * atr, "টুইজার বটম (একই লো-তে দুইবার প্রত্যাখ্যান)")
        elif abs(last["high"] - prev["high"]) <= 0.1 * atr and bull(prev) and bear(last) \
                and last["high"] >= prior_high - 0.2 * atr:
            add("TWEEZER", -1, max(last["high"], prev["high"]) + 0.15 * atr, "টুইজার টপ (একই হাই-তে দুইবার প্রত্যাখ্যান)")

    def marubozu():
        rng = span(last)
        if rng <= 0 or body(last) < 1.2 * atr or body(last) < 0.85 * rng:
            return
        ten_high = max(x["high"] for x in c[-11:-1])
        ten_low = min(x["low"] for x in c[-11:-1])
        if bull(last) and last["close"] > ten_high:
            add("MARUBOZU_BREAK", 1, last["low"] - 0.1 * atr, "শক্তিশালী মারুবোজু ক্যান্ডেলে ব্রেকআউট (ওপরে)")
        elif bear(last) and last["close"] < ten_low:
            add("MARUBOZU_BREAK", -1, last["high"] + 0.1 * atr, "শক্তিশালী মারুবোজু ক্যান্ডেলে ব্রেকডাউন (নিচে)")

    def gaps():
        n = len(c)
        for k in range(n - 1, n - 4, -1):
            gap = c[k]["open"] - c[k - 1]["close"]
            if abs(gap) < 0.5 * atr or abs(gap) > 4 * atr:
                continue
            fill = c[k - 1]["close"]
            since = c[k:]
            if gap > 0:
                if price < c[k]["open"] and price - fill > 0.25 * atr and bear(last):
                    add("GAP_FILL", -1, max(x["high"] for x in since) + 0.15 * atr, f"গ্যাপ-আপ ভরাটের দিকে ফিরছে (লক্ষ্য {fill:.2f})")
                elif price > c[k]["high"] and bull(last) and min(x["low"] for x in since) > fill + 0.5 * gap:
                    add("GAP_GO", 1, fill + 0.5 * gap - 0.15 * atr, "গ্যাপ-আপ ধরে রেখে আরও ওপরে যাচ্ছে")
            else:
                if price > c[k]["open"] and fill - price > 0.25 * atr and bull(last):
                    add("GAP_FILL", 1, min(x["low"] for x in since) - 0.15 * atr, f"গ্যাপ-ডাউন ভরাটের দিকে ফিরছে (লক্ষ্য {fill:.2f})")
                elif price < c[k]["low"] and bear(last) and max(x["high"] for x in since) < fill + 0.5 * gap:
                    add("GAP_GO", -1, fill + 0.5 * gap + 0.15 * atr, "গ্যাপ-ডাউন ধরে রেখে আরও নিচে যাচ্ছে")
            break

    for strategy in (engulfing, star, hammer, soldiers, piercing, tweezer, marubozu, gaps):
        try:
            strategy()
        except Exception:
            if strict:
                raise
    return out
