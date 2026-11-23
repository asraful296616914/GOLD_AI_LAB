import os
import re
import time
import json
import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import text

from app.database import engine
from app import smc
from app import classic
from app import classic2
from app import patterns
from app import levels
from app import notify
from app import filters
from app import analyst
from app import backtest

logger = logging.getLogger("gold-ai-lab.signals")
router = APIRouter()
router.include_router(backtest.router)
router.include_router(notify.router)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
GEMINI_FALLBACKS = [
    "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3.6-flash",
    "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite",
]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
AI_AUTO = os.getenv("AI_AUTO", "1") == "1"
AI_INTERVAL_MIN = max(15, int(os.getenv("AI_INTERVAL_MINUTES", "15")))
MANUAL_GAP_SECONDS = 120
EXPIRE_SECONDS = 3 * 3600
MAX_DAILY_SIGNALS = int(os.getenv("MAX_DAILY_SIGNALS", "0"))
MIN_GAP_SECONDS = int(os.getenv("MIN_SIGNAL_GAP_MINUTES", "0")) * 60
MARKET_STALE_SECONDS = 4 * 3600
SL_ATR = 1.5
QUALITY_GATE = os.getenv("QUALITY_GATE", "1") == "1"
TP_ATR = 3.0
TIMEFRAMES = ("15", "60", "240")
TF_LABEL = {"5": "M5", "15": "M15", "60": "H1", "240": "H4"}
LIVE_TFS = [
    tf for tf in os.getenv("STRAT_TFS", os.getenv("STRAT_TF", "15,5")).replace(" ", "").split(",")
    if tf in ("5", "15", "60")
] or ["15"]
STRAT_TF = LIVE_TFS[0]
STRAT_WINDOW = {"5": 400, "15": 200, "60": 160}
SCAN_SECONDS = max(60, int(os.getenv("SCAN_MINUTES", "5")) * 60)
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "10000"))
PAPER_RISK_PCT = float(os.getenv("PAPER_RISK_PCT", "1"))

STATE = {"last_run": None, "last_error": None, "market_closed": False}
RUN_LOCK = asyncio.Lock()

DDL = [
    """CREATE TABLE IF NOT EXISTS ai_signals (
        id BIGSERIAL PRIMARY KEY,
        created_at BIGINT NOT NULL,
        strategy TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        direction TEXT NOT NULL,
        entry DOUBLE PRECISION NOT NULL,
        stop_loss DOUBLE PRECISION NOT NULL,
        take_profit DOUBLE PRECISION NOT NULL,
        confidence INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'OPEN',
        closed_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS ai_research (
        id BIGSERIAL PRIMARY KEY,
        created_at BIGINT NOT NULL,
        symbol TEXT NOT NULL,
        model TEXT NOT NULL,
        bias TEXT,
        confidence INTEGER,
        summary TEXT,
        risk_note TEXT
    )""",
]


def db_rows(sql, params=None):
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.execute(text(sql), params or {})]


def db_exec(sql, params=None):
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})


def ensure_tables():
    with engine.begin() as connection:
        for statement in DDL:
            connection.execute(text(statement))


def insert_signal(row):
    with engine.begin() as connection:
        duplicate = connection.execute(
            text(
                "SELECT 1 FROM ai_signals WHERE strategy=:strategy AND direction=:direction AND timeframe=:timeframe "
                "AND status='OPEN' AND created_at > :since LIMIT 1"
            ),
            {"strategy": row["strategy"], "direction": row["direction"], "timeframe": row.get("timeframe", ""),
             "since": row["created_at"] - EXPIRE_SECONDS},
        ).first()
        if duplicate:
            return False
        connection.execute(
            text(
                "INSERT INTO ai_signals (created_at, strategy, symbol, timeframe, direction, entry, "
                "stop_loss, take_profit, confidence, reason) VALUES (:created_at, :strategy, :symbol, "
                ":timeframe, :direction, :entry, :stop_loss, :take_profit, :confidence, :reason)"
            ),
            row,
        )
        return True


