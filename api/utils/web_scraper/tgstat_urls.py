"""
tgstat_urls.py — генератор URL для TGStat.

ВАЖНО (проверено 2026-07-03): TGStat использует ПОДДОМЕНЫ для стран, а
не query-параметр. Country-picker с главной страницы ссылается на:
  https://ir.tgstat.com/ratings/channels?sort=members   — Иран
  https://in.tgstat.com/ratings/channels?sort=members   — Индия
  https://tr.tgstat.com/ratings/channels?sort=members   — Турция
  https://tgstat.ru/en/ratings/channels?sort=members    — Россия (спец-случай)
  https://tgstat.com/ratings/channels                   — глобально

  ❌ /ratings/channels?country=TR — параметр ИГНОРИРУЕТСЯ, возвращает глобал.
     Все country-запросы отдают один и тот же топ ['@blumcrypto','@majors',
     '@ProxyMTProto','@durov','@telegram']. Т.е. фильтр по стране через
     query не работает (проверено 3-мя странами).

Что ещё известно про URL структуру:
  ✅ /ratings/channels                  — глобальный рейтинг каналов
  ✅ /ratings/chats                     — рейтинг ЧАТОВ (discussion groups)
  ✅ /channel/@username                 — детальная страница канала
  ✅ /channel/@username/N               — конкретный пост
  ✅ ?sort=members                      — сортировка по подписчикам
  ❌ /ratings/channels?sort=discussions — не работает
  ❌ /channels/category/{cat}           — не работает

Что значит "канал с открытыми комментами":
  В Telegram комментарии под постом канала на самом деле пишутся в
  привязанный discussion group (чат). Если у канала есть linked chat —
  значит на канале можно писать комменты.
"""

from typing import Iterable
from urllib.parse import urlencode


# ── Гео ────────────────────────────────────────────────────────────
# ТОЛЬКО те страны, для которых у TGStat есть реально работающий
# поддомен (проверено 2026-07-03 через country picker на tgstat.com).
# Для остальных стран URL всё равно возвращает глобальный рейтинг.
#
# Особенности маппинга code → host:
#   RU → tgstat.ru        (не ru.tgstat.com)
#   UA → uk.tgstat.com    (UK, не ua — так у TGStat)
#   KZ → kaz.tgstat.com   (3-буквенный, не kz)
GEO_COUNTRIES: list[dict] = [
    {"code": "RU", "label": "Russia",       "flag": "🇷🇺"},
    {"code": "UA", "label": "Ukraine",      "flag": "🇺🇦"},
    {"code": "BY", "label": "Belarus",      "flag": "🇧🇾"},
    {"code": "KZ", "label": "Kazakhstan",   "flag": "🇰🇿"},
    {"code": "UZ", "label": "Uzbekistan",   "flag": "🇺🇿"},
    {"code": "KG", "label": "Kyrgyzstan",   "flag": "🇰🇬"},
    {"code": "IR", "label": "Iran",         "flag": "🇮🇷"},
    {"code": "CN", "label": "China",        "flag": "🇨🇳"},
    {"code": "IN", "label": "India",        "flag": "🇮🇳"},
    {"code": "ET", "label": "Ethiopia",     "flag": "🇪🇹"},
]


# Категории на TGStat сделаны как пользовательские "подборки" и
# их URL'ы непредсказуемы. Поэтому фильтра по категории нет —
# берём только страны и пагинацию.
GEO_CATEGORIES: list[dict] = []   # legacy совместимость

# Языки тоже не используются (на TGStat нет /{lang}/... URL'ов).
# Поле оставлено чтобы старый фронт не падал на отсутствии ключа.
GEO_LANGUAGES: list[dict] = GEO_COUNTRIES


# ── URL-генерация ──────────────────────────────────────────────────

# Target type:
#   "channels" — обычный рейтинг каналов (/ratings/channels)
#   "chats"    — рейтинг чатов = discussion groups (/ratings/chats)
#                ← вот это и есть "каналы с открытыми комментами"


def _country_host(cc: str) -> str:
    """Возвращает host для страны на TGStat.

    Спец-случаи взяты из country-picker'а на tgstat.com/ratings/channels:
      RU  → tgstat.ru       (не ru.tgstat.com)
      UA  → uk.tgstat.com   (uk, не ua)
      KZ  → kaz.tgstat.com  (3-буквенный, не kz)
    Все остальные — {cc.lower()}.tgstat.com.
    """
    cc = cc.upper()
    if cc == "RU":
        return "tgstat.ru"
    if cc == "UA":
        return "uk.tgstat.com"
    if cc == "KZ":
        return "kaz.tgstat.com"
    return f"{cc.lower()}.tgstat.com"


def build_urls(
    languages: Iterable[str] | None = None,    # legacy совместимость, не используется
    categories: Iterable[str] | None = None,   # legacy совместимость, не используется
    only_with_comments: bool = False,
    pages_per_geo: int = 1,
    include_global: bool = False,
    countries: Iterable[str] | None = None,
    target: str = "channels",                  # "channels" | "chats"
) -> list[str]:
    """
    Собирает финальный список URL'ов TGStat по выбору пользователя.

    only_with_comments=True АВТОМАТИЧЕСКИ переключает target на "chats"
    (потому что чаты на TGStat = discussion groups = места где
    собственно ведутся обсуждения под постами каналов).

    Args:
        countries: список ISO-2 кодов стран (см. GEO_COUNTRIES).
            Если пуст и include_global=True — только глобальный рейтинг.
        pages_per_geo: страниц с каждого среза. У TGStat пагинации в
            явном виде нет (это infinite scroll), поэтому реально
            ставим 1; параметр оставлен для совместимости.
        include_global: добавлять ли URL без фильтра по стране.
        target: "channels" или "chats". "chats" = discussion groups
            (в них пишут комменты под постами канала).

    Returns:
        list уникальных URL.

    Игнорируемые параметры (для обратной совместимости с языко-/
    категорийными вызовами):
        languages, categories — TGStat не поддерживает такие фильтры
        в URL. URL вида /{lang}/channels/category/{cat} возвращают 404.
    """
    # only_with_comments больше НЕ переключает target. Сама фильтрация
    # «канал с открытыми комментами» делается во 2-м проходе пайплайна
    # (Celery таск открывает /channel/@xxx для каждого и проверяет
    # есть ли linked_chat). Здесь только генерируем rating URLs.
    base_path = "/ratings/chats" if target == "chats" else "/ratings/channels"

    countries_list = [c.upper() for c in (countries or [])]
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url: str):
        if url not in seen:
            seen.add(url)
            urls.append(url)

    # 1. Глобальный (без country) — с главного tgstat.com
    if include_global or not countries_list:
        _add(f"https://tgstat.com{base_path}")

    # 2. По странам — каждая страна на своём поддомене.
    # ?sort=members — сортировка по подписчикам (как на country-picker'е)
    for cc in countries_list:
        host = _country_host(cc)
        _add(f"https://{host}{base_path}?sort=members")

    return urls
