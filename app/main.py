import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

PYTH_API_URL = "https://pyth.dourolabs.app/v1"

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only market research dashboard",
    version="1.3.0"
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
        "mode": "read_only"
    }


@app.get("/api/market/symbols")
async def market_symbols():
    api_key = os.getenv("PYTH_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="PYTH_API_KEY is missing"
        )

    headers = {
        "Authorization": f"Bearer {api_key}"
    }

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
                detail=f"Pyth API returned HTTP {response.status_code}"
            )

        data = response.json()

        if not isinstance(data, list):
            raise HTTPException(
                status_code=502,
                detail="Unexpected Pyth response"
            )

        symbols = []

        for item in data:
            symbols.append({
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "description": item.get("description"),
                "asset_type": item.get("asset_type"),
                "instrument_type": item.get("instrument_type"),
                "state": item.get("state"),
                "min_channel": item.get("min_channel"),
                "entitled": item.get("entitled")
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
            detail="Could not connect to Pyth API"
        )
