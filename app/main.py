import os
import time
import json
import asyncio
import logging
from pathlib import Path
from collections import deque

import httpx
import websockets

from fastapi import (
    FastAPI, HTTPException, Query,
    WebSocket, WebSocketDisconnect
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine


# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

PYTH_API_URL = "https://pyth.dourolabs.app/v1"
PYTH_CHANNEL = "fixed_rate@200ms"
GOLD_SYMBOL = "Metal.XAU/USD"

PYTH_WS_URLS = [
    "wss://pyth-lazer-0.dourolabs.app/v1/stream",
    "wss://pyth-lazer-1.dourolabs.app/v1/stream",
    "wss://pyth-lazer-2.dourolabs.app/v1/stream",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold-ai-lab")

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only live market chart platform",
    version="2.1.0"
)

app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR)),
    name="static"
)

TABLES = [
    "market_prices",
    "market_sources",
    "news_articles",
    "economic_events",
    "system_logs"
]

RESOLUTIONS = {"1", "5", "15", "60", "240"}


# ==================================================
# PROCESS-LOCAL PYTH STREAM DIAGNOSTICS
# ==================================================

def new_endpoint_stats(endpoint):
    return {
        "endpoint": endpoint,
        "status": "idle",
        "subscription_status": "not_started",
        "connected_at": None,
        "last_message_at": None,
        "last_price_change_at": None,
        "messages_received": 0,
        "price_changes_total": 0,
        "last_price_key": None,
        "last_price": None,
        "last_stream_timestamp_us": None,
        "last_feed_update_timestamp": None,
        "last_error": None,
        "message_times": deque(),
        "price_change_times": deque(),
    }


PYTH_DIAGNOSTICS = {
    endpoint: new_endpoint_stats(endpoint)
    for endpoint in PYTH_WS_URLS
}


def _trim_times(values, now):
    while values and now - values[0] > 60:
        values.popleft()


def record_pyth_message(endpoint):
    stats = PYTH_DIAGNOSTICS[endpoint]
    now = time.time()
    stats["last_message_at"] = now
    stats["messages_received"] += 1
    stats["message_times"].append(now)
    _trim_times(stats["message_times"], now)


def record_pyth_price(endpoint, price, exponent, stream_timestamp, feed_timestamp):
    stats = PYTH_DIAGNOSTICS[endpoint]
    now = time.time()
    price_key = (str(price), str(exponent))

    if price_key != stats["last_price_key"]:
        stats["last_price_key"] = price_key
        stats["last_price"] = price_key
        stats["last_price_change_at"] = now
        stats["price_changes_total"] += 1
        stats["price_change_times"].append(now)

    _trim_times(stats["price_change_times"], now)
    stats["last_stream_timestamp_us"] = stream_timestamp
    stats["last_feed_update_timestamp"] = feed_timestamp


def get_pyth_diagnostics():
    now = time.time()
    endpoints = []

    for stats in PYTH_DIAGNOSTICS.values():
        _trim_times(stats["message_times"], now)
        _trim_times(stats["price_change_times"], now)

        last_message = stats["last_message_at"]
        last_feed_timestamp = stats["last_feed_update_timestamp"]

        # Pyth feedUpdateTimestamp is commonly expressed in seconds.
        # Keep the raw value too; do not use it to timestamp chart candles.
        try:
            feed_age = (
                round(now - float(last_feed_timestamp), 1)
                if last_feed_timestamp is not None else None
            )
        except (TypeError, ValueError):
            feed_age = None

        endpoints.append({
            "endpoint": stats["endpoint"],
            "status": stats["status"],
            "subscription_status": stats["subscription_status"],
            "connected_at": stats["connected_at"],
            "last_message_at": last_message,
            "message_age_seconds": (
                round(now - last_message, 1) if last_message else None
            ),
            "last_price_change_at": stats["last_price_change_at"],
            "messages_received_total": stats["messages_received"],
            "messages_per_minute": len(stats["message_times"]),
            "price_changes_total": stats["price_changes_total"],
            "price_changes_per_minute": len(stats["price_change_times"]),
            "last_price": (
                f"{stats['last_price'][0]}e{stats['last_price'][1]}"
                if stats["last_price"] else None
            ),
            "last_stream_timestamp_us": stats["last_stream_timestamp_us"],
            "last_feed_update_timestamp": last_feed_timestamp,
            "feed_age_seconds_estimate": feed_age,
            "last_error": stats["last_error"],
        })

    return {
        "provider": "Pyth",
        "scope": "current Railway process; counters reset on restart",
        "checked_at": now,
        "endpoints": endpoints,
    }