def build_levels(direction, price, atr):
    if direction == "BUY":
        return price - SL_ATR * atr, price + TP_ATR * atr
    return price + SL_ATR * atr, price - TP_ATR * atr


def rules_signal(m, candles):
    closes = [c["close"] for c in candles]
    ema20 = m.calculate_ema(closes, 20)
    ema50 = m.calculate_ema(closes, 50)
    rsi = m.calculate_rsi(closes)
    macd = m.calculate_macd(closes)
    atr = m.calculate_atr(candles)
    if None in (ema20, ema50, rsi, macd, atr):
        return None
    price = closes[-1]
    score = 0
    reasons = []

    if ema20 > ema50:
        score += 1
        reasons.append("EMA20 > EMA50 (আপট্রেন্ড)")
    else:
        score -= 1
        reasons.append("EMA20 < EMA50 (ডাউনট্রেন্ড)")

    if price > ema20:
        score += 1
        reasons.append("প্রাইস EMA20-এর ওপরে")
    else:
        score -= 1
        reasons.append("প্রাইস EMA20-এর নিচে")

    if macd["histogram"] > 0:
        score += 1
        reasons.append("MACD হিস্টোগ্রাম পজিটিভ")
    else:
        score -= 1
        reasons.append("MACD হিস্টোগ্রাম নেগেটিভ")

    if rsi >= 70:
        score -= 1
        reasons.append(f"RSI {rsi:.0f} (ওভারবট)")
    elif rsi <= 30:
        score += 1
        reasons.append(f"RSI {rsi:.0f} (ওভারসোল্ড)")
    elif rsi > 50:
        score += 1
        reasons.append(f"RSI {rsi:.0f} (বুলিশ জোন)")
    else:
        score -= 1
        reasons.append(f"RSI {rsi:.0f} (বেয়ারিশ জোন)")

    direction = "BUY" if score >= 3 else "SELL" if score <= -3 else "NEUTRAL"
    return {
        "direction": direction,
        "confidence": round(abs(score) / 4 * 100),
        "reasons": reasons,
        "price": price,
        "atr": atr,
    }


def gemini_candidates():
    names = []
    for name in (STATE.get("gemini_model"), GEMINI_MODEL, *GEMINI_FALLBACKS):
        if name and name not in names:
            names.append(name)
    return names


