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
  // ── 0. DEAD-DETECTION ─────────────────────────────────────
  // TGStat отдаёт fallback-страницу с текстом "Channel not found - 404" /
  // "Такой канал не найден" / "Page does not exist - 404" когда:
  //   а) канал удалён из Telegram (Telegram выкинул 404)
  //   б) канал приватный и TGStat не имеет доступа
  //   в) username занят другим сервисом
  // Но при этом TGStat может держать в своём кеше subscribers, linked_chat,
  // и т.д. Если мы этого не проверим — запишем «канал 404» как живой.
  //
  // Признаки dead-страницы:
  //   - <title> содержит "Channel not found" / "не найден" / "404" / "not exist"
  //   - <body> содержит характерный текст ошибки
  //   - h1/h2 содержит "404" / "Not Found"
  const title_raw = (document.title || '').trim();
  const body_text = (document.body?.innerText || '').slice(0, 3000);
  const h_text = Array.from(document.querySelectorAll('h1, h2'))
    .map(h => (h.innerText || '').trim())
    .join(' | ')
    .slice(0, 500);

  const dead_patterns = [
    /channel not found/i,
    /такой канал не найден/i,
    /page does not exist/i,
    /страница не существует/i,
    /канал не найден/i,
    /канал был удалён/i,
    /channel was deleted/i,
    /\b404\b/,   // просто «404» в title или h1
  ];
  let is_dead = false;
  let dead_reason = null;
  for (const p of dead_patterns) {
    if (p.test(title_raw)) { is_dead = true; dead_reason = 'title:' + p.source; break; }
    if (p.test(h_text))    { is_dead = true; dead_reason = 'h1:' + p.source; break; }
  }
  // Body-check только по явно фраза (в body бывает много слов)
  if (!is_dead) {
    if (/channel not found - ?404/i.test(body_text)) { is_dead = true; dead_reason = 'body_cn404'; }
    else if (/page does not exist - ?404/i.test(body_text)) { is_dead = true; dead_reason = 'body_pn404'; }
  }
  // Позитивный признак живой страницы — наличие блока со статистикой канала.
  // Если is_dead === true, но при этом есть характерный TGStat-виджет, это может
  // быть false positive (например, "404" в тексте поста). Оверрайдим:
  const has_channel_widget = !!document.querySelector(
    '.channel_stats, [class*="channel_avatar"], [href*="/channel/@"][class*="channel"]'
  );
  // Не оверрайдим — если title явно 404, значит это dead независимо от виджетов

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
  const body = body_text.replace(/\s+/g, ' ');
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
    is_dead: is_dead,
    dead_reason: dead_reason,
    has_channel_widget: has_channel_widget,
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

    is_dead = bool(data.get("is_dead"))
    ch_username = data.get("channel_username")
    if is_dead:
        logger.info(
            f"[channel_detail] DEAD channel: {ch_username} "
            f"(reason: {data.get('dead_reason')})"
        )

    return {
        "kind": "channel_detail",
        "channel_username": ch_username,
        # Ключевое: если канал мёртвый, has_open_comments=False независимо
        # от того, что TGStat закешировал старый linked_chat.
        "linked_chat_username": None if is_dead else data.get("linked_chat_username"),
        "has_open_comments": (not is_dead) and bool(data.get("linked_chat_username")),
        "is_dead": is_dead,
        "dead_reason": data.get("dead_reason"),
        "title": data.get("title"),
        "subscribers": data.get("subscribers"),
        "verified": bool(data.get("verified")),
        "category": data.get("category"),
        "final_url": page.url,
    }
