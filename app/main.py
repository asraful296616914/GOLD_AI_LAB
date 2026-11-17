from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only market data and research dashboard API",
    version="1.2.0"
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
            detail={
                "api": "online",
                "database": "disconnected"
            }
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
