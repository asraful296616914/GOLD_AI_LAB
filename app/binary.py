import os
import time
import json
import bisect
import asyncio
import logging
import html as htmllib
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import text

from app.database import engine
from app import backtest, notify

logger = logging.getLogger("gold-ai-lab.binary")
router = APIRouter()

PAIRS = [
    "EUR/JPY", "CAD/JPY", "GBP/JPY", "EUR/USD", "EUR/CAD", "USD/JPY",
    "AUD/CAD", "AUD/CHF", "EUR/AUD", "GBP/CAD", "GBP/AUD", "AUD/USD",
    "GBP/CHF", "CHF/JPY", "EUR/CHF", "USD/CAD", "USD/CHF", "EUR/GBP",
]
BASE_TF = "5"
BAR_SEC = 300
EXPIRIES = (1, 3)  # in M5 candles -> 5 and 15 minutes
WINDOW = 300
MIN_BARS = 60

MIN_WIN = float(os.getenv("BINARY_MIN_WIN", "60"))        # backtest win rate must be ABOVE this (%)
MIN_TRADES = int(os.getenv("BINARY_MIN_TRADES", "30"))    # minimum decided trades in the backtest
BT_DAYS = int(os.getenv("BINARY_BACKTEST_DAYS", "30"))
BT_HOURS = max(1.0, float(os.getenv("BINARY_BACKTEST_HOURS", "12")))
AUTO = os.getenv("BINARY_AUTO", "1") == "1"
REQUIRE_OOS = os.getenv("BINARY_REQUIRE_OOS", "0") == "1"  # also require the last 30% of trades to be above MIN_WIN
TZ_OFFSET_H = 6  # Bangladesh time, used only for display

# Trading window in UTC. Asia session (and the quiet Sydney hours) are skipped:
# London 07-16, New York 12-21 -> trade only 07:00-21:00 UTC, Monday to Friday.
_start, _end = os.getenv("BINARY_SESSION_UTC", "7-21").split("-")
SESSION_START, SESSION_END = int(_start), int(_end)

DDL = [
    "CREATE TABLE IF NOT EXISTS binary_backtest (id BIGSERIAL PRIMARY KEY, created_at BIGINT NOT NULL, result TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS binary_signals ("
    "id BIGSERIAL PRIMARY KEY, created_at BIGINT NOT NULL, pair TEXT NOT NULL, strategy TEXT NOT NULL, "
    "direction TEXT NOT NULL, expiry_min INTEGER NOT NULL, candle_time BIGINT NOT NULL, entry_time BIGINT NOT NULL, "
    "expire_time BIGINT NOT NULL, entry_price DOUBLE PRECISION NOT NULL, exit_price DOUBLE PRECISION, "
    "outcome TEXT NOT NULL DEFAULT 'OPEN', resolved_at BIGINT, bt_win DOUBLE PRECISION, bt_trades INTEGER, "
    "oos_win DOUBLE PRECISION, session TEXT, telegram_sent BOOLEAN DEFAULT FALSE)",
    "CREATE UNIQUE INDEX IF NOT EXISTS binary_signals_pair_candle ON binary_signals (pair, candle_time)",
]

STATE = {"status": "idle", "progress": 0, "pair": None, "started_at": None, "finished_at": None, "error": None}
SCAN = {"last_run": None, "last_result": None, "last_error": None}
APPROVED = {}   # (pair, strategy, expiry_min) -> backtest row
LAST_RESULT = {"data": None}
SYMBOLS = {}
SCAN_LOCK = asyncio.Lock()


def _m():
    from app import main as m
    return m


def _sg():
    from app import signals as sg
    return sg


def db_rows(sql, params=None):
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.execute(text(sql), params or {})]


def db_exec(sql, params=None):
    with engine.begin() as connection:
        return connection.execute(text(sql), params or {})


def ensure_tables():
    with engine.begin() as connection:
        for statement in DDL:
            connection.execute(text(statement))


# ---------------------------------------------------------------- sessions
def _utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def session_allowed(ts):
    """Weekdays, inside the London/New York window; the Asia session is skipped."""
    dt = _utc(ts)
    return dt.weekday() < 5 and SESSION_START <= dt.hour < SESSION_END


def session_name(ts):
    hour = _utc(ts).hour
    if hour < SESSION_START or hour >= SESSION_END:
        return "Asia (বাদ)"
    if hour < 12:
        return "London"
    if hour < 16:
        return "London + New York"
    return "New York"


def bd_time(ts):
    return (_utc(ts) + timedelta(hours=TZ_OFFSET_H)).strftime("%H:%M")


