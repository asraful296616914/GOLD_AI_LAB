import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

PYTH_API_URL = "https://pyth.dourolabs.app/v1"

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only market data and research dashboard",
    version="1.4.0"
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


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(FRONTEND_DIR / "index.html")


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
        raise HTTPException(
            status_code=503,
            detail="Database disconnected"
        )


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
        raise HTTPException(
            status_code=503,
            detail="Database summary unavailable"
        )


@app.get("/api/status")
def api_status():
    return {
        "project": "GOLD AI LAB",
        "backend": "online",
        "database_monitor": "available",
        "market_data": "Pyth",
        "mode": "read_only"
    }


@app.get("/api/market/symbols")
async def market_symbols():
    headers = get_pyth_headers()

    params = {
        "query": "XAU",
        "asset_type": "metal"
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{PYTH_API_URL}/symbols",
                headers=headers,
                params=params
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Pyth symbols request failed: HTTP {response.status_code}"
            )

        data = response.json()

        if not isinstance(data, list):
            raise HTTPException(
                status_code=502,
                detail="Unexpected Pyth symbols response"
            )

        symbols = []

        for item in data:
            if item.get("entitled") is False:
                continue

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

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Pyth"
        )


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

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{PYTH_API_URL}/real_time/history",
                headers=headers,
                params=params
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

        data = response.json()

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

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Pyth history service"
        )
