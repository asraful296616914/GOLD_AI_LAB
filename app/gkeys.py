import os
import re
import time
import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("gold-ai-lab.gkeys")
router = APIRouter()

SLOT_ROLE = {1: "research", 2: "review", 3: "brief"}
ROLE_SLOT = {role: slot for slot, role in SLOT_ROLE.items()}
SLOT_LABEL = {
    1: "কী ১ · বাজার বিশ্লেষণ + Gemini সিগন্যাল",
    2: "কী ২ · প্রতিটা সিগন্যালের AI যাচাই",
    3: "কী ৩ · খবর ও ইকোনমিক ক্যালেন্ডার (ওয়েব সার্চ)",
}
KEY_RE = re.compile(r"^[A-Za-z0-9_\-\.]{20,200}$")
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
CACHE = {"at": 0.0, "slots": {}, "source": {}}
USAGE = {slot: {"calls": 0, "last_status": None, "last_at": None} for slot in SLOT_ROLE}
LAST_SLOT = {}


def _env(slot):
    return os.getenv("GEMINI_API_KEY" if slot == 1 else f"GEMINI_API_KEY_{slot}", "").strip()


def any_configured():
    return bool(CACHE["slots"]) or bool(_env(1))


async def load(force=False):
    if not force and CACHE["at"] and time.time() - CACHE["at"] < 60:
        return CACHE["slots"]
    stored = {}
    try:
        from app import signals as sg
        from app import notify

        await asyncio.to_thread(sg.db_exec, notify.DDL)
        rows = await asyncio.to_thread(sg.db_rows, "SELECT key, value FROM ai_settings WHERE key LIKE 'gemini_key_%'")
        stored = {row["key"]: row["value"] for row in rows}
    except Exception:
        logger.warning("Could not load saved Gemini keys")
    slots, source = {}, {}
    for slot in SLOT_ROLE:
        saved = (stored.get(f"gemini_key_{slot}") or "").strip()
        if saved:
            slots[slot], source[slot] = saved, "saved"
        elif _env(slot):
            slots[slot], source[slot] = _env(slot), "env"
    CACHE.update(at=time.time(), slots=slots, source=source)
    return slots


async def key_for(role):
    slots = await load()
    slot = ROLE_SLOT[role]
    if slot not in slots:
        # no dedicated key for this role: use the first key that exists (never a rate-limit fallback)
        slot = next((s for s in sorted(slots)), None)
    if slot is None:
        return None
    LAST_SLOT[role] = slot
    return slots[slot]


def record(role, status_code):
    slot = LAST_SLOT.get(role)
    if slot in USAGE:
        USAGE[slot]["calls"] += 1
        USAGE[slot]["last_status"] = status_code
        USAGE[slot]["last_at"] = int(time.time())


async def _verify(key):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(MODELS_URL, headers={"x-goog-api-key": key})
        return response.status_code
    except Exception as error:
        logger.warning("Gemini key check failed (%s)", type(error).__name__)
        return None


def _status():
    slots, source = CACHE["slots"], CACHE["source"]
    out = []
    for slot in SLOT_ROLE:
        key = slots.get(slot, "")
        out.append({
            "slot": slot, "label": SLOT_LABEL[slot], "configured": bool(key),
            "source": source.get(slot, ""), "hint": ("…" + key[-4:]) if key else "",
            "calls": USAGE[slot]["calls"], "last_status": USAGE[slot]["last_status"],
            "last_at": USAGE[slot]["last_at"],
        })
    return {"slots": out, "admin_protected": bool(os.getenv("ADMIN_KEY", ""))}


@router.get("/api/ai/gemini-keys")
async def keys_status():
    await load(force=True)
    return _status()


@router.post("/api/ai/gemini-keys/save")
async def keys_save(request: Request):
    from app import notify

    payload = await notify._payload(request)
    notify._check_admin(payload)
    try:
        slot = int(payload.get("slot", 0))
    except (TypeError, ValueError):
        slot = 0
    key = str(payload.get("key", "")).strip()
    if slot not in SLOT_ROLE:
        raise HTTPException(status_code=400, detail="কী নম্বর ঠিক নয় (১, ২ বা ৩)")
    if not KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="কী-র ফরম্যাট ঠিক নয়")
    status = await _verify(key)
    if status != 200:
        raise HTTPException(status_code=400, detail=f"কী কাজ করছে না (Google সাড়া: {status or 'সংযোগ হয়নি'})")
    await notify.save_settings({f"gemini_key_{slot}": key})
    await load(force=True)
    return _status()


@router.post("/api/ai/gemini-keys/remove")
async def keys_remove(request: Request):
    from app import notify

    payload = await notify._payload(request)
    notify._check_admin(payload)
    try:
        slot = int(payload.get("slot", 0))
    except (TypeError, ValueError):
        slot = 0
    if slot not in SLOT_ROLE:
        raise HTTPException(status_code=400, detail="কী নম্বর ঠিক নয় (১, ২ বা ৩)")
    await notify.save_settings({f"gemini_key_{slot}": ""})
    await load(force=True)
    return _status()
