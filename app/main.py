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

# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

PYTH_API_URL = "https://pyth.dourolabs.app/v1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold-ai-lab")

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only market data and research dashboard",
    version="1.5.0"
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


# --------------------------------------------------
# Pyth Authentication
# --------------------------------------------------

def get_pyth_headers():
    api_key = os.getenv("PYTH_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="PYTH_API_KEY is missing in Railway Variables"
        )

    return {
        "Authorization": f"Bearer {api_key}"
    }


# --------------------------------------------------
# Pyth HTTP Request Helper
# --------------------------------------------------

async def pyth_get(path, params=None, headers=None, timeout=20):
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

    except httpx.RequestError as error:
        logger.exception(
            "Pyth connection error: %s",
            type(error).__name__
        )

        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not connect to Pyth",
                "error_type": type(error).__name__,
                "error": str(error)[:250]
            }
        )


# --------------------------------------------------
# Dashboard
# --------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(FRONTEND_DIR / "index.html")


# --------------------------------------------------
# Health Check
# --------------------------------------------------

@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "api": "online",
            "database": "connected"
        }

    except Exception as error:
        logger.exception("Database health check failed")

        raise HTTPException(
            status_code=503,
            detail="Database disconnected"
        )


# --------------------------------------------------
# Database Summary
# --------------------------------------------------

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


# --------------------------------------------------
# API Status
# --------------------------------------------------

@app.get("/api/status")
def api_status():
    return {
        "project": "GOLD AI LAB",
        "backend": "online",
        "database_monitor": "available",
        "market_data": "Pyth",
        "mode": "read_only"
    }


# --------------------------------------------------
# PYTH CONNECTION DIAGNOSTICS
# --------------------------------------------------

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

                logger.exception(
                    "Pyth diagnostic failed: %s",
                    name
                )

                results[name] = {
                    "connected": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:250]
                }

    return {
        "provider": "Pyth",
        "base_url": PYTH_API_URL,
        "diagnostics": results
    }


# --------------------------------------------------
# PYTH SYMBOL SEARCH
# --------------------------------------------------

@app.get("/api/market/symbols")
async def market_symbols():

    # Official symbols endpoint does not require authentication.
    params = {
        "query": "XAU",
        "asset_type": "metal"
    }

    response = await pyth_get(
        "/symbols",
        params=params
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Pyth symbols request failed: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Pyth returned invalid JSON"
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


# --------------------------------------------------
# PYTH HISTORICAL OHLC DATA
# --------------------------------------------------

@app.get("/api/market/history")
async def market_history(
    symbol: str = Query(..., min_length=3, max_length=120),
    resolution: str = Query("5"),
    bars: int = Query(300, ge=50, le=500)
):

    if resolution not in RESOLUTIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported chart resolution"
        )

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

    response = await pyth_get(
        "/real_time/history",
        params=params,
        headers=headers,
        timeout=30
    )

    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="Pyth API authentication failed"
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail="Your Pyth API plan does not have access to this feed"
        )

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Gold symbol is unavailable on this channel"
        )

    if response.status_code == 429:
        raise HTTPException(
            status_code=429,
            detail="Pyth API rate limit reached. Try again later"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Pyth history request failed: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Pyth returned invalid history JSON"
        )

    if data.get("s") != "ok":
        return {
            "status": "no_data",
            "provider": "Pyth",
            "symbol": symbol,
            "bars": []
        }

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
        "resolution": resolution,
        "count": len(candles),
        "bars": candles,
        "updated_at": int(time.time())
    }
