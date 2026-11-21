import os
import re
import time
import json
import asyncio
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("gold-ai-lab.analyst")

BRIEF_TTL = 90 * 60
MANUAL_BRIEF_TTL = 10 * 60
BLACKOUT_BEFORE = 20 * 60
BLACKOUT_AFTER = 30 * 60
MAX_REVIEWS_PER_CYCLE = 4
STATE = {"brief": None, "brief_at": 0, "brief_error": None, "context": None, "reviews_ready": False}

DDL_REVIEWS = (
    "CREATE TABLE IF NOT EXISTS ai_reviews ("
    "id BIGSERIAL PRIMARY KEY, created_at BIGINT NOT NULL, strategy TEXT NOT NULL, "
    "direction TEXT NOT NULL, verdict TEXT NOT NULL, confidence INTEGER, reason TEXT)"
)

SESSION_TEXT = {
    "CLOSED": "মার্কেট বন্ধ (সপ্তাহান্ত)",
    "ASIA": "এশিয়ান সেশন: সাধারণত শান্ত, রেঞ্জে চলে",
    "LONDON": "লন্ডন সেশন: মুভমেন্ট বাড়ে, রেঞ্জ ভেঙে ব্রেকআউট হয়",
    "OVERLAP": "লন্ডন–নিউ ইয়র্ক ওভারল্যাপ: সবচেয়ে সক্রিয় ও অস্থির সময়",
    "NEW_YORK": "নিউ ইয়র্ক সেশন: ডেটা ও খবরে ঝাঁকুনি, শেষের দিকে ধীর",
    "LATE": "দিনের শেষ/রোলওভার: তারল্য কম, স্প্রেড বাড়ে",
}


