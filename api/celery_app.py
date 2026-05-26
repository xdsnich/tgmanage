"""
GramGPT API — celery_app.py
"""
import sys

# Windows console может иметь не-UTF-8 кодировку — принудительно переключаем
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from celery import Celery
import os
from dotenv import load_dotenv

load_dotenv()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

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
    # При Ctrl+C ждём максимум 15 секунд завершения тасков, потом force-kill.
    # Без этого Telethon может висеть в socket.recv() пока сервер не разорвёт.
    worker_shutdown_timeout=15,

    # При потере коннекта к Redis — отменять долгие таски (а не висеть бесконечно)
    worker_cancel_long_running_tasks_on_connection_loss=True,

    # Не держать результаты тасков в Redis дольше нужного
    task_ignore_result=False,
)