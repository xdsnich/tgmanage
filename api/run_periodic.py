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

print("=" * 50)
print("  GramGPT Scheduler (Hybrid)")
print(f"  Комментинг: {COMMENTING_INTERVAL}с (веб + Telethon)")
print(f"  AI-диалоги: {AI_INTERVAL}с")
print(f"  Прогрев:    {WARMUP_INTERVAL}с")
print("=" * 50)

last_ai = last_commenting = last_warmup = 0
ai_tid = commenting_tid = warmup_tid = None

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

        if now - last_warmup >= WARMUP_INTERVAL:
            if done(warmup_tid):
                try:
                    r = celery_app.send_task("tasks.warmup_tasks.process_warmups", queue="ai_dialogs")
                    warmup_tid = r.id; print(f"[{ts}] → Прогрев")
                except Exception as e: print(f"[{ts}] ✗ Прогрев: {e}")
            last_warmup = now

        time.sleep(5)
except KeyboardInterrupt:
    print("\n👋 Стоп")