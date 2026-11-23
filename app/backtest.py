import os
import time
import json
import bisect
import asyncio
import logging

from fastapi import APIRouter, Query

from app import filters

logger = logging.getLogger("gold-ai-lab.backtest")
router = APIRouter()

SPREAD = float(os.getenv("BACKTEST_SPREAD", "0.30"))
MAX_HOLD_HOURS = float(os.getenv("BACKTEST_MAX_HOLD_HOURS", "3"))
RR_LIST = (2.0, 3.0)
MIN_TRADES = 30
MIN_WIN = float(os.getenv("MIN_WIN_RATE", "50"))
MAX_EXPIRED_SHARE = 0.4
ADAPT = "ADAPT"


def parse_rr(text):
    return ADAPT if text == ADAPT else float(text)


def rr_label(rr):
    return "AI অ্যাডাপ্টিভ" if rr == ADAPT else f"১:{rr:g}"
TF_CONFIG = {
    "5": {"window": 400, "htf": "60", "max_days": 45, "label": "M5"},
    "15": {"window": 200, "htf": "60", "max_days": 120, "label": "M15"},
    "60": {"window": 160, "htf": "240", "max_days": 180, "label": "H1"},
}
STRATEGY_ORDER = [
    "RULES_V1", "EMA_CROSS", "MACD_CROSS", "DONCHIAN", "SESSION_BREAKOUT", "RSI_REVERSAL",
    "BB_BOUNCE", "SR_CANDLE", "TREND_PULLBACK",
    "SUPERTREND", "STOCH_CROSS", "ADX_TREND", "ICHIMOKU_TK", "FIB_PULLBACK", "INSIDE_BAR",
    "PREV_DAY_BREAK", "PIVOT_BOUNCE", "NY_OPEN_BREAK", "KELTNER_BREAK", "HA_TREND", "ROUND_LEVEL",
    "ENGULFING", "STAR_REVERSAL", "HAMMER_STAR", "THREE_SOLDIERS", "PIERCING_CLOUD", "TWEEZER",
    "MARUBOZU_BREAK", "GAP_FILL", "GAP_GO",
    "SMC_STRUCTURE", "SMC_SWEEP", "SMC_OB", "SMC_FVG", "SMC_SD", "SMC_V1",
]
STATE = {"status": "idle", "progress": 0, "started_at": None, "error": None, "result": None}

DDL = (
    "CREATE TABLE IF NOT EXISTS ai_backtest ("
    "id BIGSERIAL PRIMARY KEY, created_at BIGINT NOT NULL, result TEXT NOT NULL)"
)


async def fetch_range(m, symbol, resolution, start, end):
    headers = m.get_pyth_headers()
    endpoint = f"/{m.PYTH_CHANNEL}/history"
    response = await m.pyth_get(
        endpoint,
        params={"symbol": symbol, "from": start, "to": end, "resolution": resolution},
        headers=headers,
        timeout=30,
    )
    if response.status_code != 200:
        m.handle_pyth_error(response, endpoint)
    data = response.json()
    if not isinstance(data, dict) or data.get("s") != "ok":
        return []
    t, o, h, l, c = (data.get(key) or [] for key in ("t", "o", "h", "l", "c"))
    if len({len(t), len(o), len(h), len(l), len(c)}) != 1:
        return []
    return [
        {"time": int(t[i]), "open": float(o[i]), "high": float(h[i]), "low": float(l[i]), "close": float(c[i])}
        for i in range(len(t))
    ]


async def load_history(m, symbol, resolution, days):
    step = int(resolution) * 60 * 450
    end = int(time.time())
    limit = end - days * 86400
    out = {}
    cursor = end
    while cursor > limit:
        start = max(limit, cursor - step)
        try:
            chunk = await fetch_range(m, symbol, resolution, start, cursor)
        except Exception:
            logger.exception("Backtest history chunk failed")
            if not out:
                raise
            break
        for candle in chunk:
            out[candle["time"]] = candle
        cursor = start
        await asyncio.sleep(0.3)
    return [out[key] for key in sorted(out)]


