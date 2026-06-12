"""
ux_emulator.py — Эмуляция человеческого поведения на странице.

Что эмулируется и зачем:
  - Длинные паузы между навигацией и чтением (10-30 сек) — потому что
    ни один человек не читает страницу за 50мс. JS-челленджи (Cloudflare
    Turnstile, PerimeterX) запускают трекинг событий с момента загрузки
    и через ~5-10 сек оценивают суммарный score.

  - Случайные движения мыши перед DOM-чтением — большинство anti-bot
    решений снимают MouseEvent с document. Полное отсутствие движений
    мыши = мгновенный fingerprint бота.

  - Плавный (но не идеально равномерный) скроллинг — стандартный сигнал
    "человек дочитал" для скрипт-таггеров. WheelEvent с естественными
    интервалами.

Все случайности с jitter — фиксированные интервалы (например, "ровно
500мс между скроллами") тоже фингерпринтятся.
"""

import asyncio
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


async def human_pause(min_sec: float = 10.0, max_sec: float = 30.0) -> float:
    """Длинная пауза — эмуляция чтения/раздумий пользователя."""
    delay = random.uniform(min_sec, max_sec)
    await asyncio.sleep(delay)
    return delay


async def micro_pause(min_sec: float = 0.3, max_sec: float = 1.5) -> float:
    """Короткая пауза между микро-действиями (между кликом и скроллом)."""
    delay = random.uniform(min_sec, max_sec)
    await asyncio.sleep(delay)
    return delay


async def random_mouse_movement(page: Any, n_moves: int = 4) -> None:
    """
    Серия случайных движений мыши по viewport перед чтением DOM.

    Использует page.mouse.move() с параметром steps — Playwright
    эмулирует промежуточные точки (а не телепортируется), что выглядит
    как реальный moveEvent stream.
    """
    try:
        viewport = await page.evaluate(
            "({w: window.innerWidth, h: window.innerHeight})"
        )
        w = int(viewport.get("w") or 1280)
        h = int(viewport.get("h") or 720)
    except Exception:
        w, h = 1280, 720

    for _ in range(n_moves):
        x = random.randint(50, max(51, w - 50))
        y = random.randint(50, max(51, h - 50))
        try:
            await page.mouse.move(x, y, steps=random.randint(8, 20))
        except Exception:
            # Page closed or detached — выходим тихо
            return
        await asyncio.sleep(random.uniform(0.15, 0.7))


async def smooth_scroll(
    page: Any,
    scroll_steps: int = 6,
    step_min: int = 200,
    step_max: int = 500,
    read_pause_prob: float = 0.25,
) -> None:
    """
    Плавный скроллинг частями вниз с переменной скоростью.

    Логика:
      - На каждом шаге скроллим на (step_min..step_max) пикселей
      - С вероятностью read_pause_prob делаем "паузу на чтение" (2-6 сек)
      - Иначе короткая пауза 0.6-1.8 сек

    Не скроллим до самого низа — реальный пользователь редко долистывает
    до конца, особенно длинные страницы.
    """
    for _ in range(scroll_steps):
        step = random.randint(step_min, step_max)
        try:
            # behavior: 'smooth' включает native CSS scroll-behavior:
            # плавную анимацию вместо jump.
            await page.evaluate(
                f"window.scrollBy({{top: {step}, left: 0, behavior: 'smooth'}})"
            )
        except Exception:
            return

        if random.random() < read_pause_prob:
            await asyncio.sleep(random.uniform(2.0, 6.0))
        else:
            await asyncio.sleep(random.uniform(0.6, 1.8))


async def simulate_pre_read_session(page: Any) -> None:
    """
    Полная "до-чтение" последовательность: мышь → скролл → длинная пауза.

    Вызывать после page.goto() и перед extraction. Это обязательный
    минимум для прохождения JS-челленджей на статичных IP.
    """
    await micro_pause(0.5, 2.0)
    await random_mouse_movement(page, n_moves=random.randint(3, 6))
    await smooth_scroll(page, scroll_steps=random.randint(4, 7))
    await human_pause(min_sec=10.0, max_sec=30.0)