# ==================================================
# PYTH AUTHENTICATION / REST
# ==================================================

def get_pyth_headers():
    api_key = os.getenv("PYTH_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="PYTH_API_KEY is missing in Railway Variables"
        )
    return {"Authorization": f"Bearer {api_key.strip()}"}


async def pyth_get(path, params=None, headers=None, timeout=30):
    url = f"{PYTH_API_URL}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True
        ) as client:
            return await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        logger.exception("Pyth request timed out")
        raise HTTPException(status_code=504, detail="Pyth request timed out")
    except httpx.RequestError as error:
        logger.exception("Pyth connection error")
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not connect to Pyth",
                "error_type": type(error).__name__
            }
        )


def handle_pyth_error(response, endpoint):
    status = response.status_code

    if status == 401:
        raise HTTPException(status_code=401, detail="Pyth authentication failed. Check PYTH_API_KEY.")
    if status == 403:
        raise HTTPException(status_code=403, detail="Pyth credentials do not have access to this feed.")
    if status == 404:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Pyth could not find this symbol or channel.",
                "endpoint": endpoint,
                "provider_response": response.text[:500]
            }
        )
    if status == 429:
        raise HTTPException(status_code=429, detail="Pyth API rate limit reached.")
    if status != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Pyth API request failed",
                "endpoint": endpoint,
                "http_status": status,
                "provider_response": response.text[:500]
            }
        )


# ==================================================
# DASHBOARD / HEALTH / DATABASE
# ==================================================

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"api": "online", "database": "connected"}
    except Exception:
        logger.exception("Database health check failed")
        raise HTTPException(status_code=503, detail="Database disconnected")


@app.get("/database-summary")
def database_summary():
    try:
        results = {}
        with engine.connect() as connection:
            for table in TABLES:
                query = text(f"SELECT COUNT(*) FROM public.{table}")
                results[table] = connection.execute(query).scalar_one()
        return {"status": "success", "database": "connected", "tables": results}
    except Exception:
        logger.exception("Database summary failed")
        raise HTTPException(status_code=503, detail="Database summary unavailable")


@app.get("/api/status")
def api_status():
    return {
        "project": "GOLD AI LAB",
        "backend": "online",
        "database_monitor": "available",
        "market_data": "Pyth",
        "mode": "read_only",
        "live_stream": "websocket"
    }


@app.get("/api/pyth/diagnostics")
async def pyth_diagnostics():
    results = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for name, path in {"health": "/live", "symbols": "/symbols"}.items():
            started = time.perf_counter()
            try:
                response = await client.get(
                    f"{PYTH_API_URL}{path}",
                    params={"query": "XAU", "asset_type": "metal"} if name == "symbols" else None
                )
                results[name] = {
                    "connected": response.status_code == 200,
                    "http_status": response.status_code,
                    "response_time_ms": round((time.perf_counter() - started) * 1000)
                }
            except httpx.RequestError as error:
                results[name] = {
                    "connected": False,
                    "error_type": type(error).__name__
                }
    return {"provider": "Pyth", "diagnostics": results}


@app.get("/api/pyth/stream-diagnostics")
def pyth_stream_diagnostics():
    return get_pyth_diagnostics()


# ==================================================
# SYMBOLS / HISTORICAL OHLC
# ==================================================