async def gemini_analyze(summary):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None, "GEMINI_API_KEY সেট করা নেই"

    prompt = (
        "You are a cautious market analyst for XAU/USD (gold). Use ONLY the indicator data below. "
        "Do not invent prices, news or numbers. Return ONLY a JSON object with keys: "
        "direction (BUY, SELL or NEUTRAL), confidence (integer 0-100), "
        "summary_bn (3-4 short sentences in Bengali explaining the multi-timeframe picture), "
        "risk_note_bn (1-2 sentences in Bengali about risks and uncertainty). "
        "Prefer NEUTRAL when timeframes disagree. This is educational analysis, not financial advice.\n\n"
        "DATA:\n" + json.dumps(summary, ensure_ascii=False)
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": key.strip(), "Content-Type": "application/json"}
    last_error = "কোনো Gemini মডেল পাওয়া যায়নি"

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            for model in gemini_candidates():
                response = await client.post(GEMINI_URL.format(model=model), headers=headers, json=body)
                if response.status_code == 404:
                    last_error = f"{model}: মডেল পাওয়া যায়নি (404)"
                    continue
                if response.status_code == 429:
                    last_error = f"{model}: ফ্রি লিমিট শেষ (429)"
                    continue
                if response.status_code != 200:
                    return None, f"Gemini ত্রুটি {response.status_code} ({model})"

                parts = response.json()["candidates"][0]["content"]["parts"]
                raw = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
                parsed = json.loads(raw)
                direction = str(parsed.get("direction", "NEUTRAL")).upper()
                if direction not in ("BUY", "SELL", "NEUTRAL"):
                    direction = "NEUTRAL"
                try:
                    confidence = max(0, min(100, int(parsed.get("confidence", 0))))
                except (TypeError, ValueError):
                    confidence = 0
                STATE["gemini_model"] = model
                return {
                    "direction": direction,
                    "confidence": confidence,
                    "summary": str(parsed.get("summary_bn", ""))[:1200],
                    "risk_note": str(parsed.get("risk_note_bn", ""))[:600],
                    "model": model,
                }, None
        return None, last_error
    except Exception as error:
        logger.exception("Gemini call failed")
        return None, f"Gemini কল ব্যর্থ: {type(error).__name__}"


async def resolve_open(m, symbol):
    open_rows = await asyncio.to_thread(db_rows, "SELECT * FROM ai_signals WHERE status='OPEN'")
    if not open_rows:
        return
    try:
        candles = await m.fetch_analysis_history(symbol, "5", bars=288)
    except Exception:
        logger.exception("Could not load M5 candles to resolve signals")
        candles = []

    now = int(time.time())
    for signal in open_rows:
        outcome = None
        for candle in candles:
            if candle["time"] <= signal["created_at"]:
                continue
            if signal["direction"] == "BUY":
                hit_sl = candle["low"] <= signal["stop_loss"]
                hit_tp = candle["high"] >= signal["take_profit"]
            else:
                hit_sl = candle["high"] >= signal["stop_loss"]
                hit_tp = candle["low"] <= signal["take_profit"]
            if hit_sl:
                outcome = "LOSS"
                break
            if hit_tp:
                outcome = "WIN"
                break
        if outcome is None and now - signal["created_at"] > EXPIRE_SECONDS:
            outcome = "EXPIRED"
        if outcome:
            await asyncio.to_thread(
                db_exec,
                "UPDATE ai_signals SET status=:status, closed_at=:closed WHERE id=:id",
                {"status": outcome, "closed": now, "id": signal["id"]},
            )
            asyncio.create_task(notify.notify_result(signal, outcome))


def signal_exists(row):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT 1 FROM ai_signals WHERE strategy=:strategy AND direction=:direction AND timeframe=:timeframe "
                "AND status='OPEN' AND created_at > :since LIMIT 1"
            ),
            {"strategy": row["strategy"], "direction": row["direction"], "timeframe": row.get("timeframe", ""),
             "since": row["created_at"] - EXPIRE_SECONDS},
        ).first() is not None


def signals_today(now):
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), 0) AS last FROM ai_signals "
                "WHERE strategy <> 'GEMINI_V1' AND created_at >= :day"
            ),
            {"day": now - now % 86400},
        ).first()
        return int(row[0]), int(row[1])


async def publish(row, context):
    if await asyncio.to_thread(signal_exists, row):
        return False
    if MAX_DAILY_SIGNALS or MIN_GAP_SECONDS:
        today_count, last_at = await asyncio.to_thread(signals_today, int(time.time()))
        if (MAX_DAILY_SIGNALS and today_count >= MAX_DAILY_SIGNALS) or (
            MIN_GAP_SECONDS and last_at and time.time() - last_at < MIN_GAP_SECONDS
        ):
            return False
    if context and context.get("blackout"):
        event = context["blackout"]
        await analyst.log_review(row, "BLOCKED", 0, "বড় ইভেন্টের কাছাকাছি সময়: " + event["name"])
        return False
    verdict = await analyst.review(row, context) if context else None
    if verdict:
        if verdict["verdict"] != "APPROVE":
            return False
        row = {
            **row, "confidence": verdict["confidence"],
            "reason": (row.get("reason", "") + " | AI যাচাই: " + verdict["reason"])[:400],
        }
    else:
        row = {**row, "reason": (row.get("reason", "") + " | (AI যাচাই হয়নি)")[:400]}
    added = await asyncio.to_thread(insert_signal, row)
    if added:
        asyncio.create_task(notify.notify_new(row))
    return added


