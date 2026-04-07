"""
GramGPT — run_periodic.py
Планировщик задач. Защита от дублей.
cd api && python run_periodic.py
"""

import time, sys, os
import redis as redis_lib

sys.path.insert(0, os.path.dirname(__file__))
from celery_app import celery_app

print("🧹 Очищаю Redis...")
try:
    r = redis_lib.Redis()
    for q in ['ai_dialogs', 'bulk_actions', 'high_priority']:
        r.delete(q)
    for k in r.keys('celery*'):
        r.delete(k)
    print("✅ Redis чист")
except Exception as e:
    print(f"⚠ Redis: {e}")

AI_INTERVAL = 60
COMMENTING_INTERVAL = 90  # Веб-парсинг безопаснее, 90с достаточно
WARMUP_INTERVAL = 300
WARMUP_V2_INTERVAL = 60
COMMENT_EXECUTOR_INTERVAL = 60  # Обработка очереди комментариев

print("=" * 50)
print("  GramGPT Scheduler (Hybrid)")
print(f"  Комментинг:     {COMMENTING_INTERVAL}с (веб → очередь)")
print(f"  Executor:        {COMMENT_EXECUTOR_INTERVAL}с (очередь → отправка)")
print(f"  AI-диалоги:      {AI_INTERVAL}с")
print(f"  Прогрев v2:      {WARMUP_V2_INTERVAL}с")
print("=" * 50)

last_ai = last_commenting = last_warmup = last_warmup_v2 = last_executor = 0
ai_tid = commenting_tid = warmup_tid = warmup_v2_tid = executor_tid = None

def done(tid):
    if not tid: return True
    try: return celery_app.AsyncResult(tid).ready()
    except: return True

try:
    while True:
        now = time.time()
        ts = time.strftime('%H:%M:%S')

        if now - last_commenting >= COMMENTING_INTERVAL:
            if done(commenting_tid):
                try:
                    r = celery_app.send_task("tasks.commenting_tasks.process_campaigns", queue="ai_dialogs")
                    commenting_tid = r.id; print(f"[{ts}] → Комментинг")
                except Exception as e: print(f"[{ts}] ✗ Комментинг: {e}")
            else: print(f"[{ts}] ⏳ Комментинг работает...")
            last_commenting = now

        if now - last_ai >= AI_INTERVAL:
            if done(ai_tid):
                try:
                    r = celery_app.send_task("tasks.ai_tasks.process_ai_dialogs", queue="ai_dialogs")
                    ai_tid = r.id; print(f"[{ts}] → AI-диалоги")
                except Exception as e: print(f"[{ts}] ✗ AI: {e}")
            last_ai = now

        if now - last_executor >= COMMENT_EXECUTOR_INTERVAL:
            if done(executor_tid):
                try:
                    r = celery_app.send_task("tasks.comment_executor.process_comment_queue", queue="ai_dialogs")
                    executor_tid = r.id; print(f"[{ts}] → Executor (очередь)")
                except Exception as e: print(f"[{ts}] ✗ Executor: {e}")
            last_executor = now

        if now - last_warmup_v2 >= WARMUP_V2_INTERVAL:
            if done(warmup_v2_tid):
                try:
                    r = celery_app.send_task("tasks.warmup_v2.process_warmups_v2", queue="ai_dialogs")
                    warmup_v2_tid = r.id; print(f"[{ts}] → Прогрев v2")
                except Exception as e: print(f"[{ts}] ✗ Прогрев v2: {e}")
            last_warmup_v2 = now

        time.sleep(5)
except KeyboardInterrupt:
    print("\n👋 Стоп")