"""
GramGPT API — config.py
Все настройки читаются из .env
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── PostgreSQL ───────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://gramgpt:gramgpt@localhost:5432/gramgpt"
)

# ── JWT ─────────────────────────────────────────────────────
SECRET_KEY     = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-me-in-production-please":
    raise RuntimeError("Set SECRET_KEY in .env! Default or empty SECRET_KEY is not allowed.")
ALGORITHM      = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES  = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))
REFRESH_TOKEN_EXPIRE_DAYS    = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", 30))

# ── Telegram ─────────────────────────────────────────────────
TG_API_ID   = int(os.getenv("TG_API_ID", 0))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()

# ── Пути ────────────────────────────────────────────────────
SESSIONS_DIR = BASE_DIR.parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Приложение ───────────────────────────────────────────────
APP_NAME    = "GramGPT API"
APP_VERSION = "0.6.0"
DEBUG       = os.getenv("DEBUG", "true").lower() == "true"

# ── CORS (разрешаем фронтенд) ────────────────────────────────
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
