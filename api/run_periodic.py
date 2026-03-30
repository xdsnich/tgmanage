"""
GramGPT — run_periodic.py
Простой планировщик вместо Celery Beat.
Отправляет задачи в Celery по расписанию — ровно по одной за интервал.
Никаких накоплений, никаких дублей.

Запуск (терминал 3):
  cd api
  venv\Scripts\activate
  python run_periodic.py
"""

import time
import sys
import os

# Добавляем api/ в path
sys.path.insert(0, os.path.dirname(__file__))

from celery_app import celery_app

# Очищаем Redis от старых задач при старте
try:
    celery_app.control.purge()
    print("🧹 Redis очищен от старых задач")
except:
    pass

# Интервалы (секунды)
AI_DIALOGS_INTERVAL = 60      # Проверка AI-диалогов каждые 60 сек
COMMENTING_INTERVAL = 90      # Проверка кампаний каждые 90 сек

print("=" * 50)
print("  GramGPT Periodic Scheduler")
print(f"  AI-диалоги: каждые {AI_DIALOGS_INTERVAL}с")
print(f"  Комментинг: каждые {COMMENTING_INTERVAL}с")
print("  Ctrl+C для остановки")
print("=" * 50)

last_ai = 0
last_commenting = 0

try:
    while True:
        now = time.time()

        # AI-диалоги
        if now - last_ai >= AI_DIALOGS_INTERVAL:
            try:
                celery_app.send_task(
                    "tasks.ai_tasks.process_ai_dialogs",
                    queue="ai_dialogs",
                )
                print(f"[{time.strftime('%H:%M:%S')}] → AI-диалоги отправлены")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] ✗ AI ошибка: {e}")
            last_ai = now

        # Комментинг
        if now - last_commenting >= COMMENTING_INTERVAL:
            try:
                celery_app.send_task(
                    "tasks.commenting_tasks.process_campaigns",
                    queue="ai_dialogs",
                )
                print(f"[{time.strftime('%H:%M:%S')}] → Комментинг отправлен")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] ✗ Комментинг ошибка: {e}")
            last_commenting = now

        time.sleep(5)  # Проверяем каждые 5 сек, но отправляем по интервалу

except KeyboardInterrupt:
    print("\n👋 Планировщик остановлен")