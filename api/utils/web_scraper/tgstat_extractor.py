"""
tgstat_extractor.py — DOM-extractor для tgstat.com.

Что вытаскиваем со страницы рейтинга/категории/гео:
  - username канала (@xxx)
  - title
  - subscribers (int)
  - has_comments (есть ли привязанный чат для обсуждений)
  - category (если видно)
  - country/language (берётся из URL, не из карточки — оно в URL более надёжно)
  - position (порядковый номер в рейтинге если есть)

Подход: вместо хрупкого CSS-селектора одной конкретной разметки —
несколько slot'ов с fallback. TGStat периодически меняет классы,
поэтому защищаемся.

Дополнительно перед чтением:
  - Жмём "Показать ещё" / scroll до подгрузки большего числа карточек
  - Закрываем cookie/баннер если показался
"""

import asyncio
import logging
import random
import re
from typing import Any
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


# JS, который вытаскивает карточки. Выполняется в браузере, возвращает
# list[dict]. Любая ошибка внутри try не валит весь extract — пропускаем
# битую карточку.
_JS_EXTRACT_CARDS = r"""
() => {
  const out = [];
  // Карточки канала. Селектор широкий — на странице есть ссылки
  // на каналы (/channel/@user) и на чаты (/chat/@user). Берём оба и
  // дальше определяем тип по разметке/тексту.
  const anchors = Array.from(document.querySelectorAll('a[href*="/channel/@"], a[href*="/chat/@"]'));
  // Дедуп по username — на одной странице один канал может встретиться
  // в нескольких блоках (рейтинг + "похожие").
  const seen = new Set();

  for (const a of anchors) {
    try {
      const href = a.getAttribute('href') || '';
      const m = href.match(/\/(?:channel|chat)\/(@[A-Za-z0-9_]+)/);
      if (!m) continue;
      const username = m[1];
      if (seen.has(username)) continue;

      // Карточка = ближайший разумный контейнер
      const card = a.closest('.card, .peer-item, .channels-list-item, .row, li, .col-md-6, .col-lg-4') || a.parentElement || a;
      const cardText = (card.innerText || '').replace(/\s+/g, ' ').trim();

      // Заголовок: ищем h6/h5/strong/первый <b> в карточке или alt у img
      let title = null;
      const titleEl = card.querySelector('h6, h5, h4, .card-title, strong, b');
      if (titleEl) title = (titleEl.innerText || '').trim();
      if (!title) {
        const img = card.querySelector('img[alt]');
        if (img) title = (img.getAttribute('alt') || '').trim();
      }

      // Подписчики: ищем число + "subs"/"подп"/"K"/"M" рядом
      // На tgstat обычно отдельный <small>/<span> с цифрой.
      let subscribers = null;
      const subRegex = /([\d\s  .,]+)\s*([KMМКkm])?\s*(?:подп|subs|subscribers|подписчиков|учасн)/i;
      const subMatch = cardText.match(subRegex);
      if (subMatch) {
        let n = parseFloat(subMatch[1].replace(/[\s  ]/g, '').replace(',', '.'));
        const mult = (subMatch[2] || '').toLowerCase();
        if (mult === 'k' || mult === 'к') n *= 1000;
        if (mult === 'm' || mult === 'м') n *= 1_000_000;
        if (!isNaN(n)) subscribers = Math.round(n);
      } else {
        // Фолбэк — самое большое число в карточке
        const nums = [...cardText.matchAll(/(\d[\d\s  .,]*)/g)]
          .map(x => parseFloat(x[1].replace(/[\s  .,]/g, '')))
          .filter(n => !isNaN(n) && n > 100);
        if (nums.length) subscribers = Math.max(...nums);
      }

      // has_comments: тип сущности — /chat/@... ИЛИ карточка содержит
      // явные маркеры (значок чата, текст "комментарии", "обсуждение",
      // "Группа", link на discussion)
      const isChatHref = href.includes('/chat/@');
      const hasCommentText = /коммент|обсужд|чат канала|comments|linked\s*chat/i.test(cardText);
      const hasCommentIcon = !!card.querySelector(
        '[class*="comment"], [class*="discussion"], [class*="chat"], i[class*="bi-chat"]'
      );
      const has_comments = isChatHref || hasCommentText || hasCommentIcon;

      // Категория — ищем badge/label
      let category = null;
      const catEl = card.querySelector('.badge, .label, .tag, .category, [class*="badge-category"]');
      if (catEl) category = (catEl.innerText || '').trim().slice(0, 50);

      // Verified — синяя галка
      const verified = !!card.querySelector('[class*="verified"], .text-primary i[class*="check"], img[alt*="verified" i]');

      // Позиция в рейтинге — если есть номер слева от карточки
      let position = null;
      const posEl = card.querySelector('.position, .rank, .rating-position, .order');
      if (posEl) {
        const p = parseInt((posEl.innerText || '').trim(), 10);
        if (!isNaN(p)) position = p;
      }

      seen.add(username);
      out.push({
        username: username,           // c "@"
        title: title || null,
        subscribers: subscribers,
        has_comments: !!has_comments,
        category: category,
        verified: verified,
        position: position,
        kind: isChatHref ? 'chat' : 'channel',
      });
    } catch (e) {
      // Пропускаем битую карточку, не валим весь extract
    }
  }
  return out;
}
"""


