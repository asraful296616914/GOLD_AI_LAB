from fastapi import FastAPI
from sqlalchemy import text
from app.database import engine

app = FastAPI(
    title="GOLD AI LAB API",
    version="1.0.0"
)

@app.get("/")
def home():
    return {
        "project": "GOLD AI LAB",
        "status": "online"
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
        return {
            "api": "online",
            "database": "disconnected"
        }
