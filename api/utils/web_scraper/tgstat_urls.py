"""
tgstat_urls.py — генератор URL для TGStat по гео и категориям.

TGStat использует разные паттерны URL для разных срезов:
  - Главный рейтинг:            https://tgstat.com/ratings/channels
  - Рейтинг по языку:           https://tgstat.com/{lang}/ratings/channels
  - Категория глобально:        https://tgstat.com/channels/category/{cat}
  - Категория по языку:         https://tgstat.com/{lang}/channels/category/{cat}
  - Раздел "обсуждаемое":       https://tgstat.com/ratings/channels?sort=discussions
                                                                              ^ комменты

Здесь — справочник доступных стран/языков/категорий и функция build_urls,
которая собирает финальный список URL'ов по выбору пользователя.

Списки взяты из публичного интерфейса TGStat — стандартный набор для
русско/восточно-европейской аудитории + основные европейские и азиатские.
Если конкретный gear не работает на стороне TGStat — он просто отдаст
404 и узел этот URL пропустит без фатала.
"""

from typing import Iterable
from urllib.parse import urlencode


# ── Языки/гео ──────────────────────────────────────────────────────
# Код, который TGStat использует в первом сегменте пути (или country=).
# label — для UI.
GEO_LANGUAGES: list[dict] = [
    {"code": "ru", "label": "Russia / Russian",     "flag": "🇷🇺"},
    {"code": "uk", "label": "Ukraine / Ukrainian",  "flag": "🇺🇦"},
    {"code": "by", "label": "Belarus / Belarusian", "flag": "🇧🇾"},
    {"code": "kz", "label": "Kazakhstan",            "flag": "🇰🇿"},
    {"code": "uz", "label": "Uzbekistan",            "flag": "🇺🇿"},
    {"code": "az", "label": "Azerbaijan",            "flag": "🇦🇿"},
    {"code": "en", "label": "English (global)",      "flag": "🇬🇧"},
    {"code": "de", "label": "Germany / German",      "flag": "🇩🇪"},
    {"code": "fr", "label": "France / French",       "flag": "🇫🇷"},
    {"code": "es", "label": "Spain / Spanish",       "flag": "🇪🇸"},
    {"code": "it", "label": "Italy / Italian",       "flag": "🇮🇹"},
    {"code": "pt", "label": "Portugal / Portuguese", "flag": "🇵🇹"},
    {"code": "pl", "label": "Poland / Polish",       "flag": "🇵🇱"},
    {"code": "tr", "label": "Turkey / Turkish",      "flag": "🇹🇷"},
    {"code": "ar", "label": "Arabic",                "flag": "🇸🇦"},
    {"code": "in", "label": "India / Hindi",         "flag": "🇮🇳"},
    {"code": "id", "label": "Indonesia",             "flag": "🇮🇩"},
    {"code": "vn", "label": "Vietnam",               "flag": "🇻🇳"},
    {"code": "us", "label": "USA",                   "flag": "🇺🇸"},
    {"code": "br", "label": "Brazil / Portuguese",   "flag": "🇧🇷"},
]


# Категории TGStat. URL-slug может различаться, пробую общеупотребимые.
GEO_CATEGORIES: list[dict] = [
    {"code": "news",          "label": "Новости и СМИ"},
    {"code": "politics",      "label": "Политика"},
    {"code": "economics",     "label": "Экономика, бизнес"},
    {"code": "cryptocurrencies", "label": "Криптовалюты"},
    {"code": "technologies",  "label": "Технологии"},
    {"code": "marketing-pr",  "label": "Маркетинг и PR"},
    {"code": "sales",         "label": "Продажи"},
    {"code": "education",     "label": "Образование"},
    {"code": "telegram",      "label": "Telegram"},
    {"code": "entertainment", "label": "Развлечения"},
    {"code": "music",         "label": "Музыка"},
    {"code": "movies-series", "label": "Кино, сериалы"},
    {"code": "books",         "label": "Книги"},
    {"code": "art-design",    "label": "Искусство, дизайн"},
    {"code": "sport",         "label": "Спорт"},
    {"code": "travels",       "label": "Путешествия"},
    {"code": "fashion-beauty", "label": "Мода и красота"},
    {"code": "health-medicine", "label": "Здоровье, медицина"},
    {"code": "psychology",    "label": "Психология"},
    {"code": "religion",      "label": "Религия"},
    {"code": "humor",         "label": "Юмор"},
    {"code": "games",         "label": "Игры"},
    {"code": "cars",          "label": "Авто и мото"},
    {"code": "food-cooking",  "label": "Еда и кулинария"},
    {"code": "linguistics",   "label": "Лингвистика, языки"},
]


