"""
GramGPT API — celery_app.py
"""

from celery import Celery
import os
from dotenv import load_dotenv

load_dotenv()
from config import DATABASE_URL
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
    ]
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    task_routes={
        "tasks.account_tasks.*":    {"queue": "high_priority"},
        "tasks.proxy_tasks.*":      {"queue": "high_priority"},
        "tasks.bulk_tasks.*":       {"queue": "bulk_actions"},
        "tasks.ai_tasks.*":         {"queue": "ai_dialogs"},
        "tasks.commenting_tasks.*": {"queue": "ai_dialogs"},
        "tasks.comment_executor.*": {"queue": "ai_dialogs"},
    },

    result_expires=3600,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_track_started=True,
)