from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from app.database import engine

app = FastAPI(
    title="GOLD AI LAB",
    description="Read-only market data and research dashboard API",
    version="1.1.0"
)

TABLES = [
    "market_prices",
    "market_sources",
    "news_articles",
    "economic_events",
    "system_logs"
]


@app.get("/")
def home():
    return {
        "project": "GOLD AI LAB",
        "status": "online",
        "version": "1.1.0"
    }


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
