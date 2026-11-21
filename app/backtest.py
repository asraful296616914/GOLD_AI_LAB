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
MAX_HOLD_BARS = 24
WINDOW = 160
RR_LIST = (2.0, 3.0)
MIN_TRADES = 30
MIN_WIN = float(os.getenv("MIN_WIN_RATE", "50"))
STRATEGY_ORDER = [
    "RULES_V1", "EMA_CROSS", "MACD_CROSS", "DONCHIAN", "SESSION_BREAKOUT", "RSI_REVERSAL",
    "BB_BOUNCE", "SR_CANDLE", "TREND_PULLBACK",
    "SUPERTREND", "STOCH_CROSS", "ADX_TREND", "ICHIMOKU_TK", "FIB_PULLBACK", "INSIDE_BAR",
    "PREV_DAY_BREAK", "PIVOT_BOUNCE", "NY_OPEN_BREAK", "KELTNER_BREAK", "HA_TREND", "ROUND_LEVEL",
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


def simulate(m, sg, smc, classic, classic2, h1, h4, spread, progress=None):
    h4_times = [c["time"] for c in h4]
    combo_ids = {fid: filters.ids(fid) for fid in filters.COMBOS}
    trades = []
    open_trades = {}
    n = len(h1)

    for i in range(50, n):
        bar = h1[i]

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
            if hit_sl:
                outcome, gross = "LOSS", -1.0
            elif hit_tp:
                outcome, gross = "WIN", t["rr"]
            elif i - t["opened_i"] >= MAX_HOLD_BARS:
                outcome, gross = "EXPIRED", (bar["close"] - t["entry"]) * direction / t["risk"]
            if outcome:
                trades.append({
                    "strategy": t["strategy"], "filters": t["fid"], "rr": t["rr"],
                    "outcome": outcome, "r": round(gross - spread / t["risk"], 3),
                    "opened_at": t["opened_at"], "closed_at": bar["time"],
                })
                del open_trades[key]

        window = h1[max(0, i - WINDOW + 1): i + 1]
        atr = m.calculate_atr(window)
        if atr:
            h4_end = bisect.bisect_right(h4_times, bar["time"] - 10800)
            h4_window = h4[max(0, h4_end - WINDOW): h4_end] if h4 else None
            try:
                flag_set = filters.flags(m, window, h4_window, atr, bar["time"])
            except Exception:
                flag_set = None
            candidates = []
            for source in (
                lambda: smc.analyze(window, atr, h4_window)["signals"],
                lambda: classic.analyze(m, window, atr),
                lambda: classic2.analyze(m, window, atr),
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
                    for fid, ids in combo_ids.items():
                        if not filters.passes(ids, flag_set, item["direction"]):
                            continue
                        for rr in RR_LIST:
                            key = (item["strategy"], fid, rr)
                            if key in open_trades:
                                continue
                            open_trades[key] = {
                                "strategy": item["strategy"], "fid": fid, "rr": rr,
                                "direction": item["direction"], "entry": item["entry"],
                                "stop_loss": item["stop_loss"],
                                "take_profit": item["entry"] + direction * rr * risk,
                                "risk": risk, "opened_i": i, "opened_at": bar["time"],
                            }

        if progress and i % 25 == 0:
            progress(i / n)

    return trades


def metrics(trades):
    count = len(trades)
    if not count:
        return {"trades": 0, "wins": 0, "losses": 0, "expired": 0, "win_rate": None,
                "expectancy_r": None, "profit_factor": None, "net_r": 0, "max_dd_r": 0}
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
    }


def compute_approved(rows):
    approved = {}
    for row in rows:
        best = None
        for key, v in (row.get("variants") or {}).items():
            a, first, second = v["all"], v["first"], v["second"]
            if a["trades"] < MIN_TRADES or a["win_rate"] is None or a["win_rate"] < MIN_WIN:
                continue
            if (a["expectancy_r"] or 0) <= 0.1 or (a["profit_factor"] or 0) < 1.3:
                continue
            if any(h["trades"] < 10 or (h["expectancy_r"] or 0) <= 0 for h in (first, second)):
                continue
            if best is None or a["expectancy_r"] > best[1]["all"]["expectancy_r"]:
                best = (key, v)
        if best:
            fid, rr = best[0].split("|")
            a = best[1]["all"]
            approved[row["strategy"]] = {
                "filters": filters.ids(fid), "rr": float(rr), "win_rate": a["win_rate"],
                "trades": a["trades"], "expectancy_r": a["expectancy_r"],
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
                    "strategy": row["strategy"], "filters": filters.ids(fid), "rr": float(rr),
                    "win_rate": a["win_rate"], "trades": a["trades"],
                    "expectancy_r": a["expectancy_r"], "profit_factor": a["profit_factor"],
                })
    found.sort(key=lambda x: (x["win_rate"], x["expectancy_r"]), reverse=True)
    return found[:limit]


