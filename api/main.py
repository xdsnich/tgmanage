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
from routers import tg_auth, analytics, security, channels, actions, inbox, tdata, commenting, warmup, parser, api_apps, reactions, subscribe, service_credentials, account_media
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
app.include_router(account_media.router, prefix=PREFIX)  # Фото для сториз (per-account)
app.include_router(account_media.bulk_router, prefix=PREFIX)  # Bulk-операции с медиа (несколько акков)
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
# ЗАПУСК — конфигурация для 400+ аккаунтов
# ═══════════════════════════════════════════════════════════
#
# Предусловия (один раз перед стартом):
#   • .env содержит MAX_SLOTS_PER_USER=20, MAX_DAILY_CONNECTIONS=10, REDIS_POOL_MAX=100
#   • PostgreSQL рестартнут после ALTER SYSTEM (max_connections=300, shared_buffers=1GB)
#     Restart-Service postgresql-x64-18      ← PowerShell от админа
#   • Прокси-пул: минимум 50 уникальных IP на 400 акков (5-10 акков на прокси)
#
# ── Терминал 1: API ────────────────────────────────────────
#   cd api && python -m uvicorn main:app --reload --port 8000
#
# ── Терминал 2: Celery Worker (один на всё) ────────────────
#   cd api && python start_worker.py --concurrency 60
#
#   60 thread'ов = 20 (MAX_SLOTS_PER_USER) × 3 запас. Реально TG-сессий
#   одновременно будет 20 (упирается в user_slot), остальные thread'ы
#   обслуживают «лёгкие» вызовы (skip/lock-check) без подключения к Telegram.
#
#   Ctrl+C → wrapper делает taskkill /F /T — воркер умирает за <1 сек.
#
# ── Терминал 3: Celery Beat (планировщик) ──────────────────
#   cd api && python -m celery -A celery_app beat --loglevel=info
#
#   Каждые 60 сек кладёт dispatch_plans/process_ai_dialogs в очередь.
#   Beat НЕ выполняет таски — только отправляет.
#   ⚠ Запускать ОДИН экземпляр beat, иначе будут дубли тасков.
#
# ── Терминал 4: Фронтенд ───────────────────────────────────
#   cd gramgpt-web && npm run dev
#
# ── Терминал 5 (опц.): Мониторинг ──────────────────────────
#   cd api && python -m celery -A celery_app flower --port=5555
#   → http://localhost:5555 (видно активные задачи, очереди, fail rate)
#
#
# ═══════════════════════════════════════════════════════════
# РАЗДЕЛЁННЫЙ СЕТАП — если 400 акков жмёт один воркер
# ═══════════════════════════════════════════════════════════
# Запускать ВМЕСТО единого воркера из терминала 2.
# Полезно когда: парсеры/прогрев крутятся одновременно с активными кампаниями,
# и не хочется чтобы долгий парсинг блокировал thread'ы у комментинга.
#
# ── Терминал 2a: ПЛАНЫ (комментинг + прогрев, hot path) ────
#   cd api && python start_worker.py --queues plans --concurrency 60
#
# ── Терминал 2b: ПАРСЕРЫ (долгие задачи) ───────────────────
#   cd api && python start_worker.py --queues parsers --concurrency 8
#
# ── Терминал 2c: AI и всё остальное ────────────────────────
#   cd api && python start_worker.py --queues ai_dialogs,high_priority,bulk_actions,subscribe,warmup --concurrency 20
#
#   Итого 60+8+20=88 thread'ов суммарно. Если ОЗУ напряжена — можно ужать
#   parsers до 4 и misc до 10.
#
#
# ═══════════════════════════════════════════════════════════
# ОСТАНОВКА
# ═══════════════════════════════════════════════════════════
#   • Ctrl+C в окне воркера → таску дают доработать, потом kill (~1 сек)
#   • Если завис — из другого терминала: cd api && python kill_workers.py --hard
#   • Beat остановить отдельно: Ctrl+C в его окне
#
#
# ═══════════════════════════════════════════════════════════
# ЧТО ОЗНАЧАЮТ ПАРАМЕТРЫ
# ═══════════════════════════════════════════════════════════
# --concurrency 60      кол-во thread'ов в воркере = сколько задач параллельно
#                       (но РЕАЛЬНЫХ TG-подключений будет MAX_SLOTS_PER_USER × users)
# -P threads            каждая задача в своём треде с собственным asyncio loop
#                       НЕ менять на gevent (asyncio падает с "running loop already")
# --queues X,Y,Z        какие очереди слушает этот воркер. Без флага — слушает все.
#
# MAX_SLOTS_PER_USER    одновременных TG-сессий на одного юзера платформы (.env)
# MAX_DAILY_CONNECTIONS подключений к одному TG-аккаунту в сутки (.env)
# REDIS_POOL_MAX        потолок redis-коннектов в пуле (.env, дефолт 100)
#
#
# ═══════════════════════════════════════════════════════════
# Закрытые каналы (event listener) — отдельно если нужно
# ═══════════════════════════════════════════════════════════
#   cd api && python run_listener.py