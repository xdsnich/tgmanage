"""
tgstat_channel_detail.py — extractor для /channel/@xxx детальных страниц.

Цель: для каждого канала определить ЕСТЬ ли у него linked discussion chat
(= открыты ли комменты под постами). На детальной странице канала TGStat
рендерит ровно одну ссылку на /chat/@xxx — это и есть linked discussion.

Возвращает:
  {
    "linked_chat_username": "@xxx_chat" | None,
    "channel_username": "@xxx" — взят из URL текущей страницы,
    "title": "Channel Title" | None,
    "subscribers": int | None,
    "verified": bool,
    "category": str | None,
  }

Если linked_chat_username = None — у канала нет открытых комментов,
запись в итоговый список НЕ попадает.
"""

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_JS_EXTRACT = r"""
() => {
  // 1. Linked discussion chat — ровно одна <a href="/chat/@xxx"> в body
  let linked_chat = null;
  const chatLinks = Array.from(document.querySelectorAll('a[href*="/chat/@"]'));
  for (const a of chatLinks) {
    const m = (a.getAttribute('href') || '').match(/\/chat\/(@[A-Za-z0-9_]+)/);
    if (m) {
      linked_chat = m[1];
      break;
    }
  }

  // 2. Channel username — из URL текущей страницы
  let channel_username = null;
  const um = (location.pathname || '').match(/\/channel\/(@[A-Za-z0-9_]+)/);
  if (um) channel_username = um[1];

  // 3. Title — берём из тега <title> и срезаем " — @xxx — TGStat" суффикс
  let title = null;
  const tt = document.title || '';
  const tm = tt.match(/^Telegram[- ]?channel?\s*"(.+?)"/i);
  if (tm) title = tm[1].trim();
  else if (tt) title = tt.split('—')[0].trim();

  // 4. Subscribers — ищем большое число вблизи "subscribers"/"подписч"
  let subscribers = null;
  const body = (document.body?.innerText || '').replace(/\s+/g, ' ');
  const sm = body.match(/([0-9][0-9 ,.]{2,})\s*(?:subscribers|подписчик|учасн)/i);
  if (sm) {
    const n = parseInt(sm[1].replace(/[\s.,]/g, ''), 10);
    if (!isNaN(n)) subscribers = n;
  }

  // 5. Verified — зелёная рамка вокруг аватара / иконка verified.png
  const verified = !!document.querySelector(
    '.border-success, img[src*="verified"], [class*="verified"]'
  );

  // 6. Category — span с категорией
  let category = null;
  const catEl = document.querySelector(
    '.border.rounded.bg-light, span.bg-light, .badge'
  );
  if (catEl) {
    const t = (catEl.innerText || '').trim();
    if (t && t.length < 60 && !/^\d/.test(t)) category = t;
  }

  return {
    linked_chat_username: linked_chat,
    channel_username: channel_username,
    title: title,
    subscribers: subscribers,
    verified: verified,
    category: category,
  };
}
"""


async def tgstat_channel_detail_extractor(page: Any) -> dict:
    """
    Запускается на странице /channel/@xxx. Возвращает данные канала
    + linked_chat если есть. Если linked_chat=None, у канала комменты
    закрыты.

    Дефолтные UX-задержки (мышь/скролл) не нужны — это лёгкая страница
    без JS-челленджа.
    """
    await asyncio.sleep(0.5)
    try:
        data = await page.evaluate(_JS_EXTRACT)
    except Exception as e:
        logger.warning(f"[channel_detail] eval error: {e}")
        data = {}
    return {
        "kind": "channel_detail",
        "channel_username": data.get("channel_username"),
        "linked_chat_username": data.get("linked_chat_username"),
        "has_open_comments": bool(data.get("linked_chat_username")),
        "title": data.get("title"),
        "subscribers": data.get("subscribers"),
        "verified": bool(data.get("verified")),
        "category": data.get("category"),
        "final_url": page.url,
    }