async def load_approved(tf=None):
    tf = tf or STRAT_TF
    if not QUALITY_GATE:
        return None
    caches = STATE.setdefault("approved_cache", {})
    cached = caches.get(tf)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    value = {}
    try:
        await asyncio.to_thread(db_exec, backtest.DDL)
        rows = await asyncio.to_thread(
            db_rows,
            "SELECT result FROM ai_backtest WHERE result LIKE :pattern ORDER BY id DESC LIMIT 1",
            {"pattern": '%"timeframe": "' + tf + '"%'},
        )
        if rows:
            result = json.loads(rows[0]["result"])
            strategies = result.get("strategies") or []
            if strategies and "variants" in strategies[0]:
                value = backtest.compute_approved(strategies)
    except Exception:
        logger.exception("Could not load approved strategies")
    caches[tf] = (time.time(), value)
    return value


def gate_item(item, approved, flag_set, level_ctx=None):
    if approved is None:
        return item
    rule = approved.get(item["strategy"])
    if not rule or flag_set is None:
        return None
    if not filters.passes(rule["filters"], flag_set, item["direction"]):
        return None
    if rule["rr"] == "ADAPT":
        if not level_ctx:
            return None
        hold_bars = max(1, int(EXPIRE_SECONDS / 60 / int(level_ctx.get("tf", STRAT_TF))))
        lv = levels.adaptive(
            level_ctx["base"], item["direction"], item["entry"], item["stop_loss"],
            level_ctx["atr"], level_ctx["now"], hold_bars,
        )
        if not lv:
            return None
        note = f" | AI স্তর: ঝুঁকি-লাভ ১:{lv['rr']:.1f} (লক্ষ্য: {lv['target']})"
        return {**item, "stop_loss": lv["stop_loss"], "take_profit": lv["take_profit"],
                "reason": (item.get("reason", "") + note)[:400]}
    risk = abs(item["entry"] - item["stop_loss"])
    direction = 1 if item["direction"] == "BUY" else -1
    return {**item, "take_profit": item["entry"] + direction * rule["rr"] * risk}