def simulate(m, sg, smc, classic, classic2, patterns, levels, base, htf, spread, tf, progress=None):
    cfg = TF_CONFIG[tf]
    bar_minutes = int(tf)
    htf_minutes = int(cfg["htf"])
    max_hold_bars = max(1, int(MAX_HOLD_HOURS * 60 / bar_minutes))
    window_size = cfg["window"]
    htf_offset = (htf_minutes - bar_minutes) * 60
    htf_times = [c["time"] for c in htf]
    combo_ids = {fid: filters.ids(fid) for fid in filters.COMBOS}
    trades = []
    open_trades = {}
    n = len(base)

    for i in range(50, n):
        bar = base[i]

        for key in list(open_trades):
            t = open_trades[key]
            direction = 1 if t["direction"] == "BUY" else -1
            if direction == 1:
                hit_sl = bar["low"] <= t["stop_loss"]
                hit_tp = bar["high"] >= t["take_profit"]
            else:
                hit_sl = bar["high"] >= t["stop_loss"]
                hit_tp = bar["low"] <= t["take_profit"]
            outcome = None
            gross = 0.0
            exit_price = bar["close"]
            if hit_sl:
                outcome, gross, exit_price = "LOSS", -1.0, t["stop_loss"]
            elif hit_tp:
                outcome, gross, exit_price = "WIN", t["rr"], t["take_profit"]
            elif i - t["opened_i"] >= max_hold_bars:
                outcome, gross = "EXPIRED", (bar["close"] - t["entry"]) * direction / t["risk"]
            if outcome:
                trades.append({
                    "strategy": t["strategy"], "filters": t["fid"], "rr": t["rr"], "variant": t["variant"],
                    "direction": t["direction"], "entry": t["entry"],
                    "stop_loss": t["stop_loss"], "take_profit": t["take_profit"], "exit": exit_price,
                    "outcome": outcome, "r": round(gross - spread / t["risk"], 3),
                    "opened_at": t["opened_at"], "closed_at": bar["time"],
                })
                del open_trades[key]

        window = base[max(0, i - window_size + 1): i + 1]
        atr = m.calculate_atr(window)
        if atr:
            htf_end = bisect.bisect_right(htf_times, bar["time"] - htf_offset)
            htf_window = htf[max(0, htf_end - 160): htf_end] if htf else None
            try:
                flag_set = filters.flags(m, window, htf_window, atr, bar["time"])
            except Exception:
                flag_set = None
            candidates = []
            for source in (
                lambda: smc.analyze(window, atr, htf_window)["signals"],
                lambda: classic.analyze(m, window, atr),
                lambda: classic2.analyze(m, window, atr),
                lambda: patterns.analyze(m, window, atr),
            ):
                try:
                    candidates.extend(source())
                except Exception:
                    pass
            try:
                rule = sg.rules_signal(m, window)
                if rule and rule["direction"] in ("BUY", "SELL"):
                    sl, tp = sg.build_levels(rule["direction"], rule["price"], atr)
                    candidates.append({
                        "strategy": "RULES_V1", "direction": rule["direction"],
                        "entry": rule["price"], "stop_loss": sl, "take_profit": tp,
                    })
            except Exception:
                pass

            if flag_set:
                for item in candidates:
                    risk = abs(item["entry"] - item["stop_loss"])
                    if risk <= 0:
                        continue
                    direction = 1 if item["direction"] == "BUY" else -1
                    try:
                        adapt = levels.adaptive(
                            window, item["direction"], item["entry"], item["stop_loss"], atr, bar["time"], max_hold_bars
                        )
                    except Exception:
                        adapt = None
                    for fid, ids in combo_ids.items():
                        if not filters.passes(ids, flag_set, item["direction"]):
                            continue
                        for rr in RR_LIST:
                            key = (item["strategy"], fid, rr)
                            if key in open_trades:
                                continue
                            open_trades[key] = {
                                "strategy": item["strategy"], "fid": fid, "rr": rr, "variant": rr,
                                "direction": item["direction"], "entry": item["entry"],
                                "stop_loss": item["stop_loss"],
                                "take_profit": item["entry"] + direction * rr * risk,
                                "risk": risk, "opened_i": i, "opened_at": bar["time"],
                            }
                        if adapt:
                            key = (item["strategy"], fid, ADAPT)
                            if key not in open_trades:
                                open_trades[key] = {
                                    "strategy": item["strategy"], "fid": fid, "rr": adapt["rr"], "variant": ADAPT,
                                    "direction": item["direction"], "entry": item["entry"],
                                    "stop_loss": adapt["stop_loss"], "take_profit": adapt["take_profit"],
                                    "risk": abs(item["entry"] - adapt["stop_loss"]),
                                    "opened_i": i, "opened_at": bar["time"],
                                }

        if progress and i % 25 == 0:
            progress(i / n)

    return trades


