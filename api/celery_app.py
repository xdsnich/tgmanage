"""
GramGPT API — celery_app.py
Конфигурация Celery с Redis брокером
По ТЗ: три очереди — high_priority, bulk_actions, ai_dialogs

ВАЖНО: Beat НЕ используется. Периодические задачи (AI-диалоги, комментинг)
запускаются через отдельный скрипт run_periodic.py
"""

import sys
import os

# Принудительно: api/ первый в path, корень — убрать
API_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, API_DIR)

# Убираем родительскую папку (там конфликтующий config.py)
PARENT_DIR = os.path.dirname(API_DIR)
while PARENT_DIR in sys.path:
    sys.path.remove(PARENT_DIR)

# Сбрасываем кеш если загрузился неправильный config
if 'config' in sys.modules:
    loaded = getattr(sys.modules['config'], '__file__', '')
    if 'api' not in loaded:
        del sys.modules['config']
from celery import Celery
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
        "tasks.warmup_tasks",
        "tasks.warmup_v2",
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
    },

    result_expires=3600,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_track_started=True,

    # НЕТ beat_schedule — периодика через run_periodic.py
)
from celery.signals import worker_process_init

@worker_process_init.connect
def fix_path(**kwargs):
    """При старте каждого воркера — принудительно фиксим path."""
    api_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(api_dir)
    
    # api/ первый
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    
    # Родитель — убрать
    while parent_dir in sys.path:
        sys.path.remove(parent_dir)
    
    # Сбросить кеш config
    if 'config' in sys.modules:
        loaded = getattr(sys.modules['config'], '__file__', '')
        if 'api' not in loaded:
            del sys.modules['config']