async def run_cycle(auto=False, light=False):
    if RUN_LOCK.locked():
        return {"status": "busy"}
    async with RUN_LOCK:
        now = int(time.time())
        last = STATE["last_run"]
        if not auto and last and now - last < MANUAL_GAP_SECONDS:
            return {"status": "cooldown", "wait_seconds": MANUAL_GAP_SECONDS - (now - last)}

        from app import main as m

        symbol = m.GOLD_SYMBOL
        fetch_tfs = list(dict.fromkeys([*TIMEFRAMES, *LIVE_TFS]))
        results = await asyncio.gather(
            *(m.fetch_analysis_history(symbol, tf, bars=STRAT_WINDOW[tf] if tf in LIVE_TFS else 160) for tf in fetch_tfs),
            return_exceptions=True,
        )
        candles = {tf: r for tf, r in zip(fetch_tfs, results) if not isinstance(r, Exception)}
        if "60" not in candles:
            raise HTTPException(status_code=502, detail="H1 ক্যান্ডেল পাওয়া যায়নি")

        await resolve_open(m, symbol)

        h1 = candles["60"]
        if now - h1[-1]["time"] > MARKET_STALE_SECONDS:
            STATE["market_closed"] = True
            if auto:
                return {"status": "market_closed", "last_candle": h1[-1]["time"]}
            summary_data = {TF_LABEL[tf]: m.describe_candles(c) for tf, c in candles.items()}
            closed_context = await analyst.build_context(m, h1, m.calculate_atr(h1), now, force=True, technical=summary_data)
            summary_data["session"] = closed_context["session"]
            if closed_context["brief"]:
                summary_data["market_brief"] = analyst.brief_for_prompt(closed_context["brief"], now)
            gemini, gemini_error = await gemini_analyze(summary_data)
            if gemini:
                await asyncio.to_thread(
                    db_exec,
                    "INSERT INTO ai_research (created_at, symbol, model, bias, confidence, summary, risk_note) "
                    "VALUES (:created_at, :symbol, :model, :bias, :confidence, :summary, :risk_note)",
                    {"created_at": now, "symbol": symbol, "model": gemini["model"], "bias": gemini["direction"],
                     "confidence": gemini["confidence"], "summary": gemini["summary"], "risk_note": gemini["risk_note"]},
                )
            STATE["last_run"] = now
            STATE["last_error"] = gemini_error
            return {"status": "market_closed", "last_candle": h1[-1]["time"],
                    "gemini": bool(gemini), "gemini_error": gemini_error}
        STATE["market_closed"] = False
        atr = m.calculate_atr(h1)
        created = []

        primary = LIVE_TFS[0]
        primary_base = candles.get(primary) or h1
        base_atr = m.calculate_atr(primary_base)
        base_price = primary_base[-1]["close"]
        tf_name = TF_LABEL.get(primary, primary)

        rule = rules_signal(m, primary_base)
        STATE["reviews_used"] = 0
        gemini, gemini_error = None, None
        if light:
            context = await analyst.build_context(m, h1, atr, now, force=False, technical=None)
        else:
            summary_data = {TF_LABEL[tf]: m.describe_candles(c) for tf, c in candles.items()}
            context = await analyst.build_context(m, h1, atr, now, force=not auto, technical=summary_data)
            summary_data["session"] = context["session"]
            if context["brief"]:
                summary_data["market_brief"] = analyst.brief_for_prompt(context["brief"], now)
            gemini, gemini_error = await gemini_analyze(summary_data)

            if gemini:
                research = {
                    "model": gemini["model"],
                    "bias": gemini["direction"],
                    "confidence": gemini["confidence"],
                    "summary": gemini["summary"],
                    "risk_note": gemini["risk_note"],
                }
            else:
                research = {
                    "model": "rules-only",
                    "bias": rule["direction"] if rule else "NEUTRAL",
                    "confidence": rule["confidence"] if rule else 0,
                    "summary": "; ".join(rule["reasons"]) if rule else "পর্যাপ্ত ডেটা নেই",
                    "risk_note": gemini_error or "",
                }
            await asyncio.to_thread(
                db_exec,
                "INSERT INTO ai_research (created_at, symbol, model, bias, confidence, summary, risk_note) "
                "VALUES (:created_at, :symbol, :model, :bias, :confidence, :summary, :risk_note)",
                {"created_at": now, "symbol": symbol, **research},
            )

        pool = []

        def collect(raw_item, approved, flag_set, level_ctx, label):
            item = gate_item(raw_item, approved, flag_set, level_ctx)
            if item:
                score = ((approved or {}).get(item["strategy"]) or {}).get("expectancy_r") or 0
                pool.append((score, {
                    "created_at": now, "strategy": item["strategy"], "symbol": symbol, "timeframe": label,
                    "direction": item["direction"], "entry": item["entry"], "stop_loss": item["stop_loss"],
                    "take_profit": item["take_profit"], "confidence": item["confidence"], "reason": item["reason"],
                }))

        if gemini and gemini["direction"] in ("BUY", "SELL") and gemini["confidence"] >= 60 and base_atr:
            sl, tp = build_levels(gemini["direction"], base_price, base_atr)
            gemini_row = {
                "created_at": now, "strategy": "GEMINI_V1", "symbol": symbol, "timeframe": tf_name,
                "direction": gemini["direction"], "entry": base_price, "stop_loss": sl, "take_profit": tp,
                "confidence": gemini["confidence"], "reason": gemini["summary"][:400],
            }
            if await asyncio.to_thread(insert_signal, gemini_row):
                created.append("GEMINI_V1")
                asyncio.create_task(notify.notify_new(gemini_row))

        for tf in LIVE_TFS:
            base = candles.get(tf)
            if not base:
                continue
            tf_atr = m.calculate_atr(base)
            if not tf_atr:
                continue
            htf = candles.get("60") if tf in ("5", "15") else candles.get("240")
            label = TF_LABEL.get(tf, tf)
            approved = await load_approved(tf)
            try:
                flag_set = filters.flags(m, base, htf, tf_atr, base[-1]["time"])
            except Exception:
                logger.exception("Filter flags failed")
                flag_set = None
            level_ctx = {"base": base, "atr": tf_atr, "now": base[-1]["time"], "tf": tf}

            tf_rule = rule if tf == primary else rules_signal(m, base)
            if tf_rule and tf_rule["direction"] in ("BUY", "SELL"):
                sl, tp = build_levels(tf_rule["direction"], base[-1]["close"], tf_atr)
                collect({
                    "strategy": "RULES_V1", "direction": tf_rule["direction"], "entry": base[-1]["close"],
                    "stop_loss": sl, "take_profit": tp, "confidence": tf_rule["confidence"],
                    "reason": "; ".join(tf_rule["reasons"]),
                }, approved, flag_set, level_ctx, label)

            try:
                for raw_item in smc.analyze(base, tf_atr, htf)["signals"]:
                    collect(raw_item, approved, flag_set, level_ctx, label)
            except Exception:
                logger.exception("SMC analysis failed")

            try:
                for raw_item in [
                    *classic.analyze(m, base, tf_atr), *classic2.analyze(m, base, tf_atr),
                    *patterns.analyze(m, base, tf_atr),
                ]:
                    collect(raw_item, approved, flag_set, level_ctx, label)
            except Exception:
                logger.exception("Classic strategies failed")

        pool.sort(key=lambda entry: entry[0], reverse=True)
        for _, row in pool:
            if await publish(row, context):
                created.append(f"{row['strategy']}@{row['timeframe']}")

        STATE["last_run"] = now
        if not light:
            STATE["last_error"] = gemini_error
        return {"status": "ok", "created": created, "gemini": bool(gemini), "gemini_error": gemini_error, "light": light}


