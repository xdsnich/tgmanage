"""
GramGPT API — main.py
Точка входа FastAPI приложения

Запуск:
  uvicorn main:app --reload --port 8000
"""

import sys
import os

# ── sys.path-cleanup ДОЛЖЕН быть до любого 'from config import ...' ──
# Защита от легаси tg_manager1/config.py — см. celery_app.py для деталей.
_API_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_API_DIR)
sys.path[:] = [p for p in sys.path
               if os.path.normcase(os.path.abspath(p) if p else os.getcwd()) != os.path.normcase(_PARENT_DIR)]
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)
for _mod in list(sys.modules):
    if _mod == "config" or _mod.startswith("config."):
        del sys.modules[_mod]

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
from utils.logging_setup import setup_logging

# Логи API → logs/api.log с ротацией 100MB × 10
setup_logging("api")
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
# ЗАПУСК (масштабируемая конфигурация)
# ═══════════════════════════════════════════════════════════
#
# Терминал 1 — API:
#   cd api && python -m uvicorn main:app --reload --port 8000
#
# Терминал 2 — Celery воркер (ВСЕ очереди в одном процессе, простой сетап):
#   cd api && celery -A celery_app worker `
#     -Q plans,warmup,parsers,ai_dialogs,high_priority,bulk_actions,subscribe `
#     -P threads -c 40 `
#     --without-gossip --without-mingle --without-heartbeat `
#     --loglevel=info
#
#   -P threads — каждый таск в своём треде, у каждого треда свой asyncio loop.
#                Для I/O-bound кода (Telethon, asyncpg, httpx) GIL отпускается → параллелизм.
#   -c 40      — до 40 параллельных задач. ~5MB/тред. Для больше: -c 60, -c 100.
#
#   --without-gossip   — не общаться с другими воркерами (нет multi-worker сетапа)
#   --without-mingle   — не делать startup-sync с broker (быстрее старт + shutdown)
#   --without-heartbeat — не пинговать broker (быстрее shutdown на Windows)
#   Эти 3 флага суммарно экономят ~5 сек на каждом shutdown.
#
#   ПОЧЕМУ НЕ -P gevent: greenlet'ы делят OS-тред, asyncio видит чужой running loop →
#   "Cannot run the event loop while another loop is running". Несовместимо.
#
#   ВЫКЛЮЧЕНИЕ:
#   - Ctrl+C один раз → warm shutdown (ждёт max 5с, потом force-kill)
#   - Ctrl+C два раза  → cold shutdown (мгновенно)
#   - python kill_workers.py        → graceful через Redis broker (с другого терминала)
#   - python kill_workers.py --hard → taskkill /F (если совсем застрял)
#
# Терминал 3 — Celery Beat (планировщик, раз в 60с шлёт dispatch_plans/warmups/ai):
#   cd api && python -m celery -A celery_app beat --loglevel=info
#
#   Beat НЕ выполняет таски сам — только отправляет в очередь воркеру по расписанию.
#   Расписание задано в celery_app.py → beat_schedule.
#   ⚠ НЕ запускай run_periodic.py одновременно с beat — будут дубли тасков!
#
# Терминал 4 — Фронт:
#   cd gramgpt-web && npm run dev
#
# Терминал 5 (опционально) — Flower UI для мониторинга очередей:
#   cd api && python -m celery -A celery_app flower --port=5555
#   Открывает http://localhost:5555 — видно активные таски, очереди, fail rate.
#
# ═══════════════════════════════════════════════════════════
# ПРОДВИНУТЫЙ СЕТАП (>100 юзеров) — разделение по процессам
# ═══════════════════════════════════════════════════════════
# Парсер-crawler может занять 10 минут — изолируем чтобы не блокировал планы.
# Каждый процесс независим, можно перезапустить без остановки других.
#
# Воркер 1 — ПЛАНЫ (hot path, максимум throughput):
#   celery -A celery_app worker -Q plans -P threads -c 40 -n plans@%h --loglevel=info
#
# Воркер 2 — ПРОГРЕВ:
#   celery -A celery_app worker -Q warmup -P threads -c 20 -n warmup@%h --loglevel=info
#
# Воркер 3 — ПАРСЕРЫ (долгие задачи, низкая concurrency):
#   celery -A celery_app worker -Q parsers -P threads -c 8 -n parsers@%h --loglevel=info
#
# Воркер 4 — Прочее (AI-диалоги, комменты, импорт, подписки, account/proxy ops):
#   celery -A celery_app worker -Q ai_dialogs,high_priority,bulk_actions,subscribe -P threads -c 20 -n misc@%h --loglevel=info
#
# ───── Старая конфигурация (-P solo) — для отладки одного аккаунта ─────
# celery -A celery_app worker -Q plans,warmup,parsers,ai_dialogs,high_priority,bulk_actions,subscribe --loglevel=info -P solo
#
# ───── Закрытые каналы (event listener) ─────
# python run_listener.py