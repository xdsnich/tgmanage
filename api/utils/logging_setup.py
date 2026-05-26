"""
GramGPT — utils/logging_setup.py
Централизованная настройка логирования с ротацией.

Зачем: при 40 параллельных тасках без ротации логов диск кончится за неделю.
RotatingFileHandler автоматически перекидывает файл при достижении лимита.

Использование:
  # В начале процесса (api/celery_app.py, main.py, run_periodic.py):
  from utils.logging_setup import setup_logging
  setup_logging("api")     # → logs/api.log
  setup_logging("celery")  # → logs/celery.log
  setup_logging("beat")    # → logs/beat.log

Параметры через env:
  LOG_LEVEL       — уровень в файл (default INFO)
  LOG_MAX_BYTES   — размер одного файла (default 100MB)
  LOG_BACKUP_COUNT — сколько архивных файлов хранить (default 10)
  LOG_DIR         — куда писать (default <api>/../logs)
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler


# Уровень в файл — настраивается через env
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(100 * 1024 * 1024)))  # 100 MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "10"))             # 100MB × 10 = 1GB max

# По умолчанию logs/ рядом с api/ (т.е. в корне проекта)
_API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(_API_DIR), "logs"))


_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(name: str, level: str = None) -> logging.Logger:
    """
    Конфигурирует root logger:
      - INFO+ → файл logs/<name>.log с ротацией 100MB × 10
      - WARNING+ → stderr (видно в терминале при отладке)

    Идемпотентно: повторный вызов не дублирует handlers.
    Возвращает root logger.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    effective_level = (level or LOG_LEVEL).upper()
    root.setLevel(getattr(logging, effective_level, logging.INFO))

    # Очищаем старые handlers если уже были (для re-init)
    for h in list(root.handlers):
        if getattr(h, "_gramgpt_setup", False):
            root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # 1. Файл с ротацией
    log_file = os.path.join(LOG_DIR, f"{name}.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, effective_level, logging.INFO))
    file_handler.setFormatter(formatter)
    file_handler._gramgpt_setup = True
    root.addHandler(file_handler)

    # 2. stderr — только WARNING+ чтобы не засорять терминал
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(formatter)
    stream_handler._gramgpt_setup = True
    root.addHandler(stream_handler)

    # Snuff out chatty libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    root.info(
        f"[logging] {name} → {log_file} "
        f"(level={effective_level}, rotation={LOG_MAX_BYTES // (1024 * 1024)}MB × {LOG_BACKUP_COUNT})"
    )
    return root