async def _expand_page(page: Any, max_loadmore_clicks: int = 8) -> None:
    """
    Подгружает больше карточек: ищет кнопку "Показать ещё"/"Load more"
    и кликает по ней до 8 раз с паузами. Параллельно скроллит — на
    случай infinite scroll.
    """
    selectors = [
        "button.lm-button", "button.btn-load-more", "a.load-more",
        "button:has-text('Показать ещё')", "button:has-text('Загрузить ещё')",
        "button:has-text('Load more')", "button:has-text('Показати ще')",
        "a:has-text('Показать ещё')",
    ]
    for i in range(max_loadmore_clicks):
        # Скроллим вниз чтобы триггернуть infinite scroll и сделать
        # кнопку "Показать ещё" видимой
        try:
            await page.evaluate(
                "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
            )
        except Exception:
            break
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Пробуем нажать "Показать ещё"
        clicked = False
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible(timeout=500):
                    await btn.click(timeout=2000)
                    clicked = True
                    await asyncio.sleep(random.uniform(2.0, 4.5))
                    break
            except Exception:
                continue
        if not clicked:
            # Кнопок нет — возможно infinite scroll сам подгружает.
            # Если новых карточек после скролла не появилось — выходим.
            try:
                count = await page.evaluate(
                    "document.querySelectorAll('a[href*=\"/channel/@\"], a[href*=\"/chat/@\"]').length"
                )
                if i > 0 and count == _expand_page._last_count:
                    break
                _expand_page._last_count = count
            except Exception:
                break


# Атрибут на функции — чтобы не таскать состояние через nonlocal
_expand_page._last_count = 0


async def _dismiss_banners(page: Any) -> None:
    """Закрывает cookie/баннер если показался — иначе клики могут уйти не туда."""
    try:
        # TGStat иногда показывает cookie-плашку
        for sel in [
            "button:has-text('Принять')",
            "button:has-text('Accept')",
            "button:has-text('OK')",
            ".cookie-accept",
            "[aria-label='Close']",
        ]:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                try:
                    if await btn.is_visible(timeout=300):
                        await btn.click(timeout=1000)
                        await asyncio.sleep(0.5)
                        break
                except Exception:
                    continue
    except Exception:
        pass


def _geo_from_url(url: str) -> dict:
    """
    Угадываем гео/категорию из URL. tgstat.com использует разные паттерны:
      /{lang}                          — все каналы языка
      /ratings/channels                — глобальный рейтинг
      /{lang}/ratings/channels         — рейтинг для языка
      /channel/category/{cat}          — категория
      /?category={cat}&country={c}     — фильтры через query
    """
    try:
        p = urlparse(url)
        host = (p.hostname or "").replace("www.", "")
        path_parts = [x for x in (p.path or "").strip("/").split("/") if x]
        qs = parse_qs(p.query or "")

        out = {"source_url": url, "host": host}

        # Первый сегмент пути часто = язык (ru, ua, en, uz, ...)
        if path_parts:
            seg = path_parts[0]
            if 2 <= len(seg) <= 5 and seg.isalpha() and seg.lower() != "ratings":
                out["lang"] = seg.lower()

        if "category" in path_parts:
            idx = path_parts.index("category")
            if idx + 1 < len(path_parts):
                out["category"] = path_parts[idx + 1]

        # Query params: ?country=ru&category=news
        for key in ("country", "category", "language", "lang"):
            if key in qs and qs[key]:
                out[key] = qs[key][0]

        return out
    except Exception:
        return {"source_url": url}


async def tgstat_channels_extractor(page: Any) -> dict:
    """
    Главная точка входа — extractor для WebScraper.

    Возвращает dict вида:
        {
          "url": ...,           — поставит WebScraper, не здесь
          "geo": {lang/country/category},
          "channels": [{username, title, subscribers, has_comments, ...}, ...],
          "channels_count": N,
        }
    """
    # 1. Если стенд-баннер — закрываем
    await _dismiss_banners(page)

    # 2. Подгружаем больше карточек скроллом + "Показать ещё"
    await _expand_page(page)

    # 3. Извлекаем карточки одним evaluate
    try:
        cards = await page.evaluate(_JS_EXTRACT_CARDS)
    except Exception as e:
        logger.warning(f"[tgstat] extract eval error: {e}")
        cards = []

    geo = _geo_from_url(page.url)

    # 4. Финальный пакет
    return {
        "geo": geo,
        "channels": cards or [],
        "channels_count": len(cards or []),
        "final_url": page.url,
        # На случай если страница вообще пустая — оставим title для отладки
        "page_title": (await page.title()) if cards is not None else None,
    }