def decimals(pair):
    return 3 if pair.endswith("JPY") else 5


# ---------------------------------------------------------------- data
FEEDS = {}        # pair -> {"id": pyth_lazer_id, "channel": min_channel}  (only stable feeds)
CATALOG = {"at": 0.0, "try": 0.0, "data": {}}   # pair -> {"state", "channel", "id", "symbol"} from Pyth's public list
NOTES = {"coming_soon": "Pyth-এ এই ফিড এখনো চালু হয়নি (coming soon)"}


async def load_catalog(m):
    """One public /symbols call for all FX pairs (no per-pair calls, so no rate-limit trouble)."""
    if CATALOG["data"] and time.time() - CATALOG["at"] < 3600:
        return CATALOG["data"]
    if time.time() - CATALOG["try"] < 20:
        return CATALOG["data"]
    CATALOG["try"] = time.time()
    try:
        response = await m.pyth_get("/symbols", params={"asset_type": "fx"}, timeout=30)
        data = response.json() if response.status_code == 200 else []
    except Exception:
        logger.warning("Could not load Pyth FX symbol list")
        return CATALOG["data"]
    by_symbol = {i.get("symbol"): i for i in data if isinstance(i, dict)}
    out = {}
    for pair in PAIRS:
        item = by_symbol.get(f"FX.{pair}")
        if item:
            out[pair] = {
                "state": item.get("state"), "channel": item.get("min_channel") or m.PYTH_CHANNEL,
                "id": item.get("pyth_lazer_id"), "symbol": item["symbol"],
            }
    if out:
        CATALOG.update(at=time.time(), data=out)
    return CATALOG["data"]


def feed_state(pair):
    """'stable' / 'coming_soon' / ... from Pyth's list, or None when the list is not loaded yet."""
    info = CATALOG["data"].get(pair)
    if info:
        return info["state"]
    return "unlisted" if CATALOG["data"] else None


def feed_ok(pair):
    return feed_state(pair) in (None, "stable")   # unknown -> still try


async def resolve_symbols(m):
    """Pyth symbol + live feed id per pair, taken from the public list. Only 'stable' feeds get a live stream."""
    catalog = await load_catalog(m)
    for pair in PAIRS:
        info = catalog.get(pair)
        SYMBOLS[pair] = info["symbol"] if info else f"FX.{pair}"
        if info and info["state"] == "stable" and info["id"] is not None:
            FEEDS[pair] = {"id": int(info["id"]), "channel": info["channel"]}
        else:
            FEEDS.pop(pair, None)
    return SYMBOLS


def _reason(error):
    status = getattr(error, "status_code", None)
    if status == 403:
        return "Pyth 403: আপনার API কী-তে এই ফিডের অ্যাক্সেস নেই"
    if status == 429:
        return "Pyth 429: রেট লিমিট"
    if status == 401:
        return "Pyth 401: API কী ঠিক নেই"
    detail = getattr(error, "detail", None)
    if isinstance(detail, dict):
        detail = detail.get("message")
    return (f"Pyth {status}: " if status else "") + str(detail or type(error).__name__)[:90]


async def probe(m, symbol, resolution, start, end):
    """Like backtest.fetch_range, but also says WHY there is no data. Returns (candles, reason)."""
    endpoint = f"/{m.PYTH_CHANNEL}/history"
    try:
        response = await m.pyth_get(
            endpoint, params={"symbol": symbol, "from": start, "to": end, "resolution": resolution},
            headers=m.get_pyth_headers(), timeout=30,
        )
    except Exception as error:
        return [], _reason(error)
    if response.status_code != 200:
        return [], _reason(type("E", (), {"status_code": response.status_code, "detail": response.text[:90]})())
    try:
        data = response.json()
    except ValueError:
        return [], "Pyth থেকে ভুল JSON এসেছে"
    if not isinstance(data, dict) or data.get("s") not in ("ok", "no_data"):
        note = data.get("errmsg") if isinstance(data, dict) else None
        return [], "Pyth: " + str(note or (data.get("s") if isinstance(data, dict) else "ভুল রেসপন্স"))[:90]
    t, o, h, l, c = (data.get(key) or [] for key in ("t", "o", "h", "l", "c"))
    if len({len(t), len(o), len(h), len(l), len(c)}) != 1:
        return [], "Pyth থেকে অসম্পূর্ণ ক্যান্ডেল এসেছে"
    candles = [
        {"time": int(t[i]), "open": float(o[i]), "high": float(h[i]), "low": float(l[i]), "close": float(c[i])}
        for i in range(len(t))
    ]
    return candles, ("ok" if candles else "no_data")


