"""
GramGPT API — main.py
Точка входа FastAPI приложения

Запуск:
  uvicorn main:app --reload --port 8000
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import APP_NAME, APP_VERSION, DEBUG, CORS_ORIGINS
from database import create_tables
from routers import auth, accounts, proxies, tasks


# ── Lifespan (старт / стоп) ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Старт — создаём таблицы если нет
    await create_tables()
    print(f"✅ {APP_NAME} v{APP_VERSION} запущен")
    print(f"📖 Документация: http://localhost:8000/docs")
    yield
    # Стоп
    print("👋 API остановлен")


# ── Приложение ───────────────────────────────────────────────
app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="REST API для GramGPT — менеджера Telegram аккаунтов",
    debug=DEBUG,
    lifespan=lifespan,
)


# ── CORS ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Роутеры ──────────────────────────────────────────────────
app.include_router(auth.router,     prefix="/api/v1")
app.include_router(accounts.router, prefix="/api/v1")
app.include_router(proxies.router,  prefix="/api/v1")
app.include_router(tasks.router,    prefix="/api/v1")


# ── Healthcheck ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/")
async def root():
    return {
        "name":    APP_NAME,
        "version": APP_VERSION,
        "docs":    "/docs",
    }


# {
#   "email": "test@test.com",
#   "password": "password123"
# }
#cd api && venv\Scripts\activate
# uvicorn main:app --reload --port 8000
# python -m celery -A celery_app worker -Q high_priority,bulk_actions --loglevel=info -P solo