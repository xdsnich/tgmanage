"""
GramGPT API — main.py
Точка входа FastAPI приложения

Запуск:
  uvicorn main:app --reload --port 8000
"""

import sys

# Windows console может иметь не-UTF-8 кодировку — принудительно переключаем
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import APP_NAME, APP_VERSION, DEBUG, CORS_ORIGINS
from database import create_tables
from routers import auth, accounts, proxies, tasks
from routers import tg_auth, analytics, security, channels, actions, inbox, tdata, commenting, warmup, parser, api_apps, reactions, subscribe, service_credentials
from routers import health
from routers import web_session
from routers import diagnostics
# ── Lifespan (старт / стоп) ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    print(f"[OK] {APP_NAME} v{APP_VERSION} started")
    print(f"[DOCS] http://localhost:8000/docs")
    yield
    print("[STOP] API stopped")


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
app.include_router(subscribe.router, prefix=PREFIX)
app.include_router(health.router, prefix=f"{PREFIX}")
app.include_router(service_credentials.router, prefix=PREFIX)
app.include_router(web_session.router, prefix="/api/v1")
app.include_router(diagnostics.router, prefix=PREFIX)  # Диагностика подписок и аккаунтов
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

# ═══════════════════════════════════════════════════════════
# ЗАПУСК (масштабируемая конфигурация — gevent worker pool)
# ═══════════════════════════════════════════════════════════
#
# Терминал 1 — API:
#   cd api && python -m uvicorn main:app --reload --port 8000
#
# Терминал 2 — Celery воркер на gevent (вместо -P solo!):
#   cd api && python -m celery -A celery_app worker -Q high_priority,bulk_actions,ai_dialogs -P gevent -c 50 --loglevel=info
#
#   -P gevent  — асинхронный пул (вместо solo = 1 задача за раз)
#   -c 50      — concurrency: до 50 параллельных задач одновременно
#   Для большего scale: -c 100, -c 200. Memory ~5MB на задачу.
#
# Терминал 3 — Планировщик:
#   cd api && python run_periodic.py
#
# Терминал 4 — Фронт:
#   cd gramgpt-web && npm run dev
#
# ───── Старая конфигурация (-P solo) — для отладки одного аккаунта ─────
# python -m celery -A celery_app worker -Q high_priority,bulk_actions,ai_dialogs --loglevel=info -P solo
#
# ───── Закрытые каналы (event listener) ─────
# cd api && python run_listener.py