@app.get("/api/market/symbols")
async def market_symbols():
    response = await pyth_get(
        "/symbols",
        params={"query": "XAU", "asset_type": "metal"}
    )
    if response.status_code != 200:
        handle_pyth_error(response, "/symbols")

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Invalid JSON received from Pyth")

    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Unexpected Pyth symbols response")

    symbols = []
    for item in data:
        symbols.append({
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "description": item.get("description"),
            "asset_type": item.get("asset_type"),
            "instrument_type": item.get("instrument_type"),
            "min_channel": item.get("min_channel"),
            "state": item.get("state"),
            "pyth_lazer_id": item.get("pyth_lazer_id"),
            "exponent": item.get("exponent")
        })

    return {"status": "success", "provider": "Pyth", "count": len(symbols), "symbols": symbols}


@app.get("/api/market/history")
async def market_history(
    symbol: str = Query(GOLD_SYMBOL, min_length=3, max_length=120),
    resolution: str = Query("5"),
    bars: int = Query(500, ge=50, le=500),
    to: int | None = Query(None, ge=1)
):
    if resolution not in RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Unsupported chart resolution")

    headers = get_pyth_headers()
    end_time = to if to is not None else int(time.time())
    seconds_per_bar = int(resolution) * 60
    start_time = end_time - seconds_per_bar * bars

    params = {
        "symbol": symbol,
        "from": start_time,
        "to": end_time,
        "resolution": resolution
    }
    endpoint = f"/{PYTH_CHANNEL}/history"

    response = await pyth_get(endpoint, params=params, headers=headers, timeout=30)
    if response.status_code != 200:
        handle_pyth_error(response, endpoint)

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Invalid historical data JSON from Pyth")

    if not isinstance(data, dict) or data.get("s") != "ok":
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Pyth returned no valid OHLC history",
                "provider_message": data.get("errmsg") if isinstance(data, dict) else None,
                "symbol": symbol,
                "requested_from": start_time,
                "requested_to": end_time
            }
        )

    timestamps = data.get("t") or []
    opens = data.get("o") or []
    highs = data.get("h") or []
    lows = data.get("l") or []
    closes = data.get("c") or []

    lengths = [len(timestamps), len(opens), len(highs), len(lows), len(closes)]
    if len(set(lengths)) != 1:
        raise HTTPException(status_code=502, detail="Pyth returned inconsistent candle data")

    candles = []
    for index in range(len(timestamps)):
        candles.append({
            "time": int(timestamps[index]),
            "open": float(opens[index]),
            "high": float(highs[index]),
            "low": float(lows[index]),
            "close": float(closes[index])
        })

    candles.sort(key=lambda item: item["time"])

    return {
        "status": "success",
        "provider": "Pyth",
        "symbol": symbol,
        "channel": PYTH_CHANNEL,
        "resolution": resolution,
        "count": len(candles),
        "requested_from": start_time,
        "requested_to": end_time,
        "bars": candles,
        "oldest_time": candles[0]["time"] if candles else None,
        "newest_time": candles[-1]["time"] if candles else None,
        "updated_at": int(time.time())
    }


# ==================================================
# LIVE PYTH WEBSOCKET WORKER
# ==================================================

