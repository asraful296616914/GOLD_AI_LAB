import os
import re
import time
import hmac
import html as htmllib
import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("gold-ai-lab.notify")
router = APIRouter()

DDL = "CREATE TABLE IF NOT EXISTS ai_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,60}$")
CHAT_RE = re.compile(r"^(-?\d{5,20}|@[A-Za-z0-9_]{5,32})$")
CACHE = {"at": 0.0, "data": None}
LAST_TEST = {"at": 0.0}


def _sg():
    from app import signals as sg

    return sg


async def load_settings(force=False):
    if not force and CACHE["data"] is not None and time.time() - CACHE["at"] < 60:
        return CACHE["data"]
    stored = {}
    try:
        sg = _sg()
        await asyncio.to_thread(sg.db_exec, DDL)
        rows = await asyncio.to_thread(sg.db_rows, "SELECT key, value FROM ai_settings")
        stored = {row["key"]: row["value"] for row in rows}
    except Exception:
        logger.warning("Could not load notification settings")
    settings = {
        "token": (stored.get("telegram_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip(),
        "chat_id": (stored.get("telegram_chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip(),
        "enabled": stored.get("telegram_enabled", "1") == "1",
        "notify_new": stored.get("telegram_new", "1") == "1",
        "notify_result": stored.get("telegram_result", "1") == "1",
    }
    CACHE.update(at=time.time(), data=settings)
    return settings


async def save_settings(values):
    sg = _sg()
    await asyncio.to_thread(sg.db_exec, DDL)
    for key, value in values.items():
        await asyncio.to_thread(
            sg.db_exec,
            "INSERT INTO ai_settings (key, value) VALUES (:key, :value) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"key": key, "value": value},
        )
    CACHE["data"] = None


async def send(text, settings=None):
    cfg = settings or await load_settings()
    if not cfg["token"] or not cfg["chat_id"]:
        return False, "টোকেন বা চ্যাট আইডি দেওয়া নেই"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
                json={"chat_id": cfg["chat_id"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        if response.status_code == 200:
            return True, None
        detail = ""
        try:
            detail = str(response.json().get("description", ""))
        except Exception:
            pass
        return False, f"Telegram {response.status_code}: {detail}"[:200]
    except Exception as error:
        logger.warning("Telegram send failed (%s)", type(error).__name__)
        return False, "Telegram-এর সাথে সংযোগ হয়নি"


def _esc(value):
    return htmllib.escape(str(value))


def format_new(row):
    risk = abs(row["entry"] - row["stop_loss"])
    reward = abs(row["take_profit"] - row["entry"])
    rr = f"{reward / risk:.1f}" if risk else "-"
    icon = "🟢" if row["direction"] == "BUY" else "🔴"
    titles = {
        "ai": "✅ AI-অনুমোদিত সিগন্যাল",
        "starter": "🧪 স্টার্টার সিগন্যাল (AI অনুমোদিত)",
        "starter_noai": "🧪 স্টার্টার সিগন্যাল (AI যাচাই ছাড়া)",
        "stats_noai": "📈 যোগ্য কৌশলের সিগন্যাল (AI যাচাই ছাড়া)",
        "gemini": "🤖 Gemini AI সিগন্যাল",
    }
    title = titles.get(row.get("mode"), "✅ AI-অনুমোদিত সিগন্যাল")
    lines = [
        f"{icon} <b>{title}: {_esc(row['direction'])}</b> (XAU/USD · {_esc(row.get('timeframe', ''))})",
        f"কৌশল: <b>{_esc(row['strategy'])}</b>",
        f"এন্ট্রি: <code>{row['entry']:.2f}</code>",
        f"SL: <code>{row['stop_loss']:.2f}</code> · TP: <code>{row['take_profit']:.2f}</code>",
        f"ঝুঁকি-লাভ ১:{rr}" + (f" · AI আস্থা {_esc(row['ai_confidence'])}%" if row.get("ai_confidence") is not None else ""),
    ]
    record = []
    if row.get("live_sample"):
        record.append(f"লাইভ ল্যাব: জয় <b>{_esc(row.get('live_accuracy'))}%</b> ({_esc(row['live_sample'])} ট্রেড, গড় R {row.get('live_avg_r') or 0:+.2f})")
    if row.get("bt_accuracy") is not None:
        record.append(f"ব্যাকটেস্ট: জয় <b>{_esc(row['bt_accuracy'])}%</b>")
    elif row.get("accuracy") is not None:
        record.append(f"ব্যাকটেস্ট: জয় <b>{_esc(row['accuracy'])}%</b> ({_esc(row.get('win_sample') or 0)} ট্রেড)")
    if record:
        lines.append("📊 " + " · ".join(record))
    if row.get("session_text"):
        lines.append("🕒 " + _esc(row["session_text"]))
    if str(row.get("mode") or "").startswith("starter"):
        lines.append(f"⚠️ এই কৌশলের লাইভ ট্রেড এখনো {_esc(row.get('live_sample') or 0)}টি (১০টির কম), তাই ফলাফল অনিশ্চিত")
    if row.get("ai_reason"):
        lines.append("🤖 AI: " + _esc(str(row["ai_reason"])[:260]))
    elif row.get("mode") in ("starter_noai", "stats_noai"):
        lines.append("🤖 AI যাচাই হয়নি (Gemini লিমিট/ত্রুটি)")
    if row.get("risk_event"):
        lines.append("⚠️ কাছাকাছি বড় খবর: " + _esc(row["risk_event"]) + " — ঝুঁকি বেশি, সাবধান")
    lines.append("⚠️ শিক্ষামূলক বিশ্লেষণ, বিনিয়োগ পরামর্শ নয়")
    return "\n".join(lines)


def format_result(signal, outcome):
    risk = abs(signal["entry"] - signal["stop_loss"])
    reward = abs(signal["take_profit"] - signal["entry"])
    head = {"WIN": "✅ <b>টার্গেট (TP) হিট</b>", "LOSS": "❌ <b>স্টপ লস (SL) হিট</b>", "EXPIRED": "⏱ <b>সময় শেষ</b>"}.get(outcome, outcome)
    r_text = ""
    if risk:
        if outcome == "WIN":
            r_text = f" (+{reward / risk:.1f}R)"
        elif outcome == "LOSS":
            r_text = " (-1R)"
    return (
        f"{head}{r_text}\n"
        f"{_esc(signal['strategy'])} · {_esc(signal['direction'])} · {_esc(signal.get('timeframe', ''))}\n"
        f"এন্ট্রি <code>{signal['entry']:.2f}</code> → SL <code>{signal['stop_loss']:.2f}</code> / TP <code>{signal['take_profit']:.2f}</code>"
    )


async def notify_new(row):
    try:
        cfg = await load_settings()
        if cfg["enabled"] and cfg["notify_new"] and cfg["token"] and cfg["chat_id"]:
            await send(format_new(row), cfg)
    except Exception:
        logger.warning("notify_new failed")


async def notify_result(signal, outcome):
    try:
        cfg = await load_settings()
        if cfg["enabled"] and cfg["notify_result"] and cfg["token"] and cfg["chat_id"]:
            await send(format_result(signal, outcome), cfg)
    except Exception:
        logger.warning("notify_result failed")


def _check_admin(payload):
    required = os.getenv("ADMIN_KEY", "")
    if required and not hmac.compare_digest(str(payload.get("admin_key", "")), required):
        raise HTTPException(status_code=403, detail="অ্যাডমিন কী ভুল")


async def _payload(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        raise HTTPException(status_code=400, detail="অনুরোধ পড়া যায়নি")


@router.get("/api/ai/telegram")
async def telegram_status():
    cfg = await load_settings(force=True)
    return {
        "configured": bool(cfg["token"] and cfg["chat_id"]),
        "token_hint": ("…" + cfg["token"][-4:]) if cfg["token"] else "",
        "chat_id": cfg["chat_id"],
        "enabled": cfg["enabled"], "notify_new": cfg["notify_new"], "notify_result": cfg["notify_result"],
        "admin_protected": bool(os.getenv("ADMIN_KEY", "")),
    }


@router.post("/api/ai/telegram/save")
async def telegram_save(request: Request):
    payload = await _payload(request)
    _check_admin(payload)
    values = {}
    token = str(payload.get("token", "")).strip()
    chat_id = str(payload.get("chat_id", "")).strip()
    if token:
        if not TOKEN_RE.match(token):
            raise HTTPException(status_code=400, detail="টোকেনের ফরম্যাট ঠিক নয় (যেমন 123456789:AA...)")
        values["telegram_token"] = token
    if chat_id:
        if not CHAT_RE.match(chat_id):
            raise HTTPException(status_code=400, detail="চ্যাট আইডি ঠিক নয় (সংখ্যা, অথবা @চ্যানেলনাম)")
        values["telegram_chat_id"] = chat_id
    values["telegram_enabled"] = "1" if payload.get("enabled", True) else "0"
    values["telegram_new"] = "1" if payload.get("notify_new", True) else "0"
    values["telegram_result"] = "1" if payload.get("notify_result", True) else "0"
    await save_settings(values)
    return await telegram_status()


@router.post("/api/ai/telegram/test")
async def telegram_test(request: Request):
    payload = await _payload(request)
    _check_admin(payload)
    if time.time() - LAST_TEST["at"] < 10:
        raise HTTPException(status_code=429, detail="কয়েক সেকেন্ড পরে আবার চেষ্টা করুন")
    LAST_TEST["at"] = time.time()
    ok, error = await send("✅ GOLD AI LAB: টেলিগ্রাম সংযোগ ঠিক আছে। নতুন সিগন্যাল ও ফলাফল এখানে আসবে।", await load_settings(force=True))
    return {"ok": ok, "error": error}


@router.post("/api/ai/telegram/clear")
async def telegram_clear(request: Request):
    payload = await _payload(request)
    _check_admin(payload)
    await save_settings({"telegram_token": "", "telegram_chat_id": ""})
    return await telegram_status()