def gather(m, sg, window, htf_window, atr):
    """All strategies of the gold lab, reused here only for their BUY/SELL direction."""
    from app import smc, classic, classic2, patterns

    found = []
    for source in (
        lambda: smc.analyze(window, atr, htf_window)["signals"],
        lambda: classic.analyze(m, window, atr),
        lambda: classic2.analyze(m, window, atr),
        lambda: patterns.analyze(m, window, atr),
    ):
        try:
            found.extend(source())
        except Exception:
            pass
    try:
        rule = sg.rules_signal(m, window)
        if rule and rule.get("direction") in ("BUY", "SELL"):
            found.append({"strategy": "RULES_V1", "direction": rule["direction"]})
    except Exception:
        pass
    out = {}
    for item in found:
        if item.get("direction") in ("BUY", "SELL") and item.get("strategy") and item["strategy"] not in out:
            out[item["strategy"]] = item["direction"]
    return out


# ---------------------------------------------------------------- backtest
def simulate_pair(m, sg, base, htf):
    """Binary rule: enter at the close of the signal candle, win if price is on the right side at expiry."""
    results = defaultdict(list)
    htf_times = [c["time"] for c in htf]
    cooldown = max(EXPIRIES)
    last_i = {}
    n = len(base)
    for i in range(MIN_BARS, n - max(EXPIRIES)):
        bar = base[i]
        if not session_allowed(bar["time"]):
            continue
        window = base[max(0, i - WINDOW + 1): i + 1]
        atr = m.calculate_atr(window)
        if not atr:
            continue
        htf_end = bisect.bisect_right(htf_times, bar["time"] - (3600 - BAR_SEC))
        htf_window = htf[max(0, htf_end - 160): htf_end] if htf else None
        for strategy, direction in gather(m, sg, window, htf_window, atr).items():
            if i - last_i.get(strategy, -999) < cooldown:
                continue
            last_i[strategy] = i
            sign = 1 if direction == "BUY" else -1
            for k in EXPIRIES:
                exit_bar = base[i + k]
                if exit_bar["time"] - bar["time"] > k * BAR_SEC:
                    continue  # data gap (weekend / missing candles)
                diff = exit_bar["close"] - bar["close"]
                outcome = 0 if diff == 0 else (1 if diff * sign > 0 else -1)
                results[(strategy, k)].append((bar["time"], outcome))
        if i % 40 == 0:
            time.sleep(0)
    return results


def summarize(pair, results):
    rows = []
    for (strategy, k), items in results.items():
        decided = [x for x in items if x[1] != 0]
        trades = len(decided)
        if trades < 10:
            continue
        wins = sum(1 for x in decided if x[1] > 0)
        win_rate = round(wins / trades * 100, 1)
        tail = decided[int(trades * 0.7):]
        oos_win = round(sum(1 for x in tail if x[1] > 0) / len(tail) * 100, 1) if tail else None
        approved = trades >= MIN_TRADES and win_rate > MIN_WIN
        if approved and REQUIRE_OOS and (oos_win is None or oos_win <= MIN_WIN):
            approved = False
        rows.append({
            "pair": pair, "strategy": strategy, "expiry_min": k * 5, "trades": trades, "wins": wins,
            "losses": trades - wins, "draws": len(items) - trades, "win_rate": win_rate,
            "oos_trades": len(tail), "oos_win": oos_win, "approved": approved,
        })
    return rows


def load_approved(result):
    APPROVED.clear()
    if not result:
        return
    for row in result.get("rows", []):
        if row.get("approved"):
            APPROVED[(row["pair"], row["strategy"], row["expiry_min"])] = row


