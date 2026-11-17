import os
import time
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
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

# Gold feed supports fixed_rate@200ms and slower channels.
PYTH_CHANNEL = "fixed_rate@200ms"

GOLD_SYMBOL = "Metal.XAU/USD"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold-ai-lab")

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only market data and research dashboard",
    version="1.6.0"
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
# PYTH REQUEST HELPER
# ==================================================

async def pyth_get(
    path,
    params=None,
    headers=None,
    timeout=30
):

    url = f"{PYTH_API_URL}{path}"

    try:

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True
        ) as client:

            response = await client.get(
                url,
                params=params,
                headers=headers
            )

            return response

    except httpx.TimeoutException:

        logger.exception("Pyth request timed out")

        raise HTTPException(
            status_code=504,
            detail="Pyth request timed out"
        )

    except httpx.RequestError as error:

        logger.exception(
            "Pyth connection error: %s",
            type(error).__name__
        )

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
                "message": "Pyth could not find this symbol on the requested channel, or it is not visible to these credentials.",
                "endpoint": endpoint,
                "channel": PYTH_CHANNEL,
                "symbol": GOLD_SYMBOL,
                "provider_response": response.text[:500]
            }
        )

    if status == 429:
        raise HTTPException(
            status_code=429,
            detail="Pyth API rate limit reached. Try again later."
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

    return FileResponse(
        FRONTEND_DIR / "index.html"
    )


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
        "mode": "read_only"
    }


# ==================================================
# PYTH CONNECTION DIAGNOSTICS
# ==================================================

@app.get("/api/pyth/diagnostics")
async def pyth_diagnostics():

    results = {}

    endpoints = {
        "health": "/live",
        "symbols": "/symbols"
    }

    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True
    ) as client:

        for name, path in endpoints.items():

            started = time.time()

            try:

                response = await client.get(
                    f"{PYTH_API_URL}{path}",
                    params=(
                        {"query": "XAU", "asset_type": "metal"}
                        if name == "symbols"
                        else None
                    )
                )

                results[name] = {
                    "connected": True,
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
        "base_url": PYTH_API_URL,
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
            "state": item.get("state")
        })

    return {
        "status": "success",
        "provider": "Pyth",
        "count": len(symbols),
        "symbols": symbols
    }


# ==================================================
# GOLD HISTORICAL OHLC DATA
# ==================================================

@app.get("/api/market/history")
async def market_history(

    symbol: str = Query(
        GOLD_SYMBOL,
        min_length=3,
        max_length=120
    ),

    resolution: str = Query("5"),

    bars: int = Query(
        100,
        ge=50,
        le=500
    )

):

    if resolution not in RESOLUTIONS:

        raise HTTPException(
            status_code=400,
            detail="Unsupported chart resolution"
        )

    # Use the verified Gold symbol by default.
    # Other symbols are accepted only as explicit query inputs.

    headers = get_pyth_headers()

    end_time = int(time.time())

    minutes_per_bar = int(resolution)

    start_time = end_time - (
        minutes_per_bar * bars * 60
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

    if not isinstance(data, dict):

        raise HTTPException(
            status_code=502,
            detail="Unexpected historical data format"
        )

    if data.get("s") != "ok":

        raise HTTPException(
            status_code=502,
            detail={
                "message": "Pyth returned no valid OHLC history",
                "provider_message": data.get("errmsg"),
                "symbol": symbol,
                "channel": PYTH_CHANNEL
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

    return {
        "status": "success",
        "provider": "Pyth",
        "symbol": symbol,
        "channel": PYTH_CHANNEL,
        "resolution": resolution,
        "count": len(candles),
        "bars": candles,
        "updated_at": int(time.time())
    }