def _weekday(ts):
    return (ts // 86400 + 3) % 7


def _next_open(now, hour):
    day_start = now - now % 86400
    for offset in range(0, 5):
        candidate = day_start + offset * 86400 + hour * 3600
        if candidate > now and _weekday(candidate) < 5:
            return round((candidate - now) / 60)
    return None


def session_context(now, h1=None, atr=None):
    hour = (now // 3600) % 24
    weekday = _weekday(now)
    closed = weekday == 5 or (weekday == 6 and hour < 22) or (weekday == 4 and hour >= 21)
    if closed:
        name = "CLOSED"
    elif hour < 7:
        name = "ASIA"
    elif hour < 13:
        name = "LONDON"
    elif hour < 16:
        name = "OVERLAP"
    elif hour < 21:
        name = "NEW_YORK"
    else:
        name = "LATE"

    out = {
        "name": name, "text": SESSION_TEXT[name], "utc_hour": hour,
        "minutes_to_london_open": _next_open(now, 7), "minutes_to_ny_open": _next_open(now, 13),
    }
    if h1 and atr:
        day_start = now - now % 86400
        asian = [x for x in h1 if day_start <= x["time"] < day_start + 7 * 3600]
        if len(asian) >= 4:
            high = max(x["high"] for x in asian)
            low = min(x["low"] for x in asian)
            price = h1[-1]["close"]
            position = "ABOVE" if price > high else "BELOW" if price < low else "INSIDE"
            out["asian_range"] = round(high - low, 2)
            out["asian_vs_atr"] = round((high - low) / atr, 2)
            out["asian_position"] = position
            if name in ("LONDON", "OVERLAP"):
                out["text"] += {
                    "INSIDE": " (দাম এখনো এশিয়ান রেঞ্জের ভেতরে)",
                    "ABOVE": " (দাম এশিয়ান রেঞ্জের ওপরে বেরিয়েছে)",
                    "BELOW": " (দাম এশিয়ান রেঞ্জের নিচে বেরিয়েছে)",
                }[position]
    return out


def parse_json(text):
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception:
        return None


async def gemini_post(body):
    from app import signals as sg

    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None, "GEMINI_API_KEY সেট করা নেই"
    headers = {"x-goog-api-key": key.strip(), "Content-Type": "application/json"}
    last_error = "কোনো Gemini মডেল পাওয়া যায়নি"
    try:
        async with httpx.AsyncClient(timeout=70) as client:
            for model in sg.gemini_candidates():
                response = await client.post(sg.GEMINI_URL.format(model=model), headers=headers, json=body)
                if response.status_code in (404, 429):
                    last_error = f"{model}: {'মডেল নেই (404)' if response.status_code == 404 else 'লিমিট শেষ (429)'}"
                    continue
                if response.status_code != 200:
                    return None, f"Gemini ত্রুটি {response.status_code} ({model})"
                data = response.json()
                candidate = data["candidates"][0]
                parts = candidate["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                sg.STATE["gemini_model"] = model
                return {"text": text, "model": model, "grounded": bool(candidate.get("groundingMetadata"))}, None
        return None, last_error
    except Exception as error:
        logger.exception("Gemini analyst call failed")
        return None, f"Gemini কল ব্যর্থ: {type(error).__name__}"


def _event_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return None


def normalize_brief(parsed, now, grounded, model):
    events = []
    headlines = []
    if grounded:
        for event in (parsed.get("events") or [])[:10]:
            ts = _event_time(event.get("time_utc"))
            impact = str(event.get("impact", "")).upper()
            if ts is None or impact not in ("HIGH", "MEDIUM", "LOW"):
                continue
            if now - 2 * 3600 <= ts <= now + 72 * 3600:
                events.append({"time": ts, "name": str(event.get("name", ""))[:80], "impact": impact})
        headlines = [str(h)[:160] for h in (parsed.get("headlines") or [])[:5]]
        events.sort(key=lambda e: e["time"])
    sentiment = str(parsed.get("sentiment", "NEUTRAL")).upper()
    if sentiment not in ("BULLISH", "BEARISH", "NEUTRAL"):
        sentiment = "NEUTRAL"
    summary = str(parsed.get("summary_bn", ""))[:900]
    if not grounded:
        summary = "(ইন্টারনেট সার্চ ছাড়া, খবর ও ইভেন্ট যাচাই করা যায়নি) " + summary
    return {
        "sentiment": sentiment, "summary": summary, "events": events, "headlines": headlines,
        "grounded": grounded, "model": model, "generated_at": now,
    }


async def get_brief(force=False, now=None):
    now = now or int(time.time())
    ttl = MANUAL_BRIEF_TTL if force else BRIEF_TTL
    if STATE["brief"] and now - STATE["brief_at"] < ttl:
        return STATE["brief"]

    date = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"Now is {date}. Use Google Search to find CURRENT information relevant to gold (XAU/USD). "
        "Find: (1) high and medium impact economic events (US data, Fed speakers, central banks) scheduled in the "
        "next 48 hours with exact UTC time; (2) the 3-5 latest headlines moving gold (Fed rate expectations, US dollar, "
        "bond yields, geopolitics, central bank buying); (3) the overall bias. "
        "Never invent anything: if you cannot verify an item, leave it out and use empty arrays. "
        'Return ONLY a JSON object: {"sentiment": "BULLISH|BEARISH|NEUTRAL", '
        '"summary_bn": "3-4 short sentences in Bengali", '
        '"events": [{"time_utc": "YYYY-MM-DDTHH:MM", "name": "...", "impact": "HIGH|MEDIUM"}], '
        '"headlines": ["short English headline (source)"]}. Educational context, not financial advice.'
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    result, error = await gemini_post(body)
    if not result and error and "400" in error:
        body.pop("tools")
        result, error = await gemini_post(body)
        if result:
            result["grounded"] = False
    if not result:
        STATE["brief_error"] = error
        return STATE["brief"]
    parsed = parse_json(result["text"])
    if not parsed:
        STATE["brief_error"] = "ব্রিফ পড়া যায়নি"
        return STATE["brief"]
    STATE["brief"] = normalize_brief(parsed, now, result["grounded"], result["model"])
    STATE["brief_at"] = now
    STATE["brief_error"] = None
    return STATE["brief"]


def blackout(brief, now):
    if not brief:
        return None
    for event in brief.get("events", []):
        if event["impact"] == "HIGH" and event["time"] - BLACKOUT_BEFORE <= now <= event["time"] + BLACKOUT_AFTER:
            return event
    return None


def brief_for_prompt(brief, now=None):
    now = now or int(time.time())
    return {
        "sentiment": brief["sentiment"], "summary": brief["summary"], "internet_verified": brief["grounded"],
        "upcoming_events": [
            {"in_minutes": round((e["time"] - now) / 60), "name": e["name"], "impact": e["impact"]}
            for e in brief["events"] if e["time"] > now - 1800
        ][:6],
        "headlines": brief["headlines"][:4],
    }


async def build_context(m, h1, atr, now, force=False, technical=None):
    session = session_context(now, h1, atr)
    brief = None
    try:
        brief = await get_brief(force, now)
    except Exception:
        logger.exception("Brief failed")
    context = {
        "session": session, "brief": brief, "blackout": blackout(brief, now),
        "technical": technical, "generated_at": now,
    }
    STATE["context"] = {"session": session, "generated_at": now}
    return context


def public_context():
    now = int(time.time())
    stored = (STATE["context"] or {}).get("session") or {}
    session = session_context(now)
    for key in ("asian_range", "asian_vs_atr", "asian_position"):
        if key in stored:
            session[key] = stored[key]
    return {
        "session": session, "brief": STATE["brief"], "brief_error": STATE["brief_error"],
        "blackout": blackout(STATE["brief"], now),
        "generated_at": (STATE["context"] or {}).get("generated_at"),
    }


async def log_review(row, verdict, confidence, reason):
    from app import signals as sg

    try:
        if not STATE["reviews_ready"]:
            await asyncio.to_thread(sg.db_exec, DDL_REVIEWS)
            STATE["reviews_ready"] = True
        await asyncio.to_thread(
            sg.db_exec,
            "INSERT INTO ai_reviews (created_at, strategy, direction, verdict, confidence, reason) "
            "VALUES (:created_at, :strategy, :direction, :verdict, :confidence, :reason)",
            {"created_at": int(time.time()), "strategy": row["strategy"], "direction": row["direction"],
             "verdict": verdict, "confidence": confidence, "reason": reason[:400]},
        )
    except Exception:
        logger.exception("Could not log review")


async def recent_reviews(limit=15):
    from app import signals as sg

    if not STATE["reviews_ready"]:
        await asyncio.to_thread(sg.db_exec, DDL_REVIEWS)
        STATE["reviews_ready"] = True
    return await asyncio.to_thread(sg.db_rows, "SELECT * FROM ai_reviews ORDER BY id DESC LIMIT :limit", {"limit": limit})


async def review(row, context):
    from app import signals as sg

    used = sg.STATE.get("reviews_used", 0)
    if used >= MAX_REVIEWS_PER_CYCLE:
        return {"verdict": "SKIP", "confidence": 0, "reason": "এই রানে যাচাইয়ের সীমা শেষ"}
    sg.STATE["reviews_used"] = used + 1

    risk = abs(row["entry"] - row["stop_loss"])
    signal = {
        "strategy": row["strategy"], "direction": row["direction"], "entry": round(row["entry"], 2),
        "stop_loss": round(row["stop_loss"], 2), "take_profit": round(row["take_profit"], 2),
        "reward_to_risk": round(abs(row["take_profit"] - row["entry"]) / risk, 2) if risk else None,
        "why": row.get("reason", "")[:200],
    }
    brief = context.get("brief")
    data = {
        "signal": signal, "session": context["session"], "technical": context.get("technical"),
        "news_and_calendar": brief_for_prompt(brief) if brief else "unavailable",
    }
    prompt = (
        "You are a senior gold (XAU/USD) trader doing the FINAL CHECK on an automated trade signal, thinking like a "
        "careful human analyst. Weigh: (1) the trading session and how gold usually behaves in it (Asian = quiet range, "
        "London open = expansion, London/NY overlap = most active, late NY = thin); (2) the multi-timeframe technical "
        "picture; (3) the news and economic calendar (reject if a high-impact event is imminent or the signal fights a "
        "clear macro bias); (4) whether the reward-to-risk and stop placement are realistic. "
        "Use ONLY the data provided; do not invent prices or news. Return ONLY JSON: "
        '{"verdict": "APPROVE|REJECT|WAIT", "confidence": 0-100, "reason_bn": "2 short sentences in Bengali"}. '
        "Prefer REJECT or WAIT when evidence is mixed. Educational analysis, not financial advice.\n\nDATA:\n"
        + json.dumps(data, ensure_ascii=False)
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    result, error = await gemini_post(body)
    if not result:
        return None
    parsed = parse_json(result["text"])
    if not parsed:
        return None
    verdict = str(parsed.get("verdict", "WAIT")).upper()
    if verdict not in ("APPROVE", "REJECT", "WAIT"):
        verdict = "WAIT"
    try:
        confidence = max(0, min(100, int(parsed.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    reason = str(parsed.get("reason_bn", ""))[:300]
    await log_review(row, verdict, confidence, reason)
    return {"verdict": verdict, "confidence": confidence, "reason": reason}