# Какие шаблоны URL мы генерируем. Каждый — функция, принимающая
# (lang, category, sort, page). Возвращает абсолютный URL.
def _url_rating_global(lang: str | None, category: str | None,
                       sort: str | None, page: int) -> str:
    """https://tgstat.com/ratings/channels (или с ?sort=&page=)"""
    base = "https://tgstat.com/ratings/channels"
    qs = {}
    if sort:
        qs["sort"] = sort
    if page > 1:
        qs["page"] = page
    return f"{base}?{urlencode(qs)}" if qs else base


def _url_rating_by_lang(lang: str | None, category: str | None,
                        sort: str | None, page: int) -> str | None:
    """https://tgstat.com/{lang}/ratings/channels"""
    if not lang:
        return None
    base = f"https://tgstat.com/{lang}/ratings/channels"
    qs = {}
    if sort:
        qs["sort"] = sort
    if page > 1:
        qs["page"] = page
    return f"{base}?{urlencode(qs)}" if qs else base


def _url_category_global(lang: str | None, category: str | None,
                         sort: str | None, page: int) -> str | None:
    """https://tgstat.com/channels/category/{cat}"""
    if not category:
        return None
    base = f"https://tgstat.com/channels/category/{category}"
    qs = {}
    if sort:
        qs["sort"] = sort
    if page > 1:
        qs["page"] = page
    return f"{base}?{urlencode(qs)}" if qs else base


def _url_category_by_lang(lang: str | None, category: str | None,
                          sort: str | None, page: int) -> str | None:
    """https://tgstat.com/{lang}/channels/category/{cat}"""
    if not lang or not category:
        return None
    base = f"https://tgstat.com/{lang}/channels/category/{category}"
    qs = {}
    if sort:
        qs["sort"] = sort
    if page > 1:
        qs["page"] = page
    return f"{base}?{urlencode(qs)}" if qs else base


def build_urls(
    languages: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    only_with_comments: bool = False,
    pages_per_geo: int = 1,
    include_global: bool = True,
) -> list[str]:
    """
    Собирает финальный список URL'ов из выбора пользователя.

    Args:
        languages: список language кодов (см. GEO_LANGUAGES). Если пусто
            и include_global=True — только глобальный рейтинг.
        categories: список slug'ов категорий (см. GEO_CATEGORIES). Если
            пусто — рейтинг без фильтра по категории.
        only_with_comments: если True — добавляет sort=discussions,
            чтобы рейтинг был отсортирован по обсуждаемости (там
            каналы с активными чатами всплывают наверх). Финальная
            фильтрация has_comments всё равно делается extractor'ом.
        pages_per_geo: сколько страниц с каждого среза взять. На каждом
            URL extractor сам подгружает "Показать ещё" 8 раз, поэтому
            обычно 1 страницы хватает на 60-100 карточек.
        include_global: добавлять ли URL'ы без язык-сегмента (глобальный
            рейтинг + категории глобально).

    Returns:
        list уникальных URL.
    """
    sort = "discussions" if only_with_comments else None
    langs = list(languages or []) or [None]  # None означает "без языка"
    cats = list(categories or []) or [None]  # None — без категории

    urls: list[str] = []

    for lang in langs:
        for cat in cats:
            for page in range(1, max(1, pages_per_geo) + 1):
                # Выбираем подходящий шаблон по комбинации lang/category
                if lang and cat:
                    u = _url_category_by_lang(lang, cat, sort, page)
                elif lang and not cat:
                    u = _url_rating_by_lang(lang, cat, sort, page)
                elif cat and not lang and include_global:
                    u = _url_category_global(lang, cat, sort, page)
                elif not lang and not cat and include_global:
                    u = _url_rating_global(lang, cat, sort, page)
                else:
                    u = None
                if u:
                    urls.append(u)

    # Дедуп с сохранением порядка
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
