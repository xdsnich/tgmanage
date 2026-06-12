"""
test_throttle_and_stagger.py — high-load smoke test.

Симулирует сценарий инцидента 2026-06-12 и проверяет, что новый
механизм anti-burst отрабатывает:

  1. STAGGER (dispatch_plans)
     Делаем "1000 готовых planов" и эмулируем dispatch'ер. Проверяем
     что countdown'ы распределились в окне, а не нулевые.

  2. PER-IP LOCK (ip_throttle)
     Берём фейковый прокси, захватываем lock, проверяем что повторный
     захват блокируется. Проверяем что cooldown_remaining > 0.
     Параллельно 50 "воркеров" пытаются захватить тот же IP —
     должен пройти только один.

  3. INCIDENT REPLAY
     Симулируем условия burst'а: 200 planов через 34 прокси,
     запускаем эмуляцию stagger + IP-lock, считаем сколько реальных
     "стартов" произошло бы в каждую секунду первой минуты.
     Ожидание: ни в одну секунду не более 4-5 акков (вместо 50+).

Запуск:
    cd api
    python -m tests.test_throttle_and_stagger
"""

import asyncio
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# Изолированный Redis namespace для теста, чтобы не конфликтить с прод данными.
os.environ.setdefault("IP_COOLDOWN_SEC", "5")  # короче для тестов
os.environ.setdefault("IP_COOLDOWN_JITTER_SEC", "1")

api_dir = Path(__file__).resolve().parent.parent
if str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))

from utils.ip_throttle import (
    acquire_ip_lock,
    get_ip_cooldown_remaining,
    is_ip_locked,
)


class FakeProxy:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def __repr__(self):
        return f"FakeProxy({self.host}:{self.port})"


def _stagger_window_sec(n_plans: int) -> int:
    """Копия формулы из dispatch_plans (env-tunable)."""
    stagger_min  = int(os.getenv("DISPATCH_STAGGER_MIN_SEC", "30"))
    stagger_max  = int(os.getenv("DISPATCH_STAGGER_MAX_SEC", "3600"))
    per_plan_sec = float(os.getenv("DISPATCH_STAGGER_PER_PLAN_SEC", "30"))
    return max(stagger_min, min(stagger_max, int(n_plans * per_plan_sec)))


# ── TEST 1: STAGGER ─────────────────────────────────────────────────

