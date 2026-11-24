import json
import time
import asyncio
import logging

from fastapi import APIRouter

from app import ffcal, analyst

logger = logging.getLogger("gold-ai-lab.outlook")
router = APIRouter()

# UTC hours; Bangladesh time = UTC+6
SESSIONS = (
    ("ASIA", "এশিয়ান সেশন", 0, 7),
    ("LONDON", "লন্ডন সেশন", 7, 13),
    ("OVERLAP", "লন্ডন–নিউ ইয়র্ক ওভারল্যাপ", 13, 16),
    ("NEW_YORK", "নিউ ইয়র্ক সেশন", 16, 21),
)
BD = 6
STATS_DAYS = 180
STATS_TTL = 12 * 3600
OUTLOOK_TTL = 90 * 60
MIN_GAP = 30 * 60
STATE = {
    "stats": None, "stats_at": 0, "computing": False, "stats_error": None, "stats_try": 0,
    "outlook": None, "generating": False, "outlook_error": None, "last_try": 0, "loaded": False,
}


def _mean(values):
    return sum(values) / len(values) if values else None


def _r(value):
    return None if value is None else round(value, 2)


def _up(values):
    return round(sum(1 for v in values if v > 0) / len(values) * 100) if values else None


def compute_stats(candles):
    """Average movement of each session (whole session, first hour, last hour) from H1 candles."""
    acc = {key: {"range": [], "move": [], "s_move": [], "s_rng": [], "e_move": [], "e_rng": []} for key, *_ in SESSIONS}
    days = {}
    for c in candles:
        days.setdefault(c["time"] // 86400, []).append(c)
    for day, items in days.items():
        if (day + 3) % 7 >= 5:
            continue
        items.sort(key=lambda c: c["time"])
        for key, _, start, end in SESSIONS:
            sub = [c for c in items if start <= (c["time"] % 86400) // 3600 < end]
            if len(sub) < max(2, end - start - 1):
                continue
            first, last = sub[0], sub[-1]
            bucket = acc[key]
            bucket["range"].append(max(c["high"] for c in sub) - min(c["low"] for c in sub))
            bucket["move"].append(last["close"] - first["open"])
            bucket["s_move"].append(first["close"] - first["open"])
            bucket["s_rng"].append(first["high"] - first["low"])
            bucket["e_move"].append(last["close"] - last["open"])
            bucket["e_rng"].append(last["high"] - last["low"])
    out = {}
    for key, b in acc.items():
        out[key] = {
            "days": len(b["range"]), "avg_range": _r(_mean(b["range"])),
            "avg_abs_move": _r(_mean([abs(x) for x in b["move"]])), "up_pct": _up(b["move"]),
            "start": {"avg_abs": _r(_mean([abs(x) for x in b["s_move"]])), "up_pct": _up(b["s_move"]), "avg_range": _r(_mean(b["s_rng"]))},
            "end": {"avg_abs": _r(_mean([abs(x) for x in b["e_move"]])), "up_pct": _up(b["e_move"]), "avg_range": _r(_mean(b["e_rng"]))},
        }
    return out


async def _load():
    if STATE["loaded"]:
        return
    STATE["loaded"] = True
    saved = await ffcal.kv_get("session_stats")
    if saved and saved.get("stats"):
        STATE["stats"], STATE["stats_at"] = saved["stats"], int(saved.get("at") or 0)
    saved = await ffcal.kv_get("session_outlook")
    if saved:
        STATE["outlook"] = saved


async def _compute_stats():
    from app import main as m, backtest

    try:
        candles = await backtest.load_history(m, m.GOLD_SYMBOL, "60", STATS_DAYS)
        if len(candles) < 500:
            raise ValueError(f"পর্যাপ্ত ক্যান্ডেল নেই ({len(candles)}টি)")
        STATE["stats"] = await asyncio.to_thread(compute_stats, candles)
        STATE["stats_at"], STATE["stats_error"] = int(time.time()), None
        await ffcal.kv_set("session_stats", {"stats": STATE["stats"], "at": STATE["stats_at"], "days": STATS_DAYS})
    except Exception as error:
        logger.exception("Session stats failed")
        STATE["stats_error"] = str(getattr(error, "detail", error))[:160]
    finally:
        STATE["computing"] = False


def _events_for_prompt(now):
    rows = []
    for e in ffcal.STATE["events"]:
        if not (now - 3600 <= e["time"] <= now + 26 * 3600):
            continue
        major = e["currency"] == "USD" and e["impact"] in ("HIGH", "MEDIUM")
        other = e["currency"] in ("EUR", "GBP", "JPY", "CNY", "CHF") and e["impact"] == "HIGH"
        if not (major or other):
            continue
        hour = (e["time"] % 86400) // 3600
        session = next((key for key, _, a, b in SESSIONS if a <= hour < b), "OFF_HOURS")
        rows.append({
            "bd_time": time.strftime("%m-%d %H:%M", time.gmtime(e["time"] + BD * 3600)), "session": session,
            "currency": e["currency"], "title": e["title"], "impact": e["impact"],
            "forecast": e["forecast"], "previous": e["previous"],
        })
    return rows[:14]


def _t(value, limit=320):
    return str(value or "").strip()[:limit]


async def _generate():
    from app import main as m

    try:
        now = int(time.time())
        await ffcal.refresh()
        h1 = await m.fetch_analysis_history(m.GOLD_SYMBOL, "60", bars=160)
        atr = m.calculate_atr(h1)
        sess = analyst.session_context(now, h1, atr)
        data = {
            "utc_now": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)), "current_session": sess["name"],
            "gold_price": round(h1[-1]["close"], 2), "h1_atr": round(atr, 2) if atr else None,
            "asian_range_today": sess.get("asian_range"), "asian_position": sess.get("asian_position"),
            "history_by_session_last_180_days": STATE["stats"], "upcoming_events": _events_for_prompt(now),
        }
        prompt = (
            "You are a gold (XAU/USD) market analyst. Using ONLY the data below, write a short outlook for each "
            "trading session in Bengali. history_by_session_last_180_days gives, per session (UTC hours: ASIA 00-07, "
            "LONDON 07-13, OVERLAP 13-16, NEW_YORK 16-21), the average range in dollars, how often the session closed "
            "up, and the average move/up-share in its first hour (start) and last hour (end). "
            "For each session write: now_bn (only for the session that is currently running, else empty string), "
            "start_bn (what the history suggests for the first hour and how the day's context or events could change it), "
            "end_bn (same for the last hour). Mention events that fall inside that session; a high-impact event usually "
            "means a bigger move and the direction depends on the result versus the forecast. "
            "Rules: describe probabilities and history, never a certain prediction; never invent numbers that are not in "
            "the data; 1-2 short sentences per field. Educational context, not financial advice. "
            'Return ONLY JSON: {"headline_bn": "one sentence for the next 24 hours", "sessions": {"ASIA": {"now_bn": "", '
            '"start_bn": "", "end_bn": ""}, "LONDON": {...}, "OVERLAP": {...}, "NEW_YORK": {...}}}\n\nDATA:\n'
            + json.dumps(data, ensure_ascii=False)
        )
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.3}}
        result, error = await analyst.gemini_post(body)
        if not result:
            raise RuntimeError(error or "Gemini উত্তর দেয়নি")
        parsed = analyst.parse_json(result["text"]) or {}
        raw = parsed.get("sessions") if isinstance(parsed.get("sessions"), dict) else {}
        sessions = {}
        for key, *_ in SESSIONS:
            item = raw.get(key) if isinstance(raw.get(key), dict) else {}
            sessions[key] = {"now": _t(item.get("now_bn")), "start": _t(item.get("start_bn")), "end": _t(item.get("end_bn"))}
        outlook = {
            "generated_at": now, "session": sess["name"], "price": data["gold_price"], "model": result["model"],
            "headline": _t(parsed.get("headline_bn"), 300), "sessions": sessions, "events": len(data["upcoming_events"]),
        }
        STATE["outlook"], STATE["outlook_error"] = outlook, None
        await ffcal.kv_set("session_outlook", outlook)
    except Exception as error:
        logger.exception("Session outlook failed")
        STATE["outlook_error"] = str(getattr(error, "detail", error))[:160]
    finally:
        STATE["generating"] = False


