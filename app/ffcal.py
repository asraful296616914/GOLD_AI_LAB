import json
import time
import asyncio
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter

logger = logging.getLogger("gold-ai-lab.ffcal")
router = APIRouter()

# Forex Factory's own public weekly feed (not an official API: it can change or rate-limit).
FEEDS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GoldAILab/1.0)", "Accept": "application/json"}
TTL = 3600  # fetch at most once per hour
RETRY_AFTER_FAIL = 900
IMPACTS = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "holiday": "HOLIDAY"}
KV_DDL = "CREATE TABLE IF NOT EXISTS ai_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at BIGINT NOT NULL)"

STATE = {"events": [], "fetched_at": 0, "error": None, "next_try": 0.0, "loaded": False, "kv_ready": False}
LOCK = asyncio.Lock()


async def kv_get(key):
    from app import signals as sg

    try:
        if not STATE["kv_ready"]:
            await asyncio.to_thread(sg.db_exec, KV_DDL)
            STATE["kv_ready"] = True
        rows = await asyncio.to_thread(sg.db_rows, "SELECT v FROM ai_kv WHERE k=:k", {"k": key})
        return json.loads(rows[0]["v"]) if rows else None
    except Exception:
        logger.exception("kv_get failed (%s)", key)
        return None


async def kv_set(key, value):
    from app import signals as sg

    try:
        if not STATE["kv_ready"]:
            await asyncio.to_thread(sg.db_exec, KV_DDL)
            STATE["kv_ready"] = True
        await asyncio.to_thread(
            sg.db_exec,
            "INSERT INTO ai_kv (k, v, updated_at) VALUES (:k, :v, :t) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = EXCLUDED.updated_at",
            {"k": key, "v": json.dumps(value, ensure_ascii=False), "t": int(time.time())},
        )
    except Exception:
        logger.exception("kv_set failed (%s)", key)


def parse_feed(items):
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            ts = int(datetime.fromisoformat(item["date"]).timestamp())
        except Exception:
            continue
        impact = IMPACTS.get(str(item.get("impact", "")).strip().lower())
        if not impact:
            continue
        out.append({
            "time": ts, "currency": str(item.get("country", ""))[:6], "title": str(item.get("title", ""))[:120],
            "impact": impact, "forecast": str(item.get("forecast") or "")[:30],
            "previous": str(item.get("previous") or "")[:30], "actual": str(item.get("actual") or "")[:30],
        })
    return out


async def _load_snapshot():
    STATE["loaded"] = True
    saved = await kv_get("ff_calendar")
    if saved and saved.get("events"):
        STATE["events"], STATE["fetched_at"] = saved["events"], int(saved.get("fetched_at") or 0)


async def refresh(force=False):
    if not STATE["loaded"]:
        await _load_snapshot()
    now = time.time()
    if not force and (now - STATE["fetched_at"] < TTL or now < STATE["next_try"]):
        return
    async with LOCK:
        now = time.time()
        if not force and (now - STATE["fetched_at"] < TTL or now < STATE["next_try"]):
            return
        events = []
        try:
            async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
                for index, url in enumerate(FEEDS):
                    response = await client.get(url)
                    if response.status_code == 200:
                        events += parse_feed(response.json())
                    elif index == 0:
                        raise RuntimeError(f"Forex Factory ফিড HTTP {response.status_code}")
                    if index < len(FEEDS) - 1:
                        await asyncio.sleep(1)
            if not events:
                raise RuntimeError("ফিডে কোনো ইভেন্ট পাওয়া যায়নি")
        except Exception as error:
            logger.warning("Forex Factory feed failed: %s", error)
            STATE["error"] = str(error)[:160]
            STATE["next_try"] = time.time() + RETRY_AFTER_FAIL
            return
        unique = {(e["time"], e["currency"], e["title"]): e for e in events}
        STATE["events"] = sorted(unique.values(), key=lambda e: (e["time"], e["title"]))
        STATE["fetched_at"], STATE["error"] = int(time.time()), None
        await kv_set("ff_calendar", {"events": STATE["events"], "fetched_at": STATE["fetched_at"]})


@router.get("/api/ai/calendar")
async def calendar_events():
    from app import analyst

    await refresh()
    events, source = STATE["events"], "forexfactory" if STATE["events"] else "none"
    if not events:
        brief = analyst.STATE.get("brief") or {}
        if brief.get("events"):
            source = "gemini"
            events = [
                {"time": e["time"], "currency": "USD", "title": e["name"], "impact": e["impact"],
                 "forecast": e.get("consensus", ""), "previous": e.get("previous", ""), "actual": ""}
                for e in brief["events"]
            ]
    return {
        "source": source, "fetched_at": STATE["fetched_at"], "error": STATE["error"],
        "stale": bool(STATE["fetched_at"]) and time.time() - STATE["fetched_at"] > 3 * TTL,
        "now": int(time.time()), "events": events,
    }
