"""
GramGPT — start_worker.py
Запуск Celery worker с МГНОВЕННЫМ выключением по Ctrl+C.

Проблема которую решает:
  celery -P threads на Windows висит на "Warm shutdown" — потоки залипают
  в C-level чтении Redis (BRPOP), Python не может их прервать сигналом,
  thread.join() при выходе блокируется надолго.

Как решает:
  1. Запускает celery как дочерний процесс в ОТДЕЛЬНОЙ группе процессов
     (CREATE_NEW_PROCESS_GROUP) — чтобы он НЕ получал твой Ctrl+C напрямую
  2. На Ctrl+C этот wrapper ловит KeyboardInterrupt и делает taskkill /F /T
     — форсированно убивает celery и все его потоки МГНОВЕННО

Запуск (вместо прямого celery worker):
  cd api
  python start_worker.py

  # Своя concurrency / очереди:
  python start_worker.py --concurrency 60
  python start_worker.py --queues plans,warmup

Ctrl+C → воркер умирает за <1 секунду.
"""

import sys
import os
import subprocess
import argparse


# Все очереди по умолчанию (простой single-worker сетап)
DEFAULT_QUEUES = "plans,warmup,parsers,ai_dialogs,high_priority,bulk_actions,subscribe"
DEFAULT_CONCURRENCY = 40


def main():
    parser = argparse.ArgumentParser(description="Запуск Celery worker с быстрым shutdown")
    parser.add_argument("--queues", "-Q", default=DEFAULT_QUEUES, help="очереди через запятую")
    parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY, help="кол-во потоков")
    parser.add_argument("--loglevel", "-l", default="info", help="уровень логов")
    parser.add_argument("--pool", "-P", default="threads", help="пул (threads/solo)")
    args = parser.parse_args()

    cmd = [
        sys.executable, "-m", "celery", "-A", "celery_app", "worker",
        "-Q", args.queues,
        "-P", args.pool,
        "-c", str(args.concurrency),
        "--without-gossip", "--without-mingle", "--without-heartbeat",
        "--loglevel", args.loglevel,
    ]

    print("=" * 60)
    print("  GramGPT Worker (быстрый shutdown по Ctrl+C)")
    print(f"  Очереди:     {args.queues}")
    print(f"  Пул:         {args.pool} -c {args.concurrency}")
    print(f"  Ctrl+C       → мгновенное завершение (taskkill /F /T)")
    print("=" * 60)

    # На Windows запускаем celery в ОТДЕЛЬНОЙ группе процессов, чтобы Ctrl+C
    # консоли НЕ доходил до него напрямую — иначе он начнёт свой медленный
    # warm shutdown. Только наш wrapper ловит Ctrl+C и форс-килит дочерний.
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(cmd, creationflags=creationflags)

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[start_worker] Ctrl+C — форсированно убиваю воркер...")
        if os.name == "nt":
            # /F = force, /T = вместе со всем деревом дочерних процессов/потоков
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        print("[start_worker] Воркер остановлен.")
        sys.exit(0)


if __name__ == "__main__":
    main()