@router.get("/api/ai/session-outlook")
async def session_outlook():
    await _load()
    now = int(time.time())
    if not STATE["computing"] and now - STATE["stats_at"] > STATS_TTL and now - STATE["stats_try"] > 600:
        STATE["computing"], STATE["stats_try"] = True, now
        asyncio.create_task(_compute_stats())
    current = analyst.session_context(now)
    out = STATE["outlook"]
    if STATE["stats"] and not STATE["generating"] and now - STATE["last_try"] > MIN_GAP:
        age = now - out["generated_at"] if out else None
        if out is None or age > OUTLOOK_TTL or (out.get("session") != current["name"] and age > MIN_GAP):
            STATE["generating"], STATE["last_try"] = True, now
            asyncio.create_task(_generate())
    stats = STATE["stats"] or {}
    sessions = []
    for key, label, start, end in SESSIONS:
        sessions.append({
            "key": key, "label": label, "utc": [start, end], "is_current": current["name"] == key,
            "bd_window": f"{(start + BD) % 24:02d}:00–{(end + BD) % 24:02d}:00",
            "stats": stats.get(key), "outlook": ((out or {}).get("sessions") or {}).get(key),
        })
    return {
        "now": now, "current": current["name"], "market_text": current["text"], "sessions": sessions,
        "headline": (out or {}).get("headline", ""), "generated_at": (out or {}).get("generated_at"),
        "stats_at": STATE["stats_at"], "stats_days": STATS_DAYS,
        "stats_status": "computing" if STATE["computing"] else "ready" if STATE["stats"] else "error" if STATE["stats_error"] else "waiting",
        "outlook_status": "generating" if STATE["generating"] else "ready" if out else "error" if STATE["outlook_error"] else "waiting",
        "error": STATE["stats_error"] or STATE["outlook_error"],
    }
