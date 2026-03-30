"""
GramGPT — run_periodic.py
Планировщик задач. Отправляет РОВНО одну задачу за интервал.
Защита от дублей: не отправляет новую пока предыдущая не завершилась.

cd api && python run_periodic.py
"""

import time
import sys
import os
import redis as redis_lib

sys.path.insert(0, os.path.dirname(__file__))
from celery_app import celery_app

# ── Полная очистка Redis при старте ──────────────────────────
print("🧹 Очищаю Redis...")
try:
    r = redis_lib.Redis()
    for queue in ['ai_dialogs', 'bulk_actions', 'high_priority']:
        r.delete(queue)
    for key in r.keys('celery*'):
        r.delete(key)
    print("✅ Redis чист — старых задач нет")
except Exception as e:
    print(f"⚠ Redis: {e}")

# ── Настройки ────────────────────────────────────────────────
AI_INTERVAL = 60        # AI-диалоги каждые 60 сек
COMMENTING_INTERVAL = 30  # Комментинг каждые 30 сек

print("=" * 50)
print("  GramGPT Scheduler")
print(f"  Комментинг: каждые {COMMENTING_INTERVAL}с")
print(f"  AI-диалоги: каждые {AI_INTERVAL}с")
print("  Ctrl+C для остановки")
print("=" * 50)

last_ai = 0
last_commenting = 0
last_warmup = 0

# Защита от дублей — не отправляем новую задачу пока старая в работе
ai_task_id = None
commenting_task_id = None
warmup_task_id = None

WARMUP_INTERVAL = 300  # Прогрев каждые 5 минут

def is_task_done(task_id):
    """Проверяет завершена ли задача"""
    if not task_id:
        return True
    try:
        result = celery_app.AsyncResult(task_id)
        return result.ready()  # True если SUCCESS/FAILURE/REVOKED
    except:
        return True

try:
    while True:
        now = time.time()

        # Комментинг
        if now - last_commenting >= COMMENTING_INTERVAL:
            if is_task_done(commenting_task_id):
                try:
                    result = celery_app.send_task(
                        "tasks.commenting_tasks.process_campaigns",
                        queue="ai_dialogs",
                    )
                    commenting_task_id = result.id
                    print(f"[{time.strftime('%H:%M:%S')}] → Комментинг")
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] ✗ Комментинг: {e}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ⏳ Комментинг ещё работает, жду...")
            last_commenting = now

        # AI-диалоги
        if now - last_ai >= AI_INTERVAL:
            if is_task_done(ai_task_id):
                try:
                    result = celery_app.send_task(
                        "tasks.ai_tasks.process_ai_dialogs",
                        queue="ai_dialogs",
                    )
                    ai_task_id = result.id
                    print(f"[{time.strftime('%H:%M:%S')}] → AI-диалоги")
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] ✗ AI: {e}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] ⏳ AI ещё работает, жду...")
            last_ai = now

        # Прогрев
        if now - last_warmup >= WARMUP_INTERVAL:
            if is_task_done(warmup_task_id):
                try:
                    result = celery_app.send_task(
                        "tasks.warmup_tasks.process_warmups",
                        queue="ai_dialogs",
                    )
                    warmup_task_id = result.id
                    print(f"[{time.strftime('%H:%M:%S')}] → Прогрев")
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] ✗ Прогрев: {e}")
            last_warmup = now

        time.sleep(5)

except KeyboardInterrupt:
    print("\n👋 Планировщик остановлен")