import os
import re
import time
import json
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

logger = logging.getLogger("gold-ai-lab.context")
router = APIRouter()

CONTEXT_AUTO = os.getenv("CONTEXT_AUTO", "1") == "1"
INTERVAL_MIN = max(30, int(os.getenv("CONTEXT_INTERVAL_MINUTES", "60")))
STRICT = os.getenv("ANALYST_STRICT", "0") == "1"
STALE_SECONDS = 4 * 3600
EVENT_WINDOW = 30 * 60
STATE = {"data": None, "error": None, "last_try": None}
LOCK = asyncio.Lock()

DDL = "CREATE TABLE IF NOT EXISTS ai_context (id BIGSERIAL PRIMARY KEY, created_at BIGINT NOT NULL, data TEXT NOT NULL)"


def session_info(ts):
    hour = (ts // 3600) % 24
    weekday = (ts // 86400 + 3) % 7
    if weekday == 5 or (weekday == 6 and hour < 22) or (weekday == 4 and hour >= 22):
        return {"name": "মার্কেট বন্ধ", "volatility": "নেই", "note": "সাপ্তাহিক ছুটি, রবিবার রাতে খোলে"}
    if hour < 7:
        return {"name": "এশিয়ান সেশন", "volatility": "কম", "note": "সাধারণত শান্ত ও রেঞ্জে চলে, ব্রেকআউটের আগে দাম জমে"}
    if hour < 12:
        return {"name": "লন্ডন সেশন", "volatility": "বেশি", "note": "এশিয়ান রেঞ্জ ভেঙে বড় চাল শুরু হতে পারে"}
    if hour < 16:
        return {"name": "লন্ডন + নিউ ইয়র্ক ওভারল্যাপ", "volatility": "সর্বোচ্চ", "note": "সবচেয়ে বেশি ভলিউম, মার্কিন খবরও এই সময়ে আসে"}
    if hour < 21:
        return {"name": "নিউ ইয়র্ক সেশন", "volatility": "মাঝারি-বেশি", "note": "লন্ডন বন্ধের পর গতি কমতে থাকে"}
    return {"name": "লেট নিউ ইয়র্ক", "volatility": "কম", "note": "ভলিউম কম, হঠাৎ স্প্রেড বাড়তে পারে"}


def to_epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, TypeError):
        return None


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(text[start:end + 1])


def normalize(raw, model, grounded, now):
    bias = str(raw.get("bias", "NEUTRAL")).upper()
    if bias not in ("BULLISH", "BEARISH", "NEUTRAL"):
        bias = "NEUTRAL"
    risk = str(raw.get("risk_level", "MEDIUM")).upper()
    if risk not in ("LOW", "MEDIUM", "HIGH"):
        risk = "MEDIUM"
    try:
        confidence = max(0, min(100, int(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    events = []
    for item in (raw.get("events") or [])[:6]:
        if isinstance(item, dict) and item.get("name"):
            impact = str(item.get("impact", "MEDIUM")).upper()
            events.append({
                "name": str(item["name"])[:80],
                "time": to_epoch(item.get("time_utc")),
                "impact": impact if impact in ("HIGH", "MEDIUM") else "MEDIUM",
                "note": str(item.get("note_bn", ""))[:160],
            })
    return {
        "bias": bias, "confidence": confidence, "risk_level": risk,
        "summary": str(raw.get("summary_bn", ""))[:900],
        "drivers": [str(x)[:160] for x in (raw.get("drivers_bn") or [])[:4]],
        "events": events,
        "avoid_until": to_epoch(raw.get("avoid_until_utc")),
        "updated_at": now, "grounded": grounded, "model": model,
    }


def fresh(now):
    data = STATE["data"]
    if data and now - data["updated_at"] <= STALE_SECONDS:
        return data
    return None


def risk_window(now):
    data = fresh(now)
    if not data or not data["grounded"]:
        return None
    if data.get("avoid_until") and now < data["avoid_until"] and data["avoid_until"] - now <= 6 * 3600:
        return "বড় খবরের ঝুঁকি, নতুন ট্রেড এড়ানো হচ্ছে"
    for event in data["events"]:
        if event["impact"] == "HIGH" and event["time"] and abs(event["time"] - now) <= EVENT_WINDOW:
            return f"হাই-ইমপ্যাক্ট খবর: {event['name']} (৩০ মিনিটের মধ্যে)"
    return None


def conflicts(direction, now=None):
    if not STRICT:
        return False
    data = fresh(int(now if now is not None else time.time()))
    if not data or data["confidence"] < 70:
        return False
    return (direction == "BUY" and data["bias"] == "BEARISH") or (direction == "SELL" and data["bias"] == "BULLISH")


def annotate(item, now=None):
    data = fresh(int(now if now is not None else time.time()))
    if not data:
        return item
    return {**item, "reason": f"{item.get('reason', '')} | AI প্রেক্ষাপট: {data['bias']}, ঝুঁকি {data['risk_level']}"[:400]}


def build_prompt(now, grounded):
    iso = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"You are a senior gold (XAU/USD) market analyst. Current time: {iso}. "
        "Think like a careful human trader: consider the current trading session, recent gold news, the US dollar "
        "and Treasury yield direction, central-bank (Fed) expectations, geopolitical risk, and the economic calendar "
        "for the NEXT 24 HOURS (high-impact USD events such as NFP, CPI, PPI, FOMC, jobless claims, Fed speeches). "
        "Return ONLY one JSON object, no markdown, with keys: "
        "bias (BULLISH, BEARISH or NEUTRAL: likely gold direction over the next 1-2 days), "
        "confidence (integer 0-100), risk_level (LOW, MEDIUM or HIGH: event and news risk for trading in the next 6 hours), "
        "summary_bn (3-4 short Bengali sentences), drivers_bn (array of up to 4 short Bengali strings), "
        "events (array of up to 5 objects: name, time_utc as ISO 8601 or null, impact HIGH or MEDIUM, note_bn), "
        "avoid_until_utc (ISO 8601 time until which opening new trades is unwise because of an imminent high-impact event, or null). "
        "Be honest about uncertainty and never invent events or times. Educational analysis, not financial advice."
    )
    if not grounded:
        prompt += " Live web search is unavailable: do not claim current news, set events to [] and avoid_until_utc to null."
    return prompt


async def refresh():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        STATE["error"] = "GEMINI_API_KEY সেট করা নেই"
        return None
    async with LOCK:
        from app import signals as sg

        now = int(time.time())
        STATE["last_try"] = now
        headers = {"x-goog-api-key": key.strip(), "Content-Type": "application/json"}
        last_error = "কোনো মডেল সাড়া দেয়নি"
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                for grounded in (True, False):
                    body = {
                        "contents": [{"parts": [{"text": build_prompt(now, grounded)}]}],
                        "generationConfig": {"temperature": 0.2},
                    }
                    if grounded:
                        body["tools"] = [{"google_search": {}}]
                    for model in sg.gemini_candidates():
                        response = await client.post(sg.GEMINI_URL.format(model=model), headers=headers, json=body)
                        if response.status_code != 200:
                            last_error = f"{model}: HTTP {response.status_code}"
                            continue
                        parts = response.json()["candidates"][0]["content"]["parts"]
                        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                        try:
                            data = normalize(parse_json(text), model, grounded, now)
                        except (ValueError, KeyError):
                            last_error = f"{model}: উত্তর পড়া যায়নি"
                            continue
                        STATE["data"], STATE["error"] = data, None
                        try:
                            await asyncio.to_thread(sg.db_exec, DDL)
                            await asyncio.to_thread(
                                sg.db_exec,
                                "INSERT INTO ai_context (created_at, data) VALUES (:created_at, :data)",
                                {"created_at": now, "data": json.dumps(data)},
                            )
                        except Exception:
                            logger.exception("Could not save context")
                        return data
        except Exception as error:
            logger.exception("Context refresh failed")
            last_error = f"ব্যর্থ: {type(error).__name__}"
        STATE["error"] = last_error
        return None


async def load_saved():
    try:
        from app import signals as sg

        await asyncio.to_thread(sg.db_exec, DDL)
        rows = await asyncio.to_thread(sg.db_rows, "SELECT data FROM ai_context ORDER BY id DESC LIMIT 1")
        if rows:
            STATE["data"] = json.loads(rows[0]["data"])
    except Exception:
        logger.exception("Could not load saved context")


async def auto_loop():
    await load_saved()
    await asyncio.sleep(45)
    while True:
        now = int(time.time())
        if (now // 86400 + 3) % 7 >= 5:
            wait = 6 * 3600
        else:
            wait = INTERVAL_MIN * 60
        if not STATE["data"] or now - STATE["data"]["updated_at"] >= wait - 60:
            await refresh()
        await asyncio.sleep(300)


def payload():
    now = int(time.time())
    return {
        "context": STATE["data"], "session": session_info(now),
        "blocked": risk_window(now), "error": STATE["error"], "strict": STRICT,
    }


@router.get("/api/ai/context")
async def context_get():
    return payload()


@router.post("/api/ai/context/refresh")
async def context_refresh():
    last = STATE["last_try"]
    if last and time.time() - last < 120:
        return {**payload(), "cooldown": True}
    await refresh()
    return payload()