async def auto_loop():
    await asyncio.sleep(20)
    last_heavy = 0.0
    while True:
        heavy = time.time() - last_heavy >= AI_INTERVAL_MIN * 60 - 30
        try:
            result = await run_cycle(auto=True, light=not heavy)
            if heavy and result.get("status") != "busy":
                last_heavy = time.time()
        except Exception:
            logger.exception("AI auto cycle failed")
        await asyncio.sleep(SCAN_SECONDS)


async def start_background():
    try:
        await asyncio.to_thread(ensure_tables)
    except Exception:
        logger.exception("Could not create AI tables")
        return
    if AI_AUTO:
        asyncio.create_task(auto_loop())


@router.get("/signals", include_in_schema=False)
def signals_page():
    from app import main as m
    return FileResponse(m.FRONTEND_DIR / "signals.html")


@router.get("/api/ai/status")
def ai_status():
    return {
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
        "model": STATE.get("gemini_model") or GEMINI_MODEL or "auto",
        "auto": AI_AUTO,
        "interval_minutes": AI_INTERVAL_MIN,
        "last_run": STATE["last_run"],
        "last_error": STATE["last_error"],
        "market_closed": STATE["market_closed"],
        "quality_gate": QUALITY_GATE,
        "approved": (STATE.get("approved_cache") or (0, None))[1],
    }


@router.post("/api/ai/run")
async def ai_run():
    return await run_cycle()


@router.get("/api/ai/signals")
async def ai_signals(limit: int = Query(50, ge=1, le=200)):
    rows = await asyncio.to_thread(db_rows, "SELECT * FROM ai_signals ORDER BY id DESC LIMIT :limit", {"limit": limit})
    return {"signals": rows}


@router.get("/api/ai/research/latest")
async def ai_research_latest():
    rows = await asyncio.to_thread(db_rows, "SELECT * FROM ai_research ORDER BY id DESC LIMIT 1")
    return {"research": rows[0] if rows else None}


@router.get("/api/ai/stats")
async def ai_stats():
    rows = await asyncio.to_thread(
        db_rows,
        "SELECT strategy, COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE status='WIN') AS wins, "
        "COUNT(*) FILTER (WHERE status='LOSS') AS losses, "
        "COUNT(*) FILTER (WHERE status='EXPIRED') AS expired, "
        "COUNT(*) FILTER (WHERE status='OPEN') AS open "
        "FROM ai_signals GROUP BY strategy ORDER BY strategy",
    )
    for row in rows:
        decided = row["wins"] + row["losses"]
        row["win_rate"] = round(row["wins"] / decided * 100, 1) if decided else None
    return {"strategies": rows}


