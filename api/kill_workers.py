"""
GramGPT — kill_workers.py
Аварийный убийца зависших Celery воркеров.

Использование:
  cd api

  # Мягкий вариант — через Celery broker (graceful)
  python kill_workers.py

  # Жёсткий — через taskkill/pkill (если broker не отвечает)
  python kill_workers.py --hard

Зачем: при -P threads python-треды Celery нельзя убить извне когда они в
C-level network I/O (Telethon socket). Ctrl+C может ждать минуту+.
Этот скрипт даёт быстрый аварийный выход.
"""

import sys
import os
import argparse
import subprocess


def graceful_shutdown():
    """Отправляет broadcast 'shutdown' через broker всем воркерам."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from celery_app import celery_app
    except ImportError as e:
        print(f"ОШИБКА: не могу импортировать celery_app: {e}")
        return False

    try:
        # control.shutdown отправляет команду через Redis
        result = celery_app.control.broadcast("shutdown", reply=True, timeout=5)
        if result:
            for r in result:
                print(f"  Воркер ответил: {r}")
            return True
        else:
            print("  Никто не ответил (воркеров нет или broker недоступен)")
            return False
    except Exception as e:
        print(f"  Ошибка broker shutdown: {e}")
        return False


def hard_kill():
    """Force-kill всех Python-процессов где в командной строке есть 'celery'."""
    if os.name == 'nt':
        # Windows
        # Берём список процессов через wmic, фильтруем по celery в commandline
        try:
            r = subprocess.run(
                ['wmic', 'process', 'where',
                 'name="python.exe" and CommandLine like "%celery%"',
                 'get', 'ProcessId'],
                capture_output=True, text=True, timeout=10,
            )
            pids = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))

            if not pids:
                print("  Celery-процессов не найдено")
                return False

            for pid in pids:
                print(f"  Убиваю PID {pid}...")
                subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                               capture_output=True)
            print(f"  Убито {len(pids)} процессов")
            return True
        except Exception as e:
            print(f"  Ошибка hard kill: {e}")
            return False
    else:
        # Linux/Mac
        try:
            r = subprocess.run(['pkill', '-9', '-f', 'celery'],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print("  Все celery-процессы убиты")
                return True
            elif r.returncode == 1:
                print("  Celery-процессов не найдено")
                return False
            else:
                print(f"  pkill вернул код {r.returncode}: {r.stderr}")
                return False
        except Exception as e:
            print(f"  Ошибка hard kill: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description="Убить Celery воркеров")
    parser.add_argument("--hard", action="store_true",
                        help="taskkill/pkill вместо graceful shutdown")
    args = parser.parse_args()

    if args.hard:
        print("Hard kill — убиваю все celery-процессы...")
        ok = hard_kill()
    else:
        print("Graceful shutdown через Celery broker...")
        ok = graceful_shutdown()
        if not ok:
            print("\nНе получилось через broker. Попробуй --hard:")
            print("  python kill_workers.py --hard")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