def metrics(trades):
    count = len(trades)
    if not count:
        return {"trades": 0, "wins": 0, "losses": 0, "expired": 0, "win_rate": None,
                "expectancy_r": None, "profit_factor": None, "net_r": 0, "max_dd_r": 0,
                "avg_hold_min": None}
    ordered = sorted(trades, key=lambda t: t["closed_at"])
    rs = [t["r"] for t in ordered]
    wins = sum(1 for t in ordered if t["outcome"] == "WIN")
    losses = sum(1 for t in ordered if t["outcome"] == "LOSS")
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = -sum(r for r in rs if r < 0)
    cumulative = peak = drawdown = 0.0
    for r in rs:
        cumulative += r
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return {
        "trades": count, "wins": wins, "losses": losses, "expired": count - wins - losses,
        "win_rate": round(wins / (wins + losses) * 100, 1) if wins + losses else None,
        "expectancy_r": round(sum(rs) / count, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "net_r": round(sum(rs), 2), "max_dd_r": round(drawdown, 2),
        "avg_hold_min": round(sum(t["closed_at"] - t["opened_at"] for t in ordered) / count / 60),
    }


def compute_approved(rows, trading_days=None):
    approved = {}
    for row in rows:
        best = None
        for key, v in (row.get("variants") or {}).items():
            a, first, second = v["all"], v["first"], v["second"]
            if a["trades"] < MIN_TRADES or a["win_rate"] is None or a["win_rate"] < MIN_WIN:
                continue
            if (a["expectancy_r"] or 0) <= 0.1 or (a["profit_factor"] or 0) < 1.3:
                continue
            if a["expired"] / a["trades"] > MAX_EXPIRED_SHARE:
                continue
            if any(h["trades"] < 10 or (h["expectancy_r"] or 0) <= 0 for h in (first, second)):
                continue
            if best is None or a["expectancy_r"] > best[1]["all"]["expectancy_r"]:
                best = (key, v)
        if best:
            fid, rr = best[0].split("|")
            a = best[1]["all"]
            approved[row["strategy"]] = {
                "fid": fid, "filters": filters.ids(fid), "rr": parse_rr(rr), "win_rate": a["win_rate"],
                "per_day": round(a["trades"] / trading_days, 2) if trading_days else None,
                "trades": a["trades"], "expectancy_r": a["expectancy_r"], "avg_hold_min": a["avg_hold_min"],
            }
    return approved


def compute_closest(rows, limit=6):
    found = []
    for row in rows:
        for key, v in (row.get("variants") or {}).items():
            a = v["all"]
            if a["trades"] >= MIN_TRADES and (a["expectancy_r"] or 0) > 0 and a["win_rate"] is not None:
                fid, rr = key.split("|")
                found.append({
                    "strategy": row["strategy"], "filters": filters.ids(fid), "rr": parse_rr(rr),
                    "win_rate": a["win_rate"], "trades": a["trades"],
                    "expectancy_r": a["expectancy_r"], "profit_factor": a["profit_factor"],
                    "avg_hold_min": a["avg_hold_min"],
                })
    found.sort(key=lambda x: (x["win_rate"], x["expectancy_r"]), reverse=True)
    return found[:limit]


def build_result(trades, base, htf, days, tf):
    cfg = TF_CONFIG[tf]
    mid = (base[0]["time"] + base[-1]["time"]) // 2
    grouped = {}
    for t in trades:
        grouped.setdefault((t["strategy"], t["filters"], t["variant"]), []).append(t)
    rows = []
    for name in STRATEGY_ORDER:
        variants = {}
        for fid in filters.COMBOS:
            for rr in (*RR_LIST, ADAPT):
                sub = grouped.get((name, fid, rr), [])
                variants[f"{fid}|{rr}"] = {
                    "all": metrics(sub),
                    "first": metrics([t for t in sub if t["opened_at"] < mid]),
                    "second": metrics([t for t in sub if t["opened_at"] >= mid]),
                }
        rows.append({"strategy": name, "variants": variants})

    trading_days = max(1, len({c["time"] // 86400 for c in base if (c["time"] // 86400 + 3) % 7 < 5}))
    approved = compute_approved(rows, trading_days)

    wanted = [(name, rule["fid"], rule["rr"]) for name, rule in approved.items()]
    ranked = sorted(
        (
            (v["all"]["expectancy_r"], row["strategy"], vkey)
            for row in rows for vkey, v in row["variants"].items()
            if v["all"]["trades"] >= 20 and v["all"]["expectancy_r"] is not None
        ),
        reverse=True,
    )
    for _, name, vkey in ranked[:10]:
        fid, rr = vkey.split("|")
        wanted.append((name, fid, parse_rr(rr)))
    trade_sets, set_meta, seen = {}, [], set()
    for name, fid, rr in wanted:
        key = f"{name}|{fid}|{rr}"
        if key in seen or len(seen) >= 30:
            continue
        sub = sorted(grouped.get((name, fid, rr), []), key=lambda t: t["opened_at"])[-80:]
        if not sub:
            continue
        seen.add(key)
        trade_sets[key] = [
            [t["opened_at"], t["closed_at"], 1 if t["direction"] == "BUY" else -1,
             round(t["entry"], 2), round(t["stop_loss"], 2), round(t["take_profit"], 2),
             t["outcome"][0], round(t["exit"], 2)]
            for t in sub
        ]
        stats = next(r["variants"][f"{fid}|{rr}"]["all"] for r in rows if r["strategy"] == name)
        set_meta.append({
            "key": key, "strategy": name, "rr": rr, "filters": fid,
            "label": f"{name} · {rr_label(rr)} · {filters.LABELS[fid]} · জয় {stats['win_rate']}% · {stats['trades']} ট্রেড"
                     + (" ✓" if name in approved and approved[name]["fid"] == fid and approved[name]["rr"] == rr else ""),
        })

    return {
        "generated_at": int(time.time()), "days": days,
        "timeframe": tf, "tf_label": cfg["label"], "max_hold_hours": MAX_HOLD_HOURS,
        "h1_bars": len(base), "h4_bars": len(htf),
        "from": base[0]["time"], "to": base[-1]["time"],
        "spread": SPREAD, "min_win": MIN_WIN,
        "rr_list": [str(rr) for rr in RR_LIST] + [ADAPT], "trading_days": trading_days,
        "filters": [{"id": fid, "label": filters.LABELS[fid]} for fid in filters.COMBOS],
        "strategies": rows,
        "approved": approved, "closest": compute_closest(rows),
        "trade_sets": trade_sets, "set_meta": set_meta,
        "assumptions": [
            f"{cfg['label']} ক্যান্ডেলে সিমুলেশন, স্প্রেড ${SPREAD:.2f} ধরা হয়েছে",
            f"প্রতিটা ট্রেড সর্বোচ্চ {MAX_HOLD_HOURS:g} ঘণ্টা ধরে রাখা হয়; তার মধ্যে SL/TP না লাগলে বর্তমান দামে বন্ধ (টাইমআউট)",
            "টার্গেট ১:২, ১:৩ বা 'AI অ্যাডাপ্টিভ'; একই ক্যান্ডেলে SL ও TP দুটোই ছুঁলে লস ধরা হয়",
            "AI অ্যাডাপ্টিভ: SL প্যাটার্ন/জোনের প্রান্তে, TP সবচেয়ে কাছের বাস্তবসম্মত লেভেলে (সুইং, আগের দিনের হাই/লো, পিভট, রাউন্ড নম্বর) যা ভোলাটিলিটি ও সেশন দেখে ৩ ঘণ্টায় পৌঁছানো সম্ভব; জায়গা না থাকলে ট্রেড নেওয়া হয় না",
            "অনুমোদনের শর্ত: ৩০+ ট্রেড, জয় ≥ লক্ষ্য, গড় R > 0.1, PF ≥ 1.3, টাইমআউট ≤ ৪০%, এবং সময়ের প্রথম ও দ্বিতীয় অর্ধেক দুটোতেই লাভজনক",
            "প্রতি (স্ট্র্যাটেজি, ফিল্টার, টার্গেট) সংযোগে একসাথে একটাই ট্রেড; Gemini ব্যাকটেস্ট করা যায় না",
            "অনেক সংযোগ পরীক্ষা করলে কিছু ভাগ্যে ভালো দেখায়; পেপার ট্রেডিংই চূড়ান্ত পরীক্ষা",
        ],
    }


async def run_job(days, tf):
    STATE.update(status="running", progress=0, error=None, started_at=int(time.time()))
    try:
        from app import main as m, signals as sg, smc, classic, classic2, patterns, levels

        cfg = TF_CONFIG[tf]
        days = min(days, cfg["max_days"])
        symbol = m.GOLD_SYMBOL
        base = await load_history(m, symbol, tf, days)
        STATE["progress"] = 5
        try:
            htf = await load_history(m, symbol, cfg["htf"], days + 15)
        except Exception:
            htf = []
        if len(base) < 200:
            raise ValueError(f"পর্যাপ্ত ক্যান্ডেল পাওয়া যায়নি ({len(base)}টি)")

        def progress(fraction):
            STATE["progress"] = 5 + round(fraction * 95)

        trades = await asyncio.to_thread(
            simulate, m, sg, smc, classic, classic2, patterns, levels, base, htf, SPREAD, tf, progress
        )
        result = build_result(trades, base, htf, days, tf)
        STATE.update(status="done", progress=100, result=result)
        try:
            await asyncio.to_thread(sg.db_exec, DDL)
            await asyncio.to_thread(
                sg.db_exec,
                "INSERT INTO ai_backtest (created_at, result) VALUES (:created_at, :result)",
                {"created_at": result["generated_at"], "result": json.dumps(result)},
            )
        except Exception:
            logger.exception("Could not persist backtest result")
    except Exception as error:
        logger.exception("Backtest failed")
        STATE.update(status="error", error=str(getattr(error, "detail", error))[:200])


async def current_result():
    if STATE["result"] is None and STATE["status"] != "running":
        try:
            from app import signals as sg

            await asyncio.to_thread(sg.db_exec, DDL)
            rows = await asyncio.to_thread(sg.db_rows, "SELECT result FROM ai_backtest ORDER BY id DESC LIMIT 1")
            if rows:
                STATE["result"] = json.loads(rows[0]["result"])
        except Exception:
            logger.exception("Could not load saved backtest")
    return STATE["result"]


AUTO = os.getenv("BACKTEST_AUTO", "1") == "1"
AUTO_HOURS = max(1.0, float(os.getenv("BACKTEST_AUTO_HOURS", "6")))
AUTO_DAYS = {"5": 30, "15": 90, "60": 180}


async def latest_age(tf):
    from app import signals as sg

    await asyncio.to_thread(sg.db_exec, DDL)
    rows = await asyncio.to_thread(
        sg.db_rows,
        "SELECT created_at FROM ai_backtest WHERE result LIKE :pattern ORDER BY id DESC LIMIT 1",
        {"pattern": '%"timeframe": "' + tf + '"%'},
    )
    return int(time.time()) - int(rows[0]["created_at"]) if rows else None


async def auto_loop(tfs):
    """Re-run the backtest by itself (no button needed) whenever the last result is older than AUTO_HOURS."""
    await asyncio.sleep(90)
    while True:
        for tf in tfs:
            try:
                while STATE["status"] == "running":
                    await asyncio.sleep(30)
                age = await latest_age(tf)
                if age is None or age >= AUTO_HOURS * 3600:
                    logger.info("Auto backtest started (tf=%s)", tf)
                    await run_job(AUTO_DAYS.get(tf, 90), tf)
                    if STATE["status"] == "error":
                        await asyncio.sleep(1800)
            except Exception:
                logger.exception("Auto backtest loop failed")
        await asyncio.sleep(600)


@router.post("/api/ai/backtest/run")
async def backtest_run(days: int = Query(90, ge=10, le=180), tf: str = Query("15")):
    if STATE["status"] == "running":
        return {"status": "running"}
    if tf not in TF_CONFIG:
        tf = "15"
    asyncio.create_task(run_job(days, tf))
    return {"status": "started", "days": min(days, TF_CONFIG[tf]["max_days"]), "tf": tf}


@router.get("/api/ai/backtest")
async def backtest_get():
    result = await current_result()
    view = None
    if result:
        view = {k: v for k, v in result.items() if k != "trade_sets"}
        if view.get("strategies") and "variants" in view["strategies"][0]:
            view["approved"] = compute_approved(view["strategies"], view.get("trading_days"))
            view["closest"] = compute_closest(view["strategies"])
            view["min_win"] = MIN_WIN
    return {
        "status": STATE["status"], "progress": STATE["progress"],
        "error": STATE["error"], "result": view,
    }


@router.get("/api/ai/backtest/sets")
async def backtest_sets():
    result = await current_result() or {}
    return {"sets": result.get("set_meta", []), "timeframe": result.get("timeframe")}


@router.get("/api/ai/backtest/trades")
async def backtest_trades(key: str = Query("")):
    result = await current_result() or {}
    return {"key": key, "trades": (result.get("trade_sets") or {}).get(key, [])}
