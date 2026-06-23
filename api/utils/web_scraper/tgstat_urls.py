"""
tgstat_urls.py — генератор URL для TGStat.

ВАЖНО: TGStat'овская реальная структура URL (проверена живо 2026-06-22):

  ✅ /ratings/channels                  — глобальный рейтинг каналов
  ✅ /ratings/chats                     — рейтинг ЧАТОВ (discussion groups)
                                          ← здесь и сидят чаты в которых можно писать
  ✅ /ratings/chats?country=RU          — чаты по стране
  ✅ /channel/@username                 — детальная страница канала
  ✅ /channel/@username/N               — конкретный пост

  ❌ /ratings/channels?sort=discussions — НЕ работает (404)
  ❌ /channels/category/{cat}           — НЕ работает (404)
  ❌ /{lang}/...                        — НЕ работает (404)

Что значит "канал с открытыми комментами":
  В Telegram комментарии под постом канала на самом деле пишутся в
  привязанный discussion group (чат). Если у канала есть linked chat —
  значит на канале можно писать комменты. На TGStat этот discussion
  group имеет отдельную страницу /chat/@xxx и попадает в /ratings/chats.

  Поэтому самый прямой способ получить "каналы с комментами" =
  взять рейтинг чатов и для каждого определить линкованный канал
  (часто по конвенции имени: @xxx_chat → @xxx).
"""

from typing import Iterable
from urllib.parse import urlencode


# ── Гео ────────────────────────────────────────────────────────────
# На TGStat используется ISO country code в верхнем регистре в параметре
# ?country=XX. Проверено вживую: ?country=RU отдаёт 200.
GEO_COUNTRIES: list[dict] = [
    {"code": "RU", "label": "Russia",        "flag": "🇷🇺"},
    {"code": "UA", "label": "Ukraine",       "flag": "🇺🇦"},
    {"code": "BY", "label": "Belarus",       "flag": "🇧🇾"},
    {"code": "KZ", "label": "Kazakhstan",    "flag": "🇰🇿"},
    {"code": "UZ", "label": "Uzbekistan",    "flag": "🇺🇿"},
    {"code": "AZ", "label": "Azerbaijan",    "flag": "🇦🇿"},
    {"code": "DE", "label": "Germany",       "flag": "🇩🇪"},
    {"code": "FR", "label": "France",        "flag": "🇫🇷"},
    {"code": "ES", "label": "Spain",         "flag": "🇪🇸"},
    {"code": "IT", "label": "Italy",         "flag": "🇮🇹"},
    {"code": "PT", "label": "Portugal",      "flag": "🇵🇹"},
    {"code": "PL", "label": "Poland",        "flag": "🇵🇱"},
    {"code": "TR", "label": "Turkey",        "flag": "🇹🇷"},
    {"code": "IN", "label": "India",         "flag": "🇮🇳"},
    {"code": "ID", "label": "Indonesia",     "flag": "🇮🇩"},
    {"code": "VN", "label": "Vietnam",       "flag": "🇻🇳"},
    {"code": "US", "label": "USA",           "flag": "🇺🇸"},
    {"code": "BR", "label": "Brazil",        "flag": "🇧🇷"},
    {"code": "GB", "label": "United Kingdom","flag": "🇬🇧"},
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
    # Если юзер хочет "только с комментариями" — это значит чаты
    # (а не каналы). Переключаем target.
    if only_with_comments and target == "channels":
        target = "chats"

    base_path = "/ratings/chats" if target == "chats" else "/ratings/channels"

    countries_list = [c.upper() for c in (countries or [])]
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url: str):
        if url not in seen:
            seen.add(url)
            urls.append(url)

    # 1. Глобальный (без country)
    if include_global or not countries_list:
        _add(f"https://tgstat.com{base_path}")

    # 2. По странам
    for cc in countries_list:
        _add(f"https://tgstat.com{base_path}?{urlencode({'country': cc})}")

    return urls