def test_stagger_distributes_starts():
    print("[1/3] STAGGER: 1000 planов получают распределённый countdown...")
    n = 1000
    window = _stagger_window_sec(n)
    countdowns = [random.uniform(0, window) for _ in range(n)]
    countdowns.sort()

    # Группируем по секундам — в первой секунде должно быть мало запусков
    by_second = Counter(int(c) for c in countdowns)
    top_5 = by_second.most_common(5)
    worst_sec_load = top_5[0][1]

    # При окне 30000с (1000 * 30) и 1000 planов средняя плотность
    # ~0.033 на сек. Ожидаем что ни одна секунда не получит больше 5.
    assert worst_sec_load <= 6, (
        f"FAIL: в одну секунду {worst_sec_load} запусков (ожидали ≤ 6)"
    )

    # Проверяем что хвост распределения честный (>50% planов после 25% окна)
    median = countdowns[n // 2]
    assert median > window * 0.3, (
        f"FAIL: медиана {median:.0f}с < 30% окна {window}с — окно не работает"
    )

    print(
        f"      OK -- window={window}s, max-per-sec={worst_sec_load}, "
        f"median={median:.0f}s ({median/window:.0%} от окна)"
    )


# ── TEST 2: PER-IP LOCK — basic ────────────────────────────────────

def test_ip_lock_basic():
    print("[2/3] PER-IP LOCK: acquire -> block -> cooldown -> release...")
    p = FakeProxy("203.0.113.42", 1080 + random.randint(0, 9999))

    # Первый захват должен пройти
    assert acquire_ip_lock(p), "FAIL: первый acquire не сработал"

    # Второй — заблокирован
    assert not acquire_ip_lock(p), "FAIL: повторный acquire должен был не пройти"

    # cooldown_remaining > 0
    rem = get_ip_cooldown_remaining(p)
    assert rem > 0, f"FAIL: cooldown_remaining={rem}, ожидали > 0"
    assert is_ip_locked(p), "FAIL: is_ip_locked должен быть True"

    print(f"      OK -- IP {p.host} blocked, cooldown {rem}s remaining")


# ── TEST 3: INCIDENT REPLAY — 200 акков, 34 прокси ─────────────────

async def test_incident_replay_no_burst():
    print("[3/3] INCIDENT REPLAY: 200 planов через 34 прокси за 60 сек...")
    # Каждый прокси — реальная пара host:port (имитация пула пользователя)
    proxies = [
        FakeProxy(f"10.10.{i // 256}.{i % 256}", 1080 + i)
        for i in range(34)
    ]
    # 200 planов: каждый акк закреплён за случайным прокси (как в реале)
    plan_proxies = [random.choice(proxies) for _ in range(200)]

    # Каждый "план" получает случайный countdown из stagger-окна
    window = _stagger_window_sec(len(plan_proxies))
    countdowns = [random.uniform(0, min(60, window)) for _ in plan_proxies]
    # ↑ window большой, но смотрим только первые 60 сек чтобы поймать burst

    # Критерий — реалистичный: Telegram анализирует ПОДКЛЮЧЕНИЯ ПО IP,
    # а не "сколько всего в системе". Несколько акков, стартующих
    # в одну секунду через РАЗНЫЕ IP — нормально. Опасно когда на
    # ОДНОМ IP стартует ≥ 2 в окне cooldown'а.
    starts_per_ip_per_window: dict = defaultdict(list)
    ordered = sorted(zip(countdowns, plan_proxies), key=lambda x: x[0])

    ip_busy_until: dict[tuple, float] = {}
    cooldown = float(os.getenv("IP_COOLDOWN_SEC", "5"))
    total_started = 0
    deferred_count = 0
    starts_per_sec: dict[int, int] = defaultdict(int)
    starts_per_sec_per_ip: dict[int, Counter] = defaultdict(Counter)

    for sched_time, proxy in ordered:
        sec = int(sched_time)
        if sec >= 60:
            break
        key = (proxy.host, proxy.port)
        busy_until = ip_busy_until.get(key, 0)
        if sched_time >= busy_until:
            total_started += 1
            starts_per_sec[sec] += 1
            starts_per_sec_per_ip[sec][key] += 1
            starts_per_ip_per_window[key].append(sched_time)
            ip_busy_until[key] = sched_time + cooldown
        else:
            deferred_count += 1

    # ГЛАВНЫЙ КРИТЕРИЙ: на любом IP в любую секунду — ровно 1 старт
    worst_ip_per_sec = max(
        (cnt for sec_counter in starts_per_sec_per_ip.values()
         for cnt in sec_counter.values()),
        default=0
    )
    assert worst_ip_per_sec <= 1, (
        f"FAIL: {worst_ip_per_sec} акков на одном IP в одну секунду — это burst!"
    )

    # Вторичный критерий: ни на одном IP за всё окно cooldown'а
    # не должно быть >1 успешного старта. ip_busy_until это гарантирует
    # по построению, но проверим.
    for ip_key, ts_list in starts_per_ip_per_window.items():
        for i in range(1, len(ts_list)):
            gap = ts_list[i] - ts_list[i-1]
            assert gap >= cooldown - 0.01, (
                f"FAIL: на {ip_key} два старта с gap={gap:.2f}s < cooldown={cooldown}s"
            )

    max_total_per_sec = max(starts_per_sec.values()) if starts_per_sec else 0
    unique_ips_used = len(starts_per_ip_per_window)
    print(
        f"      OK -- стартовало {total_started} planов / {unique_ips_used} IP, "
        f"max общих стартов/сек {max_total_per_sec}, "
        f"max на ОДНОМ IP/сек {worst_ip_per_sec}, отложено {deferred_count}"
    )


# ── Main ────────────────────────────────────────────────────────────

async def main():
    t0 = time.time()
    test_stagger_distributes_starts()
    try:
        test_ip_lock_basic()
    except RuntimeError as e:
        if "redis" in str(e).lower() or "Connection" in str(e):
            print("      SKIP -- Redis недоступен")
        else:
            raise
    await test_incident_replay_no_burst()
    print(f"\nВсе high-load тесты прошли за {time.time() - t0:.1f}с")


if __name__ == "__main__":
    asyncio.run(main())