@router.get("/api/ai/paper")
async def ai_paper():
    rows = await asyncio.to_thread(db_rows, "SELECT * FROM ai_signals ORDER BY created_at ASC, id ASC")
    risk_amount = PAPER_START_BALANCE * PAPER_RISK_PCT / 100
    closed = []
    open_trades = []

    for s in rows:
        risk_unit = abs(s["entry"] - s["stop_loss"])
        reward_unit = abs(s["take_profit"] - s["entry"])
        rr = reward_unit / risk_unit if risk_unit else 0
        if s["status"] == "OPEN":
            open_trades.append({
                "id": s["id"], "strategy": s["strategy"], "direction": s["direction"],
                "entry": s["entry"], "stop_loss": s["stop_loss"], "take_profit": s["take_profit"],
                "created_at": s["created_at"], "risk_amount": round(risk_amount, 2),
                "units": round(risk_amount / risk_unit, 4) if risk_unit else 0,
            })
            continue
        if s["status"] == "WIN":
            pnl = risk_amount * rr
        elif s["status"] == "LOSS":
            pnl = -risk_amount
        else:
            pnl = 0.0
        closed.append({
            "id": s["id"], "strategy": s["strategy"], "direction": s["direction"],
            "status": s["status"], "pnl": round(pnl, 2), "entry": s["entry"],
            "closed_at": s["closed_at"] or s["created_at"],
        })

    closed.sort(key=lambda t: t["closed_at"])
    equity = PAPER_START_BALANCE
    peak = equity
    max_dd = 0.0
    curve = []
    if rows:
        curve.append({"time": rows[0]["created_at"], "equity": round(equity, 2)})
    by_strategy = {}
    for trade in closed:
        equity += trade["pnl"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
        curve.append({"time": trade["closed_at"], "equity": round(equity, 2)})
        item = by_strategy.setdefault(trade["strategy"], {"trades": 0, "net_pnl": 0.0})
        item["trades"] += 1
        item["net_pnl"] = round(item["net_pnl"] + trade["pnl"], 2)

    wins = sum(1 for t in closed if t["status"] == "WIN")
    losses = sum(1 for t in closed if t["status"] == "LOSS")
    decided = wins + losses
    net = equity - PAPER_START_BALANCE
    return {
        "start_balance": PAPER_START_BALANCE,
        "risk_pct": PAPER_RISK_PCT,
        "equity": round(equity, 2),
        "net_pnl": round(net, 2),
        "return_pct": round(net / PAPER_START_BALANCE * 100, 2) if PAPER_START_BALANCE else 0,
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "max_drawdown_pct": round(max_dd, 2),
        "closed_count": len(closed),
        "open_count": len(open_trades),
        "open_trades": open_trades,
        "recent_closed": closed[-8:][::-1],
        "by_strategy": by_strategy,
        "curve": curve,
    }


SMC_CACHE = {"time": 0, "data": None}


@router.get("/api/ai/smc/zones")
async def ai_smc_zones():
    now = time.time()
    if SMC_CACHE["data"] and now - SMC_CACHE["time"] < 120:
        return SMC_CACHE["data"]

    from app import main as m

    symbol = m.GOLD_SYMBOL
    results = await asyncio.gather(
        m.fetch_analysis_history(symbol, "60"),
        m.fetch_analysis_history(symbol, "240"),
        return_exceptions=True,
    )
    if isinstance(results[0], Exception):
        raise HTTPException(status_code=502, detail="H1 ক্যান্ডেল পাওয়া যায়নি")
    h1 = results[0]
    h4 = None if isinstance(results[1], Exception) else results[1]
    atr = m.calculate_atr(h1)
    out = smc.analyze(h1, atr, h4)
    data = {
        "zones": out["zones"], "events": out["events"], "sweeps": out["sweeps"],
        "context": out["context"], "generated_at": int(now),
    }
    SMC_CACHE["time"] = now
    SMC_CACHE["data"] = data
    return data


@router.get("/api/ai/context")
async def ai_context():
    return analyst.public_context()


@router.get("/api/ai/reviews")
async def ai_reviews(limit: int = Query(15, ge=1, le=50)):
    return {"reviews": await analyst.recent_reviews(limit)}
