"""
GramGPT — services/channel_monitor.py
Гибридный мониторинг каналов:
1. Публичные каналы → веб-парсинг https://t.me/s/username (без аккаунта)
2. Закрытые каналы → Telethon event listener с прокси

Этот модуль отвечает за ОБНАРУЖЕНИЕ новых постов.
Комментирование — отдельная задача.
"""

import re
import logging
import httpx
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ParsedPost:
    post_id: int
    text: str
    has_media: bool
    channel_username: str


async def fetch_latest_posts_web(username: str, last_post_id: int = 0) -> list[ParsedPost]:
    """
    Парсит последние посты публичного канала через https://t.me/s/username
    БЕЗ аккаунта Telegram. Риск бана = 0.
    
    Returns: список новых постов (post_id > last_post_id)
    """
    url = f"https://t.me/s/{username}"
    posts = []

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            })

            if resp.status_code != 200:
                logger.warning(f"[web] @{username}: HTTP {resp.status_code}")
                return []

            html = resp.text

            # Проверяем что это публичный канал (есть посты)
            if 'tgme_widget_message_wrap' not in html:
                logger.info(f"[web] @{username}: не публичный или нет постов")
                return []

            # Парсим посты: data-post="channelname/POST_ID"
            post_pattern = re.compile(
                r'data-post="([^/]+)/(\d+)".*?'
                r'tgme_widget_message_text[^>]*>(.*?)</div>',
                re.DOTALL
            )

            for match in post_pattern.finditer(html):
                ch_name = match.group(1)
                post_id = int(match.group(2))
                raw_text = match.group(3)

                # Убираем HTML теги
                text = re.sub(r'<[^>]+>', '', raw_text).strip()

                if post_id <= last_post_id:
                    continue

                if not text or len(text) < 10:
                    continue

                posts.append(ParsedPost(
                    post_id=post_id,
                    text=text[:2000],
                    has_media='tgme_widget_message_photo' in html or 'tgme_widget_message_video' in html,
                    channel_username=ch_name,
                ))

            logger.info(f"[web] @{username}: найдено {len(posts)} новых постов (после ID {last_post_id})")

    except httpx.TimeoutException:
        logger.warning(f"[web] @{username}: таймаут")
    except Exception as e:
        logger.error(f"[web] @{username}: ошибка {e}")

    return sorted(posts, key=lambda p: p.post_id)


async def is_channel_public(username: str) -> bool:
    """Проверяет доступен ли канал через веб-превью."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://t.me/s/{username}", headers={
                "User-Agent": "Mozilla/5.0"
            })
            return resp.status_code == 200 and 'tgme_widget_message_wrap' in resp.text
    except:
        return False