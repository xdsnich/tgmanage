"""
GramGPT — run_periodic.py  ⚠ DEPRECATED — используй Celery Beat

Старый планировщик на while + sleep. Без supervisor, упадёт ночью —
никто не узнает. Заменён Celery Beat (см. celery_app.py beat_schedule).

Новый запуск планировщика:
  cd api && celery -A celery_app beat --loglevel=info

НЕ запускай run_periodic.py И beat одновременно — таски будут дублироваться.
Этот файл оставлен как fallback на случай проблем с Beat.
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
COMMENTING_INTERVAL = 90
WARMUP_DISPATCH_INTERVAL = 60
COMMENT_DISPATCH_INTERVAL = 60
PLAN_DISPATCH_INTERVAL = 60
last_plans = 0
plans_tid = None

print("=" * 50)
print("  GramGPT Scheduler (Parallel)")
print(f"  Комментинг:       {COMMENTING_INTERVAL}с (парсинг → очередь)")
print(f"  Warmup dispatch:   {WARMUP_DISPATCH_INTERVAL}с (→ параллельные задачи)")
print(f"  Comment dispatch:  {COMMENT_DISPATCH_INTERVAL}с (→ параллельные задачи)")
print(f"  AI-диалоги:        {AI_INTERVAL}с")
print("=" * 50)

last_ai = last_commenting = last_warmup = last_comments = 0
ai_tid = commenting_tid = warmup_tid = comments_tid = None

def done(tid):
    if not tid: return True
    try: return celery_app.AsyncResult(tid).ready()
    except: return True

try:
    while True:
        now = time.time()
        ts = time.strftime('%H:%M:%S')

        # Парсинг каналов → очередь комментариев
    

        # Диспетчер прогрева → параллельные задачи по аккаунтам
        if now - last_warmup >= WARMUP_DISPATCH_INTERVAL:
            if done(warmup_tid):
                try:
                    r = celery_app.send_task("tasks.warmup_v2.dispatch_warmups", queue="warmup")
                    warmup_tid = r.id; print(f"[{ts}] → Warmup dispatch")
                except Exception as e: print(f"[{ts}] ✗ Warmup: {e}")
            last_warmup = now


        # Диспетчер планов кампаний → параллельные сессии
        if now - last_plans >= PLAN_DISPATCH_INTERVAL:
            if done(plans_tid):
                try:
                    r = celery_app.send_task("tasks.plan_executor.dispatch_plans", queue="plans")
                    plans_tid = r.id; print(f"[{ts}] → Plan dispatch")
                except Exception as e: print(f"[{ts}] ✗ Plans: {e}")
            last_plans = now

        # AI-диалоги
        if now - last_ai >= AI_INTERVAL:
            if done(ai_tid):
                try:
                    r = celery_app.send_task("tasks.ai_tasks.process_ai_dialogs", queue="ai_dialogs")
                    ai_tid = r.id
                except: pass
            last_ai = now

        time.sleep(5)
except KeyboardInterrupt:
    print("\n👋 Стоп")