async def latest_result():
    rows = await asyncio.to_thread(db_rows, "SELECT created_at, result FROM binary_backtest ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    try:
        data = json.loads(rows[0]["result"])
        data["created_at"] = int(rows[0]["created_at"])
        return data
    except Exception:
        return None


async def run_job(days=BT_DAYS):
    if STATE["status"] == "running":
        return {"status": "busy"}
    STATE.update(status="running", progress=0, pair=None, started_at=int(time.time()), error=None)
    try:
        m, sg = _m(), _sg()
        await asyncio.to_thread(ensure_tables)
        symbols = await resolve_symbols(m)
        rows, per_pair = [], []
        for index, pair in enumerate(PAIRS):
            STATE["pair"] = pair
            if not feed_ok(pair):
                per_pair.append({"pair": pair, "error": NOTES["coming_soon"]})
                STATE["progress"] = round((index + 1) / len(PAIRS) * 100)
                continue
            try:
                base = await backtest.load_history(m, symbols[pair], BASE_TF, days)
                try:
                    htf = await backtest.load_history(m, symbols[pair], "60", days + 3)
                except Exception:
                    htf = []
                if len(base) < 200:
                    _, why = await probe(m, symbols[pair], BASE_TF, int(time.time()) - 3 * 86400, int(time.time()))
                    per_pair.append({"pair": pair, "error": f"ক্যান্ডেল কম ({len(base)}টি)" + ("" if why in ("ok", "no_data") else " · " + why), "bars": len(base)})
                    continue
                results = await asyncio.to_thread(simulate_pair, m, sg, base, htf)
                pair_rows = summarize(pair, results)
                rows.extend(pair_rows)
                per_pair.append({"pair": pair, "bars": len(base), "approved": sum(1 for r in pair_rows if r["approved"])})
            except Exception as error:
                logger.exception("Binary backtest failed for %s", pair)
                per_pair.append({"pair": pair, "error": _reason(error)})
            STATE["progress"] = round((index + 1) / len(PAIRS) * 100)
        result = {
            "days": days, "timeframe": "M5", "min_win": MIN_WIN, "min_trades": MIN_TRADES,
            "session": f"{SESSION_START:02d}:00-{SESSION_END:02d}:00 UTC (Asia বাদ)",
            "rows": rows, "pairs": per_pair, "approved": sum(1 for r in rows if r["approved"]),
        }
        if not any("bars" in p and "error" not in p for p in per_pair):
            raise RuntimeError("কোনো পেয়ারের হিস্ট্রি আনা যায়নি (PYTH_API_KEY বা সিম্বল চেক করুন)")
        await asyncio.to_thread(
            db_exec, "INSERT INTO binary_backtest (created_at, result) VALUES (:t, :r)",
            {"t": int(time.time()), "r": json.dumps(result)},
        )
        result["created_at"] = int(time.time())
        LAST_RESULT["data"] = result
        load_approved(result)
        STATE.update(status="done", progress=100, pair=None, finished_at=int(time.time()))
        logger.info("Binary backtest done: %s approved", result["approved"])
        return {"status": "done", "approved": result["approved"]}
    except Exception as error:
        logger.exception("Binary backtest failed")
        STATE.update(status="error", error=str(error)[:200], finished_at=int(time.time()))
        return {"status": "error", "error": STATE["error"]}


# ---------------------------------------------------------------- telegram
def _esc(value):
    return htmllib.escape(str(value))


def format_signal(row):
    up = row["direction"] == "BUY"
    d = decimals(row["pair"])
    lines = [
        f"{'🟢' if up else '🔴'} <b>বাইনারি সিগন্যাল: {'CALL ⬆️' if up else 'PUT ⬇️'}</b>",
        f"📌 পেয়ার: <b>{_esc(row['pair'])}</b> · M5",
        f"⏱ এন্ট্রি: <b>{bd_time(row['entry_time'])}</b> (বাংলাদেশ সময়) · এক্সপায়ারি: <b>{row['expiry_min']} মিনিট</b>",
        f"💰 ক্যান্ডেল ক্লোজ: <code>{row['entry_price']:.{d}f}</code>",
        f"🧠 কৌশল: <b>{_esc(row['strategy'])}</b>",
        f"📊 ব্যাকটেস্ট: জয় <b>{row['bt_win']}%</b> ({row['bt_trades']} ট্রেড)"
        + (f" · শেষ অংশে {row['oos_win']}%" if row.get("oos_win") is not None else ""),
        f"🕒 সেশন: {_esc(row['session'])}",
        "⚠️ ব্যাকটেস্ট ভবিষ্যতের গ্যারান্টি নয়। শুধু ঝুঁকি নিতে পারেন এমন অংকে ট্রেড করুন।",
    ]
    return "\n".join(lines)


def format_result(row, outcome):
    head = {"WIN": "✅ <b>জয়</b>", "LOSS": "❌ <b>হার</b>", "DRAW": "➖ <b>ড্র (সমান)</b>"}.get(outcome, outcome)
    d = decimals(row["pair"])
    return (
        f"{head} · {_esc(row['pair'])} · {'CALL' if row['direction'] == 'BUY' else 'PUT'} · {row['expiry_min']} মিনিট\n"
        f"{_esc(row['strategy'])}\n"
        f"এন্ট্রি <code>{row['entry_price']:.{d}f}</code> → এক্সপায়ারি <code>{row['exit_price']:.{d}f}</code>"
    )


async def tg_send(text_value, kind):
    try:
        cfg = await notify.load_settings()
        if not (cfg["enabled"] and cfg["token"] and cfg["chat_id"]):
            return False
        if kind == "new" and not cfg["notify_new"]:
            return False
        if kind == "result" and not cfg["notify_result"]:
            return False
        ok, error = await notify.send(text_value, cfg)
        if not ok:
            logger.warning("Binary telegram failed: %s", error)
        return ok
    except Exception:
        logger.warning("Binary telegram error")
        return False


# ---------------------------------------------------------------- live scan
def insert_signal(row):
    result = db_exec(
        "INSERT INTO binary_signals (created_at, pair, strategy, direction, expiry_min, candle_time, entry_time, expire_time, "
        "entry_price, bt_win, bt_trades, oos_win, session) VALUES (:created_at, :pair, :strategy, :direction, :expiry_min, "
        ":candle_time, :entry_time, :expire_time, :entry_price, :bt_win, :bt_trades, :oos_win, :session) "
        "ON CONFLICT (pair, candle_time) DO NOTHING",
        row,
    )
    return result.rowcount == 1


async def scan_pair(m, sg, pair, symbol, now):
    options = {k: v for k, v in APPROVED.items() if k[0] == pair}
    if not options:
        return None
    candles = await backtest.fetch_range(m, symbol, BASE_TF, now - 450 * BAR_SEC, now)
    candles = [c for c in candles if c["time"] + BAR_SEC <= now]
    candles.sort(key=lambda c: c["time"])
    if len(candles) < 100:
        return None
    last = candles[-1]
    close_time = last["time"] + BAR_SEC
    if now - close_time > 150 or not session_allowed(close_time):
        return None  # market closed / stale data / outside the trading window
    try:
        htf = await backtest.fetch_range(m, symbol, "60", now - 200 * 3600, now)
        htf = [c for c in htf if c["time"] + 3600 <= close_time]
    except Exception:
        htf = []
    window = candles[-WINDOW:]
    atr = m.calculate_atr(window)
    if not atr:
        return None
    found = gather(m, sg, window, htf[-160:] if htf else None, atr)
    picks = []
    for strategy, direction in found.items():
        for k in EXPIRIES:
            row = options.get((pair, strategy, k * 5))
            if row:
                picks.append((row["win_rate"], row, direction, k))
    if not picks:
        return None
    if len({p[2] for p in picks}) > 1:
        return {"pair": pair, "skipped": "BUY/SELL দ্বন্দ্ব"}
    picks.sort(key=lambda p: (p[0], p[1]["trades"]), reverse=True)
    _, row, direction, k = picks[0]
    return {
        "created_at": int(now), "pair": pair, "strategy": row["strategy"], "direction": direction,
        "expiry_min": k * 5, "candle_time": last["time"], "entry_time": close_time,
        "expire_time": close_time + k * BAR_SEC, "entry_price": last["close"],
        "bt_win": row["win_rate"], "bt_trades": row["trades"], "oos_win": row.get("oos_win"),
        "session": session_name(close_time),
    }


async def scan_once():
    if SCAN_LOCK.locked():
        return {"status": "busy"}
    async with SCAN_LOCK:
        now = int(time.time())
        SCAN["last_run"] = now
        if not session_allowed(now):
            SCAN["last_result"] = {"status": "outside_session"}
            return SCAN["last_result"]
        if not APPROVED:
            SCAN["last_result"] = {"status": "no_approved"}
            return SCAN["last_result"]
        m, sg = _m(), _sg()
        try:
            symbols = await resolve_symbols(m)
            sent, found = 0, 0
            for pair in sorted({k[0] for k in APPROVED}):
                try:
                    row = await scan_pair(m, sg, pair, symbols[pair], now)
                except Exception:
                    logger.warning("Binary scan failed for %s", pair)
                    continue
                if not row or "skipped" in row:
                    continue
                inserted = await asyncio.to_thread(insert_signal, row)
                if not inserted:
                    continue
                found += 1
                if await tg_send(format_signal(row), "new"):
                    sent += 1
                    await asyncio.to_thread(
                        db_exec, "UPDATE binary_signals SET telegram_sent = TRUE WHERE pair = :p AND candle_time = :c",
                        {"p": pair, "c": row["candle_time"]},
                    )
                await asyncio.sleep(0.2)
            SCAN["last_result"] = {"status": "ok", "new_signals": found, "telegram_sent": sent}
            SCAN["last_error"] = None
        except Exception as error:
            logger.exception("Binary scan failed")
            SCAN["last_error"] = str(error)[:200]
            SCAN["last_result"] = {"status": "error"}
        return SCAN["last_result"]


async def resolve_open():
    now = int(time.time())
    rows = await asyncio.to_thread(
        db_rows, "SELECT * FROM binary_signals WHERE outcome = 'OPEN' AND expire_time <= :now ORDER BY id LIMIT 60", {"now": now - 8}
    )
    if not rows:
        return 0
    m = _m()
    symbols = await resolve_symbols(m)
    done = 0
    for row in rows:
        try:
            candles = await backtest.fetch_range(m, symbols[row["pair"]], BASE_TF, row["entry_time"] - BAR_SEC, row["expire_time"] + BAR_SEC)
            target = row["expire_time"] - BAR_SEC
            exit_bar = next((c for c in candles if c["time"] == target), None)
            if exit_bar is None:
                if now - row["expire_time"] > 1800:
                    await asyncio.to_thread(
                        db_exec, "UPDATE binary_signals SET outcome = 'VOID', resolved_at = :t WHERE id = :id", {"t": now, "id": row["id"]}
                    )
                continue
            diff = exit_bar["close"] - row["entry_price"]
            sign = 1 if row["direction"] == "BUY" else -1
            outcome = "DRAW" if diff == 0 else ("WIN" if diff * sign > 0 else "LOSS")
            await asyncio.to_thread(
                db_exec, "UPDATE binary_signals SET outcome = :o, exit_price = :x, resolved_at = :t WHERE id = :id",
                {"o": outcome, "x": exit_bar["close"], "t": now, "id": row["id"]},
            )
            done += 1
            if row.get("telegram_sent"):
                await tg_send(format_result({**row, "exit_price": exit_bar["close"]}, outcome), "result")
        except Exception:
            logger.warning("Binary resolve failed for id=%s", row.get("id"))
    return done


# ---------------------------------------------------------------- background loops
async def backtest_loop():
    await asyncio.sleep(60)
    while True:
        try:
            age = int(time.time()) - LAST_RESULT["data"]["created_at"] if LAST_RESULT["data"] else None
            if STATE["status"] != "running" and (age is None or age >= BT_HOURS * 3600):
                logger.info("Binary auto backtest started")
                await run_job(BT_DAYS)
                if STATE["status"] == "error":
                    await asyncio.sleep(1800)
        except Exception:
            logger.exception("Binary backtest loop failed")
        await asyncio.sleep(600)


async def scan_loop():
    await asyncio.sleep(30)
    while True:
        now = time.time()
        await asyncio.sleep(max(1.0, (now // BAR_SEC + 1) * BAR_SEC + 8 - now))
        try:
            await resolve_open()
            await scan_once()
        except Exception:
            logger.exception("Binary scan loop failed")


async def start_background():
    try:
        await asyncio.to_thread(ensure_tables)
        LAST_RESULT["data"] = await latest_result()
        load_approved(LAST_RESULT["data"])
    except Exception:
        logger.exception("Could not prepare binary tables")
        return
    asyncio.create_task(live_loop())
    if AUTO:
        asyncio.create_task(backtest_loop())
        asyncio.create_task(scan_loop())


# ---------------------------------------------------------------- chart + market watch
CANDLE_CACHE = {}
MARKET_CACHE = {"at": 0.0, "data": None}


async def chart_candles(pair, bars=300):
    cached = CANDLE_CACHE.get(pair)
    if cached and time.time() - cached[0] < 20:
        return cached[1]
    m = _m()
    await resolve_symbols(m)
    if not feed_ok(pair):
        raise HTTPException(status_code=404, detail=NOTES["coming_soon"])
    symbol = SYMBOLS[pair]
    end = int(time.time())
    merged, reason = {}, "no_data"
    for _ in range(4):  # step back over weekend gaps until there are enough candles
        start = end - bars * BAR_SEC
        candles, reason = await probe(m, symbol, BASE_TF, start, end)
        for c in candles:
            merged[c["time"]] = c
        if len(merged) >= min(bars, 120) or reason not in ("ok", "no_data"):
            break
        end = start - 1
    if not merged:
        raise HTTPException(status_code=404, detail="ডেটা নেই — " + ("মার্কেট বন্ধ বা ডেটা আসেনি" if reason in ("ok", "no_data") else reason))
    candles = [merged[t] for t in sorted(merged)][-bars:]
    CANDLE_CACHE[pair] = (time.time(), candles)
    return candles


async def market_overview():
    if MARKET_CACHE["data"] is not None and time.time() - MARKET_CACHE["at"] < 30:
        return MARKET_CACHE["data"]
    m = _m()
    symbols = await resolve_symbols(m)
    gate = asyncio.Semaphore(3)
    now = int(time.time())

    async def one(pair):
        if not feed_ok(pair):
            return {"pair": pair, "price": None, "reason": "coming_soon", "note": NOTES["coming_soon"]}
        async with gate:
            candles, reason = await probe(m, symbols[pair], BASE_TF, now - 40 * BAR_SEC, now)
            if "429" in reason:           # one polite retry
                await asyncio.sleep(1.5)
                candles, reason = await probe(m, symbols[pair], BASE_TF, now - 40 * BAR_SEC, now)
            if not candles:
                weekend = reason in ("ok", "no_data")
                return {"pair": pair, "price": None, "reason": "no_data" if weekend else "error",
                        "note": "এই মুহূর্তে ক্যান্ডেল নেই (মার্কেট বন্ধ?)" if weekend else reason}
            candles.sort(key=lambda c: c["time"])
            last = candles[-1]
            ref = candles[-13] if len(candles) >= 13 else candles[0]
            return {
                "pair": pair, "price": last["close"], "time": last["time"], "ref_price": ref["close"],
                "change_pct": round((last["close"] - ref["close"]) / ref["close"] * 100, 3) if ref["close"] else None,
                "open": now - (last["time"] + BAR_SEC) < 900,
            }

    rows = await asyncio.gather(*(one(p) for p in PAIRS))
    for row in rows:
        row["approved"] = sum(1 for k in APPROVED if k[0] == row["pair"])
        row["decimals"] = decimals(row["pair"])
    MARKET_CACHE.update(at=time.time(), data=rows)
    return rows


# ---------------------------------------------------------------- live price stream (all 18 pairs, one websocket)
LIVE = {}   # pair -> {"price", "ts", "recv"}
LIVE_STATE = {"status": "idle", "error": None, "since": None}
LIVE_IDLE_S = 60  # reconnect (next Pyth endpoint) when the stream is silent this long


async def live_loop():
    import websockets

    api_key = os.getenv("PYTH_API_KEY", "").strip()
    if not api_key:
        LIVE_STATE.update(status="no_key", error="PYTH_API_KEY নেই")
        return
    m = _m()
    index = 0
    while True:
        try:
            await resolve_symbols(m)
            by_channel, id_to_pair = {}, {}
            for pair in PAIRS:
                feed = FEEDS.get(pair)
                if feed:
                    by_channel.setdefault(feed["channel"], []).append(feed["id"])
                    id_to_pair[feed["id"]] = pair
            if not by_channel:
                LIVE_STATE.update(status="no_feeds", error="Pyth ফিড আইডি পাওয়া যায়নি")
                await asyncio.sleep(60)
                continue
            url = m.PYTH_WS_URLS[index % len(m.PYTH_WS_URLS)]
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {api_key}"},
                ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2**21,
            ) as ws:
                for number, (channel, ids) in enumerate(by_channel.items(), 1):
                    await ws.send(json.dumps({
                        "type": "subscribe", "subscriptionId": number, "priceFeedIds": ids,
                        "properties": ["price", "exponent", "feedUpdateTimestamp"], "formats": [],
                        "channel": channel, "ignoreInvalidFeeds": True, "deliveryFormat": "json", "parsed": True,
                    }))
                LIVE_STATE.update(status="connected", error=None, since=time.time())
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=LIVE_IDLE_S)
                    except asyncio.TimeoutError:
                        raise ConnectionError(f"no data for {LIVE_IDLE_S}s")
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    kind = message.get("type")
                    if kind == "subscriptionError":
                        LIVE_STATE["error"] = str(message)[:300]
                        continue
                    if kind != "streamUpdated":
                        continue
                    parsed = message.get("parsed") or {}
                    ts = (int(parsed.get("timestampUs") or 0) / 1e6) or time.time()
                    for feed in parsed.get("priceFeeds") or []:
                        try:
                            pair = id_to_pair.get(int(feed.get("priceFeedId", -1)))
                            price, exponent = feed.get("price"), feed.get("exponent")
                            if pair is None or price is None or exponent is None:
                                continue
                            LIVE[pair] = {"price": float(price) * (10 ** int(exponent)), "ts": ts, "recv": time.time()}
                        except (TypeError, ValueError):
                            continue
                    LIVE_STATE["status"] = "streaming"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LIVE_STATE.update(status="reconnecting", error=type(error).__name__)
            logger.warning("Binary live stream disconnected (%s)", type(error).__name__)
            index += 1
            await asyncio.sleep(3)


# ---------------------------------------------------------------- API
async def _admin(request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    notify._check_admin(payload if isinstance(payload, dict) else {})


@router.get("/binary", include_in_schema=False)
def binary_page():
    m = _m()
    return FileResponse(m.FRONTEND_DIR / "binary.html", headers={"Cache-Control": "no-store"})


@router.get("/api/binary/status")
async def binary_status():
    now = int(time.time())
    try:
        await load_catalog(_m())
    except Exception:
        pass
    data = LAST_RESULT["data"]
    return {
        "pairs": PAIRS, "expiries_min": [k * 5 for k in EXPIRIES], "min_win": MIN_WIN, "min_trades": MIN_TRADES,
        "session_window_utc": f"{SESSION_START:02d}:00-{SESSION_END:02d}:00",
        "session_open": session_allowed(now), "session_now": session_name(now),
        "auto": AUTO, "backtest": dict(STATE),
        "backtest_age_s": (now - data["created_at"]) if data else None,
        "approved_count": len(APPROVED), "approved_pairs": sorted({k[0] for k in APPROVED}),
        "scan": dict(SCAN), "live": {"status": LIVE_STATE["status"], "error": LIVE_STATE["error"]},
        "unsupported": [p for p in PAIRS if not feed_ok(p)],
    }


@router.get("/api/binary/backtest")
async def binary_backtest():
    return {"state": dict(STATE), "result": LAST_RESULT["data"]}


@router.post("/api/binary/backtest/run")
async def binary_backtest_run(request: Request, days: int = Query(BT_DAYS, ge=7, le=45)):
    await _admin(request)
    if STATE["status"] == "running":
        return {"status": "busy"}
    asyncio.create_task(run_job(days))
    return {"status": "started", "days": days}


@router.post("/api/binary/scan")
async def binary_scan(request: Request):
    await _admin(request)
    await resolve_open()
    return await scan_once()


@router.get("/api/binary/signals")
async def binary_signals(limit: int = Query(50, ge=1, le=200), pair: str = Query("")):
    if pair and pair not in PAIRS:
        raise HTTPException(status_code=400, detail="অজানা পেয়ার")
    where, params = ("WHERE pair = :pair", {"pair": pair, "limit": limit}) if pair else ("", {"limit": limit})
    rows = await asyncio.to_thread(db_rows, f"SELECT * FROM binary_signals {where} ORDER BY id DESC LIMIT :limit", params)
    return {"signals": rows}


@router.get("/api/binary/stats")
async def binary_stats():
    rows = await asyncio.to_thread(
        db_rows,
        "SELECT pair, outcome, COUNT(*) AS n FROM binary_signals GROUP BY pair, outcome",
    )
    total = {"WIN": 0, "LOSS": 0, "DRAW": 0, "OPEN": 0, "VOID": 0}
    per_pair = {}
    for row in rows:
        total[row["outcome"]] = total.get(row["outcome"], 0) + int(row["n"])
        slot = per_pair.setdefault(row["pair"], {"WIN": 0, "LOSS": 0, "DRAW": 0, "OPEN": 0, "VOID": 0})
        slot[row["outcome"]] = slot.get(row["outcome"], 0) + int(row["n"])
    decided = total["WIN"] + total["LOSS"]
    for slot in per_pair.values():
        d = slot["WIN"] + slot["LOSS"]
        slot["win_rate"] = round(slot["WIN"] / d * 100, 1) if d else None
    return {"total": total, "win_rate": round(total["WIN"] / decided * 100, 1) if decided else None, "per_pair": per_pair}


@router.get("/api/binary/candles")
async def binary_candles(pair: str = Query("EUR/USD"), bars: int = Query(300, ge=60, le=450)):
    if pair not in PAIRS:
        raise HTTPException(status_code=400, detail="অজানা পেয়ার")
    try:
        candles = await chart_candles(pair, bars)
    except HTTPException:
        raise
    except Exception as error:
        logger.warning("Binary candles failed for %s (%s)", pair, type(error).__name__)
        raise HTTPException(status_code=502, detail="চার্টের ডেটা আনা যায়নি")
    return {"pair": pair, "decimals": decimals(pair), "candles": candles}


@router.get("/api/binary/market")
async def binary_market():
    try:
        return {"markets": await market_overview(), "session_open": session_allowed(int(time.time()))}
    except Exception as error:
        logger.warning("Binary market failed (%s)", type(error).__name__)
        raise HTTPException(status_code=502, detail="মার্কেটের ডেটা আনা যায়নি")


@router.get("/api/binary/live")
async def binary_live():
    now = time.time()
    return {
        "status": LIVE_STATE["status"], "error": LIVE_STATE["error"], "now": int(now),
        "feeds": len(FEEDS), "missing": [p for p in PAIRS if p not in FEEDS],
        "prices": {pair: {"price": v["price"], "ts": v["ts"], "age": round(now - v["recv"], 1)} for pair, v in LIVE.items()},
    }