def build_result(trades, h1, h4, days):
    mid = (h1[0]["time"] + h1[-1]["time"]) // 2
    grouped = {}
    for t in trades:
        grouped.setdefault((t["strategy"], t["filters"], t["rr"]), []).append(t)
    rows = []
    for name in STRATEGY_ORDER:
        variants = {}
        for fid in filters.COMBOS:
            for rr in RR_LIST:
                sub = grouped.get((name, fid, rr), [])
                variants[f"{fid}|{rr}"] = {
                    "all": metrics(sub),
                    "first": metrics([t for t in sub if t["opened_at"] < mid]),
                    "second": metrics([t for t in sub if t["opened_at"] >= mid]),
                }
        rows.append({"strategy": name, "variants": variants})
    return {
        "generated_at": int(time.time()), "days": days,
        "h1_bars": len(h1), "h4_bars": len(h4),
        "from": h1[0]["time"], "to": h1[-1]["time"],
        "spread": SPREAD, "min_win": MIN_WIN,
        "rr_list": [str(rr) for rr in RR_LIST],
        "filters": [{"id": fid, "label": filters.LABELS[fid]} for fid in filters.COMBOS],
        "strategies": rows,
        "approved": compute_approved(rows), "closest": compute_closest(rows),
        "assumptions": [
            f"H1 ক্যান্ডেলে সিমুলেশন, স্প্রেড ${SPREAD:.2f} ধরা হয়েছে",
            "টার্গেট ১:২ বা ১:৩; একই ক্যান্ডেলে SL ও TP দুটোই ছুঁলে লস ধরা হয়",
            f"{MAX_HOLD_BARS} ঘণ্টায় SL/TP না লাগলে বর্তমান দামে বন্ধ (লাইভ সিস্টেমের মতোই)",
            "অনুমোদনের শর্ত: ৩০+ ট্রেড, জয় ≥ লক্ষ্য, গড় R > 0.1, PF ≥ 1.3, এবং সময়ের প্রথম ও দ্বিতীয় অর্ধেক দুটোতেই লাভজনক",
            "প্রতি (স্ট্র্যাটেজি, ফিল্টার, টার্গেট) সংযোগে একসাথে একটাই ট্রেড; Gemini ব্যাকটেস্ট করা যায় না",
            "অনেক সংযোগ পরীক্ষা করলে কিছু ভাগ্যে ভালো দেখায়; পেপার ট্রেডিংই চূড়ান্ত পরীক্ষা",
        ],
    }


async def run_job(days):
    STATE.update(status="running", progress=0, error=None, started_at=int(time.time()))
    try:
        from app import main as m, signals as sg, smc, classic, classic2

        symbol = m.GOLD_SYMBOL
        h1 = await load_history(m, symbol, "60", days)
        STATE["progress"] = 5
        try:
            h4 = await load_history(m, symbol, "240", days + 30)
        except Exception:
            h4 = []
        if len(h1) < 120:
            raise ValueError(f"পর্যাপ্ত ক্যান্ডেল পাওয়া যায়নি ({len(h1)}টি)")

        def progress(fraction):
            STATE["progress"] = 5 + round(fraction * 95)

        trades = await asyncio.to_thread(simulate, m, sg, smc, classic, classic2, h1, h4, SPREAD, progress)
        result = build_result(trades, h1, h4, days)
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


@router.post("/api/ai/backtest/run")
async def backtest_run(days: int = Query(180, ge=30, le=180)):
    if STATE["status"] == "running":
        return {"status": "running"}
    asyncio.create_task(run_job(days))
    return {"status": "started", "days": days}


@router.get("/api/ai/backtest")
async def backtest_get():
    if STATE["result"] is None and STATE["status"] != "running":
        try:
            from app import signals as sg

            await asyncio.to_thread(sg.db_exec, DDL)
            rows = await asyncio.to_thread(sg.db_rows, "SELECT result FROM ai_backtest ORDER BY id DESC LIMIT 1")
            if rows:
                STATE["result"] = json.loads(rows[0]["result"])
        except Exception:
            logger.exception("Could not load saved backtest")
    result = STATE["result"]
    if result and result.get("strategies") and "variants" in result["strategies"][0]:
        result["approved"] = compute_approved(result["strategies"])
        result["closest"] = compute_closest(result["strategies"])
        result["min_win"] = MIN_WIN
    return {
        "status": STATE["status"], "progress": STATE["progress"],
        "error": STATE["error"], "result": result,
    }
