import os
import time
import json
import asyncio
import logging
from pathlib import Path

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
    version="2.0.0"
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
# PYTH AUTHENTICATION
# ==================================================

def get_pyth_headers():
    api_key = os.getenv("PYTH_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="PYTH_API_KEY is missing in Railway Variables"
        )

    return {
        "Authorization": f"Bearer {api_key.strip()}"
    }


# ==================================================
# PYTH REST REQUEST
# ==================================================

async def pyth_get(path, params=None, headers=None, timeout=30):
    url = f"{PYTH_API_URL}{path}"

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True
        ) as client:
            return await client.get(
                url,
                params=params,
                headers=headers
            )

    except httpx.TimeoutException:
        logger.exception("Pyth request timed out")
        raise HTTPException(
            status_code=504,
            detail="Pyth request timed out"
        )

    except httpx.RequestError as error:
        logger.exception("Pyth connection error")

        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not connect to Pyth",
                "error_type": type(error).__name__
            }
        )


# ==================================================
# PYTH ERROR HANDLER
# ==================================================

def handle_pyth_error(response, endpoint):
    status = response.status_code

    if status == 401:
        raise HTTPException(
            status_code=401,
            detail="Pyth authentication failed. Check PYTH_API_KEY."
        )

    if status == 403:
        raise HTTPException(
            status_code=403,
            detail="Pyth credentials do not have access to this feed."
        )

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
        raise HTTPException(
            status_code=429,
            detail="Pyth API rate limit reached."
        )

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
# DASHBOARD
# ==================================================

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(FRONTEND_DIR / "index.html")


# ==================================================
# HEALTH CHECK
# ==================================================

@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "api": "online",
            "database": "connected"
        }

    except Exception:
        logger.exception("Database health check failed")

        raise HTTPException(
            status_code=503,
            detail="Database disconnected"
        )


# ==================================================
# DATABASE SUMMARY
# ==================================================

@app.get("/database-summary")
def database_summary():
    try:
        results = {}

        with engine.connect() as connection:
            for table in TABLES:
                query = text(
                    f"SELECT COUNT(*) FROM public.{table}"
                )

                results[table] = connection.execute(
                    query
                ).scalar_one()

        return {
            "status": "success",
            "database": "connected",
            "tables": results
        }

    except Exception:
        logger.exception("Database summary failed")

        raise HTTPException(
            status_code=503,
            detail="Database summary unavailable"
        )


# ==================================================
# API STATUS
# ==================================================

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


# ==================================================
# PYTH DIAGNOSTICS
# ==================================================

@app.get("/api/pyth/diagnostics")
async def pyth_diagnostics():
    results = {}

    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True
    ) as client:

        for name, path in {
            "health": "/live",
            "symbols": "/symbols"
        }.items():

            started = time.time()

            try:
                response = await client.get(
                    f"{PYTH_API_URL}{path}",
                    params=(
                        {"query": "XAU", "asset_type": "metal"}
                        if name == "symbols" else None
                    )
                )

                results[name] = {
                    "connected": response.status_code == 200,
                    "http_status": response.status_code,
                    "response_time_ms": round(
                        (time.time() - started) * 1000
                    )
                }

            except httpx.RequestError as error:
                results[name] = {
                    "connected": False,
                    "error_type": type(error).__name__
                }

    return {
        "provider": "Pyth",
        "diagnostics": results
    }


# ==================================================
# GOLD SYMBOL SEARCH
# ==================================================

@app.get("/api/market/symbols")
async def market_symbols():
    response = await pyth_get(
        "/symbols",
        params={
            "query": "XAU",
            "asset_type": "metal"
        }
    )

    if response.status_code != 200:
        handle_pyth_error(response, "/symbols")

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Invalid JSON received from Pyth"
        )

    if not isinstance(data, list):
        raise HTTPException(
            status_code=502,
            detail="Unexpected Pyth symbols response"
        )

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

    return {
        "status": "success",
        "provider": "Pyth",
        "count": len(symbols),
        "symbols": symbols
    }


# ==================================================
# GOLD HISTORICAL OHLC
# ==================================================

