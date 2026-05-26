"""
GramGPT API — celery_app.py
"""
import sys
import os

# ── ВАЖНО: sys.path-cleanup ДОЛЖЕН быть до любого 'from config import ...' ──
# В корне репо лежит легаси tg_manager1/config.py без DATABASE_URL.
# Если PYTHONPATH/IDE/.pth кладёт parent dir в sys.path раньше api/,
# Python берёт легаси и таски падают на импорте database.py.
_API_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_API_DIR)
sys.path[:] = [p for p in sys.path
               if os.path.normcase(os.path.abspath(p) if p else os.getcwd()) != os.path.normcase(_PARENT_DIR)]
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)
# Сбрасываем кеш модулей если config уже был импортирован из неправильного места
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

from celery import Celery
from celery.signals import worker_process_init, beat_init
import os
from dotenv import load_dotenv

load_dotenv()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Logging с ротацией ──────────────────────────────────────
# Подключаем при старте процесса (worker или beat).
# logs/celery.log + logs/beat.log с ротацией 100MB × 10 файлов.
@worker_process_init.connect
def _setup_worker_logging(**_):
    from utils.logging_setup import setup_logging
    setup_logging("celery")


@beat_init.connect
def _setup_beat_logging(**_):
    from utils.logging_setup import setup_logging
    setup_logging("beat")

celery_app = Celery(
    "gramgpt",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "tasks.account_tasks",
        "tasks.proxy_tasks",
        "tasks.bulk_tasks",
        "tasks.ai_tasks",
        "tasks.commenting_tasks",
        "tasks.comment_executor",
        "tasks.warmup_tasks",
        "tasks.warmup_v2",
        "tasks.subscribe_tasks",
        "tasks.plan_executor",
        "tasks.parser_tasks",
        "tasks.parser_similar_tasks",   # ← ДОБАВЛЕНО: crawler похожих каналов
    ]
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    task_routes={
        # Быстрые юзер-инициированные операции
        "tasks.account_tasks.*":         {"queue": "high_priority"},
        "tasks.proxy_tasks.*":           {"queue": "high_priority"},

        # Bulk-операции (импорт TData, 2FA batch и т.п.)
        "tasks.bulk_tasks.*":            {"queue": "bulk_actions"},

        # ПЛАНЫ КАМПАНИЙ — самые важные, отдельная очередь
        # для предсказуемой latency. Дальше всё масштабируется по ней.
        "tasks.plan_executor.*":         {"queue": "plans"},

        # Прогрев аккаунтов — отдельная очередь
        "tasks.warmup_v2.*":             {"queue": "warmup"},
        "tasks.warmup_tasks.*":          {"queue": "warmup"},

        # Парсеры — могут быть долгими (crawler до 10 мин), изолируем
        # чтобы не блокировали planning/commenting
        "tasks.parser_tasks.*":          {"queue": "parsers"},
        "tasks.parser_similar_tasks.*":  {"queue": "parsers"},

        # Подписки — отдельно, бывают long-running
        "tasks.subscribe_tasks.*":       {"queue": "subscribe"},

        # AI-диалоги и комментинг
        "tasks.ai_tasks.*":              {"queue": "ai_dialogs"},
        "tasks.commenting_tasks.*":      {"queue": "ai_dialogs"},
        "tasks.comment_executor.*":      {"queue": "ai_dialogs"},
    },

    result_expires=3600,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_track_started=True,

    # ── Тайм-ауты тасков ─────────────────────────────────────
    # Hard kill: таск убивается после 10 мин (защита от вечно висящих)
    task_time_limit=600,
    # Soft warning: SoftTimeLimitExceeded поднимается на 8-й минуте — можно почистить ресурсы
    task_soft_time_limit=480,

    # ── Быстрый shutdown ─────────────────────────────────────
    # При Ctrl+C ждём максимум 5 секунд, потом force-kill всех тредов.
    # Без этого Celery+threads на Windows может зависать на minutes пока
    # redis.brpop в C-level вернётся (треды нельзя прервать сигналом).
    worker_shutdown_timeout=5,

    # При потере коннекта к Redis — отменять долгие таски (а не висеть бесконечно)
    worker_cancel_long_running_tasks_on_connection_loss=True,

    # Не слать события задач в broker — экономит Redis-трафик и ускоряет shutdown
    # (на dev можно False, на prod = True если используешь Flower с full features)
    worker_send_task_events=True,
    task_send_sent_event=False,

    # Не держать результаты тасков в Redis дольше нужного
    task_ignore_result=False,

    # ── Beat schedule — заменяет run_periodic.py ─────────────
    # Beat = встроенный планировщик Celery с supervisor-friendly архитектурой.
    # Запуск: celery -A celery_app beat --loglevel=info
    # Beat НЕ выполняет таски сам — он только посылает их в очередь воркеру
    # по расписанию. Так что Beat и Worker — это два РАЗНЫХ процесса.
    beat_schedule={
        "dispatch-plans": {
            "task": "tasks.plan_executor.dispatch_plans",
            "schedule": 60.0,                  # каждые 60 секунд
            "options": {"queue": "plans"},
        },
        "dispatch-warmups": {
            "task": "tasks.warmup_v2.dispatch_warmups",
            "schedule": 60.0,
            "options": {"queue": "warmup"},
        },
        "process-ai-dialogs": {
            "task": "tasks.ai_tasks.process_ai_dialogs",
            "schedule": 60.0,
            "options": {"queue": "ai_dialogs"},
        },
    },
)