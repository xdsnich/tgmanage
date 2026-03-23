"""
GramGPT API — celery_app.py
Конфигурация Celery с Redis брокером
По ТЗ: три очереди — high_priority, bulk_actions, ai_dialogs
"""

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
    ]
)

celery_app.conf.update(
    # Сериализация
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Таймзона
    timezone="UTC",
    enable_utc=True,

    # Очереди по ТЗ
    task_routes={
        "tasks.account_tasks.*": {"queue": "high_priority"},
        "tasks.proxy_tasks.*":   {"queue": "high_priority"},
        "tasks.bulk_tasks.*":    {"queue": "bulk_actions"},
        "tasks.ai_tasks.*":      {"queue": "ai_dialogs"},
    },

    # Результаты хранить 1 час
    result_expires=3600,

    # Максимум задач на воркер (защита от перегрузки)
    worker_prefetch_multiplier=1,
    task_acks_late=True,

    # Прогресс задач
    task_track_started=True,
)
