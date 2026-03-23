"""
GramGPT — config.py
Все настройки в одном месте.
Значения читаются из .env файла (никаких ключей в коде).
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ============================================================
# TELEGRAM API
# Заполняй в .env, не здесь
# ============================================================
API_ID   = int(os.getenv("TG_API_ID", "0").strip())
API_HASH = os.getenv("TG_API_HASH", "").strip()



# ============================================================
# ПУТИ
# ============================================================
SESSIONS_DIR = BASE_DIR / "sessions"
DATA_DIR     = BASE_DIR / "data"
LOGS_DIR     = BASE_DIR / "logs"

SESSIONS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
PROXIES_FILE  = DATA_DIR / "proxies.json"

# ============================================================
# TRUST SCORE
# ============================================================
TRUST_SCORE = {
    "base":         50,
    "has_username": +5,
    "has_bio":      +3,
    "has_photo":    +3,
    "active_ok":    +2,
    "spamblock":    -30,
    "frozen":       -20,
    "clean_day":    +1,
    "system_mute":  -3,
}

# ============================================================
# ПОВЕДЕНИЕ
# ============================================================
MIN_DELAY   = float(os.getenv("MIN_DELAY", 1.5))
MAX_DELAY   = float(os.getenv("MAX_DELAY", 4.0))
BOT_TIMEOUT = 15
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))