async def pyth_stream_worker(endpoint, queue, api_key, feed_id, channel):
    subscribe_message = {
        "type": "subscribe",
        "subscriptionId": 1,
        "priceFeedIds": [feed_id],
        "properties": ["price", "exponent", "feedUpdateTimestamp"],
        "formats": [],
        "channel": channel,
        "ignoreInvalidFeeds": True,
        "deliveryFormat": "json",
        "parsed": True
    }

    stats = PYTH_DIAGNOSTICS[endpoint]

    while True:
        try:
            async with websockets.connect(
                endpoint,
                additional_headers={"Authorization": f"Bearer {api_key}"},
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=2**20
            ) as upstream:
                stats["status"] = "connected"
                stats["subscription_status"] = "pending"
                stats["connected_at"] = time.time()
                stats["last_error"] = None

                await upstream.send(json.dumps(subscribe_message))
                await queue.put({
                    "type": "connection",
                    "status": "connected",
                    "endpoint": endpoint
                })

                async for raw_message in upstream:
                    try:
                        message = json.loads(raw_message)
                    except (ValueError, TypeError):
                        continue

                    record_pyth_message(endpoint)
                    message_type = message.get("type")

                    if message_type == "subscriptionError":
                        error_text = str(message)[:500]
                        stats["subscription_status"] = "failed"
                        stats["last_error"] = error_text
                        await queue.put({
                            "type": "error",
                            "endpoint": endpoint,
                            "message": error_text
                        })
                        break

                    if message_type != "streamUpdated":
                        continue

                    parsed = message.get("parsed") or {}
                    feeds = parsed.get("priceFeeds") or []

                    for feed in feeds:
                        try:
                            current_feed_id = int(feed.get("priceFeedId", -1))
                        except (TypeError, ValueError):
                            continue

                        if current_feed_id != int(feed_id):
                            continue

                        price = feed.get("price")
                        exponent = feed.get("exponent")
                        if price is None or exponent is None:
                            continue

                        stats["subscription_status"] = "streaming"
                        stream_timestamp = parsed.get("timestampUs")
                        feed_timestamp = feed.get("feedUpdateTimestamp")

                        record_pyth_price(
                            endpoint, price, exponent,
                            stream_timestamp, feed_timestamp
                        )

                        await queue.put({
                            "type": "price",
                            "symbol": GOLD_SYMBOL,
                            "price": price,
                            "exponent": exponent,
                            "timestampUs": stream_timestamp,
                            "feedUpdateTimestamp": feed_timestamp,
                            "endpoint": endpoint
                        })

        except asyncio.CancelledError:
            stats["status"] = "stopped"
            raise

        except Exception as error:
            stats["status"] = "reconnecting"
            stats["subscription_status"] = "disconnected"
            stats["last_error"] = f"{type(error).__name__}: {str(error)[:300]}"
            logger.warning(
                "Pyth WebSocket disconnected (%s): %s",
                endpoint, type(error).__name__
            )
            await queue.put({
                "type": "connection",
                "status": "reconnecting",
                "endpoint": endpoint
            })
            await asyncio.sleep(3)


# ==================================================
# FRONTEND LIVE MARKET WEBSOCKET
# ==================================================

@app.websocket("/ws/market")
async def live_market(websocket: WebSocket):
    await websocket.accept()

    symbol = websocket.query_params.get("symbol", GOLD_SYMBOL)
    api_key = os.getenv("PYTH_API_KEY")

    if not api_key:
        await websocket.send_json({"type": "error", "message": "PYTH_API_KEY is missing"})
        await websocket.close()
        return

    queue = asyncio.Queue()
    workers = []

    try:
        response = await pyth_get("/symbols", params={"query": symbol}, timeout=20)
        if response.status_code != 200:
            handle_pyth_error(response, "/symbols")

        symbol_data = response.json()
        matched = next(
            (
                item for item in symbol_data
                if item.get("symbol") == symbol and item.get("state") == "stable"
            ),
            None
        )

        if not matched:
            await websocket.send_json({
                "type": "error",
                "message": f"Active Pyth feed not found: {symbol}"
            })
            await websocket.close()
            return

        feed_id = matched.get("pyth_lazer_id")
        channel = matched.get("min_channel") or PYTH_CHANNEL

        if feed_id is None:
            await websocket.send_json({
                "type": "error",
                "message": "Pyth feed ID is missing for this symbol"
            })
            await websocket.close()
            return

        await websocket.send_json({
            "type": "connection",
            "status": "starting",
            "symbol": symbol,
            "feed_id": feed_id,
            "channel": channel
        })

        for endpoint in PYTH_WS_URLS:
            workers.append(asyncio.create_task(
                pyth_stream_worker(
                    endpoint, queue, api_key.strip(), feed_id, channel
                )
            ))

        last_update_key = None

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({
                    "type": "heartbeat",
                    "timestamp": int(time.time())
                })
                continue

            if item.get("type") == "price":
                update_key = (
                    item.get("timestampUs"),
                    item.get("price"),
                    item.get("exponent")
                )
                if update_key == last_update_key:
                    continue
                last_update_key = update_key

            await websocket.send_json(item)

    except WebSocketDisconnect:
        logger.info("Market WebSocket client disconnected")

    except Exception:
        logger.exception("Live market WebSocket error")
        try:
            await websocket.send_json({
                "type": "error",
                "message": "Live market stream encountered an error"
            })
        except Exception:
            pass

    finally:
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
