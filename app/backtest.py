import os
import time
import json
import bisect
import asyncio
import logging

from fastapi import APIRouter, Query

logger = logging.getLogger("gold-ai-lab.backtest")
router = APIRouter()

SPREAD = float(os.getenv("BACKTEST_SPREAD", "0.30"))
MAX_HOLD_BARS = 48
RR_LIST = (1.0, 1.5, 2.0)
MIN_TRADES = 30
MIN_WIN = float(os.getenv("MIN_WIN_RATE", "50"))
WINDOW = 160
STRATEGY_ORDER = [
    "RULES_V1", "EMA_CROSS", "MACD_CROSS", "DONCHIAN", "SESSION_BREAKOUT", "RSI_REVERSAL",
    "BB_BOUNCE", "SR_CANDLE", "TREND_PULLBACK",
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


def simulate(m, sg, smc, h1, h4, spread, progress=None):
    h4_times = [c["time"] for c in h4]
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
                    "strategy": t["strategy"], "rr": t["rr"], "direction": t["direction"], "outcome": outcome,
                    "r": round(gross - spread / t["risk"], 3),
                    "opened_at": t["opened_at"], "closed_at": bar["time"],
                })
                del open_trades[key]

        window = h1[max(0, i - WINDOW + 1): i + 1]
        atr = m.calculate_atr(window)
        if atr:
            h4_end = bisect.bisect_right(h4_times, bar["time"] - 10800)
            h4_window = h4[max(0, h4_end - WINDOW): h4_end] if h4 else None
            candidates = []
            try:
                candidates.extend(smc.analyze(window, atr, h4_window)["signals"])
            except Exception:
                pass
            try:
                from app import classic
                candidates.extend(classic.analyze(m, window, atr))
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
            for item in candidates:
                risk = abs(item["entry"] - item["stop_loss"])
                if risk <= 0:
                    continue
                direction = 1 if item["direction"] == "BUY" else -1
                for rr in RR_LIST:
                    key = (item["strategy"], rr)
                    if key in open_trades:
                        continue
                    open_trades[key] = {
                        "strategy": item["strategy"], "direction": item["direction"],
                        "entry": item["entry"], "stop_loss": item["stop_loss"],
                        "take_profit": item["entry"] + direction * rr * risk,
                        "rr": rr, "opened_i": i, "opened_at": bar["time"], "risk": risk,
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


def in_session(timestamp):
    hour = (timestamp // 3600) % 24
    weekday = (timestamp // 86400 + 3) % 7
    return weekday < 5 and 7 <= hour < 20


def verdict(all_metrics):
    if all_metrics["trades"] < 30:
        return "FEW_TRADES"
    pf = all_metrics["profit_factor"]
    if all_metrics["expectancy_r"] > 0.1 and pf is not None and pf >= 1.2:
        return "PASS"
    return "WEAK"


def compute_approved(rows):
    approved = {}
    for row in rows:
        variants = row.get("by_rr") or {}
        positive = sum(
            1 for v in variants.values()
            if v["all"]["trades"] >= MIN_TRADES and (v["all"]["expectancy_r"] or 0) > 0
        )
        if positive < 2:
            continue
        best = None
        for rr_key, variant in variants.items():
            a = variant["all"]
            if (
                a["trades"] >= MIN_TRADES
                and a["win_rate"] is not None and a["win_rate"] >= MIN_WIN
                and a["expectancy_r"] is not None and a["expectancy_r"] > 0.05
                and (a["profit_factor"] or 0) >= 1.1
            ):
                if best is None or a["expectancy_r"] > best[1]:
                    best = (float(rr_key), a["expectancy_r"])
        if best:
            approved[row["strategy"]] = best[0]
    return approved


def build_result(trades, h1, h4, days):
    rows = []
    for name in STRATEGY_ORDER:
        own = [t for t in trades if t["strategy"] == name]
        by_rr = {}
        for rr in RR_LIST:
            sub = [t for t in own if t["rr"] == rr]
            all_metrics = metrics(sub)
            by_rr[str(rr)] = {
                "all": all_metrics,
                "session": metrics([t for t in sub if in_session(t["opened_at"])]),
                "verdict": verdict(all_metrics),
            }
        base = by_rr["2.0"]
        rows.append({
            "strategy": name, "all": base["all"], "session": base["session"],
            "verdict": base["verdict"], "by_rr": by_rr,
        })
    approved = compute_approved(rows)
    return {
        "generated_at": int(time.time()), "days": days,
        "h1_bars": len(h1), "h4_bars": len(h4),
        "from": h1[0]["time"], "to": h1[-1]["time"],
        "spread": SPREAD, "rr_list": [str(rr) for rr in RR_LIST], "strategies": rows,
        "approved": approved, "min_win": MIN_WIN,
        "assumptions": [
            f"H1 ক্যান্ডেলে সিমুলেশন, স্প্রেড ${SPREAD:.2f} ধরা হয়েছে",
            "টার্গেট = ঝুঁকির 1, 1.5 বা 2 গুণ (উপরের বাছাই অনুযায়ী); কম টার্গেটে জয়ের হার বাড়ে, কিন্তু প্রতি জয়ে লাভ কমে",
            "একই ক্যান্ডেলে SL ও TP দুটোই ছুঁলে লস ধরা হয়",
            f"{MAX_HOLD_BARS} ঘণ্টায় SL/TP না লাগলে বর্তমান দামে বন্ধ (মেয়াদোত্তীর্ণ)",
            "প্রতি স্ট্র্যাটেজিতে একসাথে একটাই ট্রেড; Gemini ব্যাকটেস্ট করা যায় না",
            "R = ঝুঁকির একক (১R = SL পর্যন্ত দূরত্ব); পুরনো ফল ভবিষ্যতের নিশ্চয়তা নয়",
        ],
    }


async def run_job(days):
    STATE.update(status="running", progress=0, error=None, started_at=int(time.time()))
    try:
        from app import main as m, signals as sg, smc

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

        trades = await asyncio.to_thread(simulate, m, sg, smc, h1, h4, SPREAD, progress)
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
async def backtest_run(days: int = Query(90, ge=30, le=180)):
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
    if result and result.get("strategies") and "by_rr" in result["strategies"][0]:
        result["approved"] = compute_approved(result["strategies"])
        result["min_win"] = MIN_WIN
    return {
        "status": STATE["status"], "progress": STATE["progress"],
        "error": STATE["error"], "result": STATE["result"],
    }
