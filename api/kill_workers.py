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
    # sys.path: api/ — первым, parent (tg_manager1/ с легаси config.py) — убрать
    api_dir    = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(api_dir)
    sys.path = [p for p in sys.path
                if os.path.normcase(os.path.abspath(p) if p else "") != os.path.normcase(parent_dir)]
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
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
        # Windows — wmic удалён в Win11, используем PowerShell Get-CimInstance
        try:
            ps_cmd = (
                "Get-CimInstance Win32_Process "
                "-Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
                "Where-Object { $_.CommandLine -like '*celery*' } | "
                "Select-Object -ExpandProperty ProcessId"
            )
            r = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_cmd],
                capture_output=True, text=True, timeout=15,
            )

            if r.returncode != 0:
                print(f"  PowerShell вернул код {r.returncode}: {r.stderr.strip()[:200]}")
                return False

            pids = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))

            # Защита: не убиваем себя
            self_pid = os.getpid()
            pids = [p for p in pids if p != self_pid]

            if not pids:
                print("  Celery-процессов не найдено")
                return False

            killed = 0
            for pid in pids:
                kr = subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                                    capture_output=True, text=True)
                if kr.returncode == 0:
                    print(f"  ✓ Убит PID {pid}")
                    killed += 1
                else:
                    print(f"  ✗ Не получилось убить PID {pid}: {kr.stderr.strip()[:120]}")
            print(f"  Убито {killed}/{len(pids)} процессов")
            return killed > 0
        except FileNotFoundError:
            print("  Ошибка: powershell.exe не найден в PATH")
            return False
        except Exception as e:
            print(f"  Ошибка hard kill: {type(e).__name__}: {e}")
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