@app.get("/api/market/history")
async def market_history(
    symbol: str = Query(
        GOLD_SYMBOL,
        min_length=3,
        max_length=120
    ),
    resolution: str = Query("5"),
    bars: int = Query(500, ge=50, le=500),
    to: int | None = Query(None, ge=1)
):

    if resolution not in RESOLUTIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported chart resolution"
        )

    headers = get_pyth_headers()

    end_time = to if to is not None else int(time.time())

    seconds_per_bar = int(resolution) * 60

    start_time = end_time - (
        seconds_per_bar * bars
    )

    params = {
        "symbol": symbol,
        "from": start_time,
        "to": end_time,
        "resolution": resolution
    }

    endpoint = f"/{PYTH_CHANNEL}/history"

    response = await pyth_get(
        endpoint,
        params=params,
        headers=headers,
        timeout=30
    )

    if response.status_code != 200:
        handle_pyth_error(response, endpoint)

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Invalid historical data JSON from Pyth"
        )

    if not isinstance(data, dict) or data.get("s") != "ok":
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Pyth returned no valid OHLC history",
                "provider_message": (
                    data.get("errmsg")
                    if isinstance(data, dict) else None
                ),
                "symbol": symbol
            }
        )

    timestamps = data.get("t") or []
    opens = data.get("o") or []
    highs = data.get("h") or []
    lows = data.get("l") or []
    closes = data.get("c") or []

    lengths = [
        len(timestamps),
        len(opens),
        len(highs),
        len(lows),
        len(closes)
    ]

    if len(set(lengths)) != 1:
        raise HTTPException(
            status_code=502,
            detail="Pyth returned inconsistent candle data"
        )

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
        "bars": candles,
        "oldest_time": (
            candles[0]["time"] if candles else None
        ),
        "updated_at": int(time.time())
    }


# ==================================================
# LIVE PYTH WEBSOCKET WORKER
# ==================================================

async def pyth_stream_worker(
    endpoint,
    queue,
    api_key,
    feed_id,
    channel
):
    subscribe_message = {
        "type": "subscribe",
        "subscriptionId": 1,
        "priceFeedIds": [feed_id],
        "properties": [
            "price",
            "exponent",
            "feedUpdateTimestamp"
        ],
        "formats": [],
        "channel": channel,
        "ignoreInvalidFeeds": True,
        "deliveryFormat": "json",
        "parsed": True
    }

    while True:
        try:
            async with websockets.connect(
                endpoint,
                additional_headers={
                    "Authorization": f"Bearer {api_key}"
                },
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=2**20
            ) as upstream:

                await upstream.send(
                    json.dumps(subscribe_message)
                )

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

                    if message.get("type") == "subscriptionError":
                        await queue.put({
                            "type": "error",
                            "message": str(message)[:500]
                        })
                        break

                    parsed = message.get("parsed") or {}

                    if message.get("type") != "streamUpdated":
                        continue

                    feeds = parsed.get("priceFeeds") or []

                    for feed in feeds:
                        if int(feed.get("priceFeedId", -1)) != int(feed_id):
                            continue

                        await queue.put({
                            "type": "price",
                            "symbol": GOLD_SYMBOL,
                            "price": feed.get("price"),
                            "exponent": feed.get("exponent"),
                            "timestampUs": parsed.get("timestampUs"),
                            "feedUpdateTimestamp": feed.get(
                                "feedUpdateTimestamp"
                            )
                        })

        except asyncio.CancelledError:
            raise

        except Exception as error:
            logger.warning(
                "Pyth WebSocket disconnected (%s): %s",
                endpoint,
                type(error).__name__
            )

            await queue.put({
                "type": "connection",
                "status": "reconnecting",
                "endpoint": endpoint
            })

            await asyncio.sleep(3)


# ==================================================
# LIVE MARKET WEBSOCKET
# ==================================================

@app.websocket("/ws/market")
async def live_market(websocket: WebSocket):
    await websocket.accept()

    symbol = websocket.query_params.get(
        "symbol",
        GOLD_SYMBOL
    )

    api_key = os.getenv("PYTH_API_KEY")

    if not api_key:
        await websocket.send_json({
            "type": "error",
            "message": "PYTH_API_KEY is missing"
        })
        await websocket.close()
        return

    queue = asyncio.Queue()
    workers = []

    try:
        response = await pyth_get(
            "/symbols",
            params={"query": symbol},
            timeout=20
        )

        if response.status_code != 200:
            handle_pyth_error(response, "/symbols")

        symbol_data = response.json()

        matched = next(
            (
                item for item in symbol_data
                if item.get("symbol") == symbol
                and item.get("state") == "stable"
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

        # Connect to all Pyth endpoints for redundancy.
        for endpoint in PYTH_WS_URLS:
            task = asyncio.create_task(
                pyth_stream_worker(
                    endpoint,
                    queue,
                    api_key.strip(),
                    feed_id,
                    channel
                )
            )
            workers.append(task)

        last_update_key = None

        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(),
                    timeout=30
                )
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

                # Remove duplicate messages from redundant endpoints.
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
            await asyncio.gather(
                *workers,
                return_exceptions=True
                        )
