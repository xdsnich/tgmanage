"""
checkpoint.py — Persistence через JSONL с потокобезопасной записью.

Почему JSONL, а не CSV/SQLite/Redis:
  - Каждая строка независима — можно дописывать атомарно (одна f.write())
  - Краш в середине строки оставляет только эту строку битой,
    остальные читаются дальше
  - Не нужны транзакции, не нужна схема
  - Легко стримить в Pandas/ETL дальше

Алгоритм resume:
  1. При старте читаем существующий файл, собираем set обработанных URL
  2. Вызывающий код через `filter_pending()` отбрасывает уже сделанные
  3. Дописываем новые результаты в append-режиме

Concurrency: asyncio.Lock на запись. Чтение делается синхронно один раз
при инициализации, потому что мы доверяем что parallel-writer'ов не
существует (один WebScraper = один JSONLCheckpoint).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JSONLCheckpoint:
    """
    JSONL-based чекпоинт.

    key_field указывает поле, по которому определяется уникальность записи
    (по умолчанию "url"). Если запись с таким значением уже есть в файле —
    она считается обработанной, повторно её не запускаем.
    """

    def __init__(self, path: str | Path, key_field: str = "url"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_field = key_field
        self._lock = asyncio.Lock()
        self._processed: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        """Один раз читает файл при инициализации. Битые строки скипает."""
        if not self.path.exists():
            return
        loaded = 0
        broken = 0
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        broken += 1
                        continue
                    key = rec.get(self.key_field)
                    if key:
                        self._processed.add(str(key))
                        loaded += 1
        except Exception as e:
            logger.warning(f"[checkpoint] Не удалось прочитать {self.path}: {e}")
            return

        logger.info(
            f"[checkpoint] {self.path.name}: загружено {loaded} обработанных, "
            f"пропущено {broken} битых строк"
        )

    def is_processed(self, key: str) -> bool:
        return str(key) in self._processed

    def filter_pending(self, keys: list[str]) -> list[str]:
        """Возвращает только те ключи, которые ещё не обработаны."""
        return [k for k in keys if str(k) not in self._processed]

    @property
    def processed_count(self) -> int:
        return len(self._processed)

    async def save(self, record: dict[str, Any]) -> None:
        """
        Дописывает запись в JSONL атомарно (write+flush+fsync).

        fsync на каждый write жертвует производительностью ради
        durability — для скрейпинга 1000 URL ценность не потерять
        результат после краша важнее +5мс на запись.
        """
        key = record.get(self.key_field)
        if key is None:
            raise ValueError(
                f"Запись должна содержать поле '{self.key_field}', получено: {record}"
            )
        line = json.dumps(record, ensure_ascii=False)

        async with self._lock:
            # Файловые операции синхронные — выполняем под locks'ом, чтобы
            # ни один другой воркер не пытался писать одновременно.
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    # На некоторых FS (например, network mounts) fsync
                    # может фейлить — это не критично, flush уже отдал
                    # данные в kernel buffer.
                    pass
            self._processed.add(str(key))

    async def reset(self) -> None:
        """Удаляет файл и сбрасывает in-memory state. Использовать аккуратно."""
        async with self._lock:
            if self.path.exists():
                self.path.unlink()
            self._processed.clear()
