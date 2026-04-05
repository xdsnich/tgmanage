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
from routers import tg_auth, analytics, security, channels, actions, inbox, tdata, commenting, warmup, parser, api_apps, reactions

# ── Lifespan (старт / стоп) ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    print(f"✅ {APP_NAME} v{APP_VERSION} запущен")
    print(f"📖 Документация: http://localhost:8000/docs")
    yield
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
PREFIX = "/api/v1"

app.include_router(auth.router,      prefix=PREFIX)
app.include_router(accounts.router,  prefix=PREFIX)
app.include_router(proxies.router,   prefix=PREFIX)
app.include_router(tasks.router,     prefix=PREFIX)

# Новые роутеры
app.include_router(tg_auth.router,   prefix=PREFIX)   # Веб-авторизация Telegram
app.include_router(analytics.router, prefix=PREFIX)   # Dashboard / аналитика
app.include_router(security.router,  prefix=PREFIX)   # Сессии, 2FA
app.include_router(channels.router,  prefix=PREFIX)   # Каналы
app.include_router(actions.router,   prefix=PREFIX)   # Быстрые действия
app.include_router(inbox.router,     prefix=PREFIX)   # Входящие / ИИ-диалоги
app.include_router(tdata.router,     prefix=PREFIX)   # TData / Session импорт
app.include_router(commenting.router, prefix=PREFIX)  # Нейрокомментинг
app.include_router(warmup.router,    prefix=PREFIX)  # Прогрев аккаунтов
app.include_router(parser.router,    prefix=PREFIX)  # Парсер каналов
app.include_router(api_apps.router, prefix=PREFIX)  # Мульти-API ключи
app.include_router(reactions.router, prefix=PREFIX)  # Реакции


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
        "routes": {
            "auth":       f"{PREFIX}/auth",
            "accounts":   f"{PREFIX}/accounts",
            "proxies":    f"{PREFIX}/proxies",
            "tasks":      f"{PREFIX}/tasks",
            "tg_auth":    f"{PREFIX}/tg-auth",
            "analytics":  f"{PREFIX}/analytics",
            "security":   f"{PREFIX}/security",
            "channels":   f"{PREFIX}/channels",
            "actions":    f"{PREFIX}/actions",
            "inbox":      f"{PREFIX}/inbox",
            "api_apps": f"{PREFIX}/api-apps",
            "reactions": f"{PREFIX}/reactions",
        }
    }

# Запуск:
# cd api && venv\Scripts\activate
# uvicorn main:app --reload --port 8000
# python -m celery -A celery_app worker -Q high_priority,bulk_actions --loglevel=info -P solo

# Терминал 1 (API):      cd api → uvicorn main:app --reload --port 8000
# Терминал 2 (Worker):   cd api → celery -A celery_app worker -Q high_priority,bulk_actions,ai_dialogs --loglevel=info -P solo
# Терминал 3 (Beat):     cd api → python run_periodic.py
# Терминал 4 (Frontend): cd gramgpt-web → npm run dev
# Терминал 1: API
# cd api && uvicorn main:app --reload --port 8000

# # Терминал 2: Worker
# cd api && celery -A celery_app worker -Q high_priority,bulk_actions,ai_dialogs --loglevel=info -P solo

# # Терминал 3: Публичные каналы (веб-парсинг каждые 90с)
# cd api && python run_periodic.py

# # Терминал 4: Закрытые каналы (event listener)
# cd api && python run_listener.py

# # Терминал 5: Frontend
# cd gramgpt-web && npm run dev