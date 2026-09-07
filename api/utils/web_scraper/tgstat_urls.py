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

import os
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


# Категории TGStat — /ratings/channels/{slug}?sort=members.
# Проверено live 2026-07-04 через country-picker и дамп ссылок с
# главной. Каждая категория = свой топ-100 (существенно разный).
# Скрейп всех 40 категорий даёт до ~4000 уникальных кандидатов
# только по глобальному tgstat.com. Плюс комбинации со странами.
GEO_CATEGORIES: list[dict] = [
    {"slug": "crypto",         "label": "Криптовалюты",       "icon": "💰"},
    {"slug": "business",       "label": "Бизнес",              "icon": "💼"},
    {"slug": "economics",      "label": "Экономика",           "icon": "📈"},
    {"slug": "marketing",      "label": "Маркетинг",           "icon": "📣"},
    {"slug": "career",         "label": "Карьера",             "icon": "💡"},
    {"slug": "education",      "label": "Образование",         "icon": "🎓"},
    {"slug": "edutainment",    "label": "Обучение и развитие", "icon": "🧠"},
    {"slug": "courses",        "label": "Курсы",               "icon": "📚"},
    {"slug": "books",          "label": "Книги",               "icon": "📖"},
    {"slug": "language",       "label": "Языки",               "icon": "🗣️"},
    {"slug": "tech",           "label": "Технологии",          "icon": "⚙️"},
    {"slug": "apps",           "label": "Приложения",          "icon": "📱"},
    {"slug": "games",          "label": "Игры",                "icon": "🎮"},
    {"slug": "news",           "label": "Новости и СМИ",       "icon": "📰"},
    {"slug": "politics",       "label": "Политика",            "icon": "🏛️"},
    {"slug": "law",            "label": "Право",               "icon": "⚖️"},
    {"slug": "medicine",       "label": "Медицина",            "icon": "🏥"},
    {"slug": "health",         "label": "Здоровье",            "icon": "💪"},
    {"slug": "beauty",         "label": "Красота",             "icon": "💄"},
    {"slug": "food",           "label": "Еда",                 "icon": "🍔"},
    {"slug": "travel",         "label": "Путешествия",         "icon": "✈️"},
    {"slug": "nature",         "label": "Природа",             "icon": "🌳"},
    {"slug": "sport",          "label": "Спорт",               "icon": "⚽"},
    {"slug": "music",          "label": "Музыка",              "icon": "🎵"},
    {"slug": "art",            "label": "Искусство",           "icon": "🎨"},
    {"slug": "design",         "label": "Дизайн",              "icon": "🖌️"},
    {"slug": "handmade",       "label": "Рукоделие",           "icon": "🧵"},
    {"slug": "blogs",          "label": "Блоги",               "icon": "📝"},
    {"slug": "psychology",     "label": "Психология",          "icon": "🧘"},
    {"slug": "esoterics",      "label": "Эзотерика",           "icon": "🔮"},
    {"slug": "entertainment",  "label": "Развлечения",         "icon": "🎉"},
    {"slug": "instagram",      "label": "Instagram",           "icon": "📸"},
    {"slug": "babies",         "label": "Дети",                "icon": "👶"},
    {"slug": "construction",   "label": "Строительство",       "icon": "🏗️"},
    {"slug": "gambling",       "label": "Азартные игры",       "icon": "🎰"},
]   # legacy: возвращает [] у старых клиентов будет через отдельный API

# Языки тоже не используются (на TGStat нет /{lang}/... URL'ов).
# Поле оставлено чтобы старый фронт не падал на отсутствии ключа.
GEO_LANGUAGES: list[dict] = GEO_COUNTRIES


# ── URL-генерация ──────────────────────────────────────────────────

# Target type:
#   "channels" — обычный рейтинг каналов (/ratings/channels)
#   "chats"    — рейтинг чатов = discussion groups (/ratings/chats)
#                ← вот это и есть "каналы с открытыми комментами"


# Хосты, которые проверенно отказывают нашим прокси. Ссылки на них не
# строятся вовсе: один такой домен способен выжечь весь пул узлов, потому
# что каждый его отказ = остывание прокси на 15-20 минут.
DEAD_HOSTS = set(
    h.strip() for h in os.getenv("TGSTAT_DEAD_HOSTS", "tgstat.ru").split(",") if h.strip()
)


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
        # ⚠ 2026-08-26: tgstat.ru отдаёт 403 на КАЖДЫЙ запрос через наши прокси.
        # Замер: 35 попыток — 35 отказов, при том что tgstat.com с тех же узлов
        # дал 35 успехов из 35. Хуже того, каждый 403 отправляет прокси в
        # остывание, и 35 подряд выжгли весь пул из 36 узлов: остальные девять
        # страновых поддоменов остались непройденными, задача встала на
        # no_available_node.
        # Домен исключён из RU_HOSTS_OK, см. build_urls — страна RU просто
        # пропускается, пока не найдётся способ туда достучаться.
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
    categories_list = [c.strip().lower() for c in (categories or []) if c and c.strip()]
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url: str):
        if url not in seen:
            seen.add(url)
            urls.append(url)

    def _paths_for_category(cat: str | None) -> str:
        # /ratings/channels/{slug}?sort=members
        # /ratings/chats — категорий не имеет, только базовый путь
        if target == "chats" or not cat:
            return base_path
        return f"{base_path}/{cat}"

    # 1. Глобальный (без country) — с главного tgstat.com
    if include_global or not countries_list:
        if categories_list:
            for cat in categories_list:
                _add(f"https://tgstat.com{_paths_for_category(cat)}?sort=members")
        else:
            _add(f"https://tgstat.com{base_path}")

    # 2. По странам — каждая страна на своём поддомене.
    # Категории комбинируются: {host}/ratings/channels/{cat}?sort=members
    for cc in countries_list:
        host = _country_host(cc)
        if host in DEAD_HOSTS:
            # Не строим ссылки на заведомо отказывающие домены: каждый их
            # отказ уводит прокси в остывание и в итоге останавливает всю
            # очередь (см. комментарий в _country_host).
            continue
        if categories_list:
            for cat in categories_list:
                _add(f"https://{host}{_paths_for_category(cat)}?sort=members")
        else:
            _add(f"https://{host}{base_path}?sort=members")

    return urls
