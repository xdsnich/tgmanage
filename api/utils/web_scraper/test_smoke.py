"""
test_smoke.py — лёгкие проверки без Camoufox.

Запуск:
    cd api
    python -m utils.web_scraper.test_smoke

Проверяет:
  - NodeRotator: acquire / release / cooldown / round-robin
  - JSONLCheckpoint: запись / resume / filter_pending
  - retry.backoff_delay: монотонность

НЕ запускает реальный Camoufox (это дорого и требует установки).
Для полноценного теста — запусти job через UI с тестовым URL вроде
https://httpbin.org/headers через 3+ прокси.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Запуск как модуль: python -m utils.web_scraper.test_smoke
# Здесь api/ уже в sys.path благодаря -m
if __name__ == "__main__":
    api_dir = Path(__file__).resolve().parent.parent.parent
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))

from utils.web_scraper.node_rotator import NodeRotator
from utils.web_scraper.checkpoint import JSONLCheckpoint
from utils.web_scraper.retry import backoff_delay


async def test_rotator_basic():
    print("[1/4] NodeRotator: acquire/release/round-robin...")
    proxies = [f"http://user:pass@10.0.0.{i}:8080" for i in range(1, 6)]
    rot = NodeRotator(proxies, cooldown_min_sec=2, cooldown_max_sec=3)

    # Берём 3 узла подряд — должны быть разные
    nodes = []
    for _ in range(3):
        n = await rot.acquire()
        nodes.append(n)
    assert len({n.url for n in nodes}) == 3, "Round-robin должен дать разные узлы"

    # Отпускаем
    for n in nodes:
        await rot.mark_success(n)

    stats = rot.stats()
    assert stats["total"] == 5
    assert stats["busy"] == 0
    assert stats["total_successes"] == 3
    print(f"      OK — stats={stats['available']}/{stats['total']} avail, "
          f"{stats['total_successes']} successes")


async def test_rotator_cooldown():
    print("[2/4] NodeRotator: cooldown работает...")
    proxies = ["http://1.1.1.1:8080", "http://2.2.2.2:8080"]
    rot = NodeRotator(proxies, cooldown_min_sec=1, cooldown_max_sec=1)

    n1 = await rot.acquire()
    await rot.mark_failed(n1, "test fail")

    stats = rot.stats()
    assert stats["in_cooldown"] == 1, f"Один узел должен быть в cooldown, got {stats}"
    assert stats["available"] == 1

    # Ждём истечения cooldown'а
    await asyncio.sleep(1.3)
    stats = rot.stats()
    assert stats["in_cooldown"] == 0, f"Cooldown должен истечь, got {stats}"
    print(f"      OK — cooldown истёк через 1с, в пуле снова {stats['available']} узлов")


async def test_checkpoint_resume():
    print("[3/4] JSONLCheckpoint: запись + resume...")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.jsonl"

        # Первый "запуск" — пишем 3 записи
        cp = JSONLCheckpoint(path)
        await cp.save({"url": "https://a.com", "ok": True})
        await cp.save({"url": "https://b.com", "ok": False})
        await cp.save({"url": "https://c.com", "ok": True})
        assert cp.processed_count == 3

        # "Перезапуск" — новый инстанс читает существующий файл
        cp2 = JSONLCheckpoint(path)
        assert cp2.processed_count == 3
        assert cp2.is_processed("https://a.com")
        assert not cp2.is_processed("https://d.com")

        # filter_pending корректно фильтрует
        pending = cp2.filter_pending(["https://a.com", "https://b.com", "https://d.com"])
        assert pending == ["https://d.com"], f"Ожидали ['https://d.com'], получили {pending}"
        print(f"      OK — 3 записи сохранены, resume пропустил их, новый URL остался")


async def test_backoff_monotonic():
    print("[4/4] retry.backoff_delay: монотонный рост...")
    # full_jitter=False — детерминированный режим для теста
    d1 = await backoff_delay(1, base=0.05, factor=2.0, full_jitter=False)
    d2 = await backoff_delay(2, base=0.05, factor=2.0, full_jitter=False)
    d3 = await backoff_delay(3, base=0.05, factor=2.0, full_jitter=False)
    # Equal-jitter: половина детерминированная, поэтому d3 > d2 > d1 строго
    assert d1 < d2 < d3, f"Ожидали рост, получили {d1:.3f} {d2:.3f} {d3:.3f}"
    # Cap проверим
    d_big = await backoff_delay(10, base=0.05, factor=2.0, max_delay=0.5, full_jitter=False)
    assert d_big <= 0.5, f"Cap должен сработать, got {d_big}"
    print(f"      OK -- delays {d1*1000:.0f} -> {d2*1000:.0f} -> {d3*1000:.0f} ms")


async def main():
    t0 = time.time()
    await test_rotator_basic()
    await test_rotator_cooldown()
    await test_checkpoint_resume()
    await test_backoff_monotonic()
    print(f"\nВсе smoke-тесты прошли за {time.time() - t0:.1f}с")


if __name__ == "__main__":
    asyncio.run(main())
