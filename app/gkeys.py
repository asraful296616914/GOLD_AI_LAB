import os
import time
import logging

import httpx
from fastapi import APIRouter

logger = logging.getLogger("gold-ai-lab.gkeys")
router = APIRouter()

# Railway Variables: GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3 (in this order).
# One key is used until its limit ends, then the next key takes over automatically.
ENV_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
COOLDOWN = max(1, int(os.getenv("GEMINI_KEY_COOLDOWN_MINUTES", "15"))) * 60
BAD_KEY_COOLDOWN = 6 * 3600

STATE = {"current": 1}
USAGE = {
    slot: {"calls": 0, "last_status": None, "last_at": None, "cooldown_until": 0.0}
    for slot in range(1, len(ENV_NAMES) + 1)
}


def _keys():
    """[(slot, key)] for every key set in Railway Variables (duplicates ignored)."""
    out, seen = [], set()
    for slot, name in enumerate(ENV_NAMES, start=1):
        key = os.getenv(name, "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append((slot, key))
    return out


def any_configured():
    return bool(_keys())


def _available(now=None):
    """Keys not cooling down, starting from the one currently in use."""
    now = now or time.time()
    keys = [(s, k) for s, k in _keys() if USAGE[s]["cooldown_until"] <= now]
    keys.sort(key=lambda item: (item[0] < STATE["current"], item[0]))
    return keys


def _cooldown(slot, seconds):
    USAGE[slot]["cooldown_until"] = time.time() + seconds


def record(slot, status_code):
    USAGE[slot]["calls"] += 1
    USAGE[slot]["last_status"] = status_code
    USAGE[slot]["last_at"] = int(time.time())


def _bad_key(response):
    if response.status_code in (401, 403):
        return True
    return response.status_code == 400 and "API_KEY_INVALID" in response.text


async def generate(body, models, url_template, timeout=60):
    """Call Gemini with automatic key rotation.

    Returns (data, model, error). Tries the current key on every model; when it is
    rate-limited (429) or invalid, it is paused and the next key is used.
    """
    keys = _keys()
    if not keys:
        return None, None, "GEMINI_API_KEY সেট করা নেই"
    pool = _available()
    if not pool:
        return None, None, "সব Gemini কী-র লিমিট শেষ, কিছুক্ষণ পরে আবার চেষ্টা হবে (429)"

    last_error = "কোনো Gemini মডেল পাওয়া যায়নি"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for slot, key in pool:
                headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
                limited = False
                for model in models:
                    response = await client.post(url_template.format(model=model), headers=headers, json=body)
                    record(slot, response.status_code)
                    if response.status_code == 404:
                        last_error = f"{model}: মডেল নেই (404)"
                        continue
                    if response.status_code == 429:
                        limited = True
                        last_error = f"{model}: লিমিট শেষ (429)"
                        continue
                    if _bad_key(response):
                        _cooldown(slot, BAD_KEY_COOLDOWN)
                        last_error = f"Gemini কী {slot} কাজ করছে না ({response.status_code})"
                        logger.warning("Gemini key %s rejected (%s), switching", slot, response.status_code)
                        limited = False
                        break
                    if response.status_code != 200:
                        return None, None, f"Gemini ত্রুটি {response.status_code} ({model})"
                    if STATE["current"] != slot:
                        logger.info("Gemini key switched to %s", slot)
                    STATE["current"] = slot
                    return response.json(), model, None
                if limited:
                    _cooldown(slot, COOLDOWN)
                    logger.warning("Gemini key %s limit reached, switching to next key", slot)
        return None, None, last_error
    except Exception as error:
        logger.exception("Gemini call failed")
        return None, None, f"Gemini কল ব্যর্থ: {type(error).__name__}"


@router.get("/api/ai/gemini-keys")
async def keys_status():
    now = time.time()
    return {
        "active_slot": STATE["current"],
        "slots": [
            {
                "slot": slot, "configured": any(s == slot for s, _ in _keys()),
                "calls": USAGE[slot]["calls"], "last_status": USAGE[slot]["last_status"],
                "last_at": USAGE[slot]["last_at"],
                "paused_for_seconds": max(0, int(USAGE[slot]["cooldown_until"] - now)),
            }
            for slot in USAGE
        ],
    }
