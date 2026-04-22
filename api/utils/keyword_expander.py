"""
GramGPT — utils/keyword_expander.py

Модуль автоматического расширения ключевых слов для парсинга каналов.

Логика:
  1. Seed-ключевик (crypto, трейдинг, etc.)
  2. Транслитерация (cyrillic <-> latin)
  3. Переводы на 20+ языков
  4. Гео-суффиксы по языкам (crypto_ua, crypto_tr, etc.)
  5. Универсальные префиксы/суффиксы (best_, _news, _pro)
  6. Тематические синонимы (crypto → btc, eth, binance, defi, nft)

Rating — приблизительный рейтинг популярности keyword в Telegram:
  - short (3-6 символов) = высокий
  - общие языки (en/ru) = высокий
  - редкие суффиксы = низкий
"""

from __future__ import annotations
from typing import Dict, List, Optional
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════
# Гео-коды и их особенности (язык, суффиксы, префиксы)
# ═══════════════════════════════════════════════════════════

GEO_CONFIG: Dict[str, dict] = {
    "en": {
        "name": "English / Global",
        "suffixes": ["_news", "_signals", "_pro", "_hub", "_daily", "_official",
                     "_world", "_global", "_channel", "_group", "_live", "_alerts"],
        "prefixes": ["best_", "top_", "pro_", "ai_", "my_", "the_", "daily_"],
        "country_codes": ["us", "uk", "world"],
    },
    "ru": {
        "name": "Русский",
        "suffixes": ["_рф", "_ru", "_россия", "_ру", "_официальный", "_новости",
                     "_сигналы", "_канал", "_чат", "_про", "_трейд"],
        "prefixes": ["топ_", "лучший_", "про_", "новости_", "сигналы_"],
        "country_codes": ["ru", "rus", "russia"],
    },
    "ua": {
        "name": "Українська",
        "suffixes": ["_ua", "_укр", "_україна", "_новини", "_канал", "_чат",
                     "_ukraine", "_київ", "_ukr", "_pro_ua"],
        "prefixes": ["укр_", "ukr_", "ua_", "київ_", "kyiv_"],
        "country_codes": ["ua", "ukr", "ukraine", "kyiv"],
    },
    "es": {
        "name": "Español (LATAM / ES)",
        "suffixes": ["_es", "_latam", "_mexico", "_argentina", "_espanol",
                     "_noticias", "_senales", "_oficial", "_canal", "_trading"],
        "prefixes": ["mejor_", "top_", "senales_", "cripto_"],
        "country_codes": ["es", "mx", "ar", "co", "cl", "pe", "latam"],
    },
    "pt": {
        "name": "Português (BR / PT)",
        "suffixes": ["_br", "_pt", "_brasil", "_portugal", "_noticias",
                     "_oficial", "_canal", "_sinais", "_trading"],
        "prefixes": ["melhor_", "top_", "sinais_"],
        "country_codes": ["br", "pt", "brasil"],
    },
    "tr": {
        "name": "Türkçe",
        "suffixes": ["_tr", "_turkiye", "_turk", "_haberler", "_sinyal",
                     "_resmi", "_kanal", "_pro_tr"],
        "prefixes": ["en_iyi_", "top_", "turk_", "kripto_"],
        "country_codes": ["tr", "turkiye", "turkey"],
    },
    "de": {
        "name": "Deutsch",
        "suffixes": ["_de", "_deutsch", "_deutschland", "_nachrichten",
                     "_signale", "_offiziell", "_kanal", "_pro_de"],
        "prefixes": ["beste_", "top_", "signale_"],
        "country_codes": ["de", "deutschland", "germany"],
    },
    "fr": {
        "name": "Français",
        "suffixes": ["_fr", "_france", "_francais", "_news_fr",
                     "_officiel", "_signaux", "_chaine"],
        "prefixes": ["meilleur_", "top_", "pro_fr_"],
        "country_codes": ["fr", "france"],
    },
    "it": {
        "name": "Italiano",
        "suffixes": ["_it", "_italia", "_news_it", "_ufficiale",
                     "_segnali", "_canale"],
        "prefixes": ["top_", "migliore_", "cripto_"],
        "country_codes": ["it", "italia", "italy"],
    },
    "pl": {
        "name": "Polski",
        "suffixes": ["_pl", "_polska", "_wiadomosci", "_oficjalny",
                     "_sygnaly", "_kanal"],
        "prefixes": ["najlepszy_", "top_", "sygnaly_"],
        "country_codes": ["pl", "polska", "poland"],
    },
    "nl": {
        "name": "Nederlands",
        "suffixes": ["_nl", "_nederland", "_nieuws", "_officieel", "_kanaal"],
        "prefixes": ["beste_", "top_"],
        "country_codes": ["nl", "nederland"],
    },
    "ar": {
        "name": "العربية (Arabic)",
        "suffixes": ["_ar", "_arab", "_arabic", "_news_ar",
                     "_saudi", "_uae", "_egypt"],
        "prefixes": ["top_", "best_ar_", "arab_"],
        "country_codes": ["ae", "sa", "eg", "ar", "arab", "saudi", "dubai"],
    },
    "fa": {
        "name": "فارسی (Persian)",
        "suffixes": ["_ir", "_iran", "_farsi", "_persian"],
        "prefixes": ["top_ir_", "iran_"],
        "country_codes": ["ir", "iran"],
    },
    "hi": {
        "name": "हिन्दी (Hindi)",
        "suffixes": ["_hindi", "_india", "_in", "_news_india",
                     "_desi", "_bharat"],
        "prefixes": ["top_india_", "india_", "desi_"],
        "country_codes": ["in", "india", "bharat", "desi"],
    },
    "id": {
        "name": "Bahasa Indonesia",
        "suffixes": ["_id", "_indonesia", "_berita", "_resmi"],
        "prefixes": ["top_id_", "indo_"],
        "country_codes": ["id", "indonesia"],
    },
    "vi": {
        "name": "Tiếng Việt",
        "suffixes": ["_vn", "_vietnam", "_tintuc", "_chinhthuc"],
        "prefixes": ["top_vn_", "vn_"],
        "country_codes": ["vn", "vietnam", "viet"],
    },
    "zh": {
        "name": "中文 (Chinese)",
        "suffixes": ["_cn", "_china", "_chinese", "_zh", "_news_cn"],
        "prefixes": ["top_cn_", "china_"],
        "country_codes": ["cn", "china", "tw", "hk"],
    },
    "ja": {
        "name": "日本語 (Japanese)",
        "suffixes": ["_jp", "_japan", "_japanese", "_news_jp"],
        "prefixes": ["top_jp_", "japan_"],
        "country_codes": ["jp", "japan"],
    },
    "ko": {
        "name": "한국어 (Korean)",
        "suffixes": ["_kr", "_korea", "_korean", "_news_kr"],
        "prefixes": ["top_kr_", "korea_"],
        "country_codes": ["kr", "korea"],
    },
    "th": {
        "name": "ภาษาไทย (Thai)",
        "suffixes": ["_th", "_thailand", "_news_th"],
        "prefixes": ["top_th_", "thai_"],
        "country_codes": ["th", "thailand", "thai"],
    },
    "sv": {
        "name": "Svenska",
        "suffixes": ["_se", "_sweden", "_nyheter"],
        "prefixes": ["basta_", "top_"],
        "country_codes": ["se", "sverige", "sweden"],
    },
    "cs": {
        "name": "Čeština",
        "suffixes": ["_cz", "_czech", "_cesky"],
        "prefixes": ["nejlepsi_", "top_"],
        "country_codes": ["cz", "cesko", "czech"],
    },
    "ro": {
        "name": "Română",
        "suffixes": ["_ro", "_romania", "_stiri"],
        "prefixes": ["top_ro_"],
        "country_codes": ["ro", "romania"],
    },
}

# ═══════════════════════════════════════════════════════════
# Тематические синонимы и связанные термины
# ═══════════════════════════════════════════════════════════

TOPIC_SYNONYMS: Dict[str, List[str]] = {
    # Крипта
    "crypto": ["cryptocurrency", "btc", "bitcoin", "eth", "ethereum", "defi", "nft",
               "altcoin", "binance", "okx", "bybit", "airdrop", "web3", "blockchain",
               "trading", "dex", "memecoin", "solana", "xrp"],
    "bitcoin": ["btc", "btcusd", "bitcoin_news", "crypto", "satoshi", "hodl"],
    "trading": ["trade", "signals", "forex", "tradingview", "analysis", "chart",
                "scalping", "swing", "daytrading"],
    "forex": ["fx", "forex_signals", "currency", "trading", "eurusd", "gbpusd"],

    # Финансы
    "finance": ["money", "invest", "investing", "stocks", "wealth", "business",
                "entrepreneur", "passive_income"],
    "stocks": ["nasdaq", "sp500", "nyse", "invest", "investing", "wallstreet", "finance"],

    # Маркетинг
    "marketing": ["smm", "social_media", "seo", "digital_marketing", "ads",
                  "copywriting", "branding"],
    "business": ["entrepreneur", "startup", "company", "ceo", "bizdev", "marketing"],

    # Техно
    "tech": ["technology", "gadgets", "smartphone", "ai", "gpt", "software", "hardware"],
    "ai": ["artificial_intelligence", "chatgpt", "gpt", "ml", "machine_learning",
           "openai", "claude", "gemini", "llm"],
    "programming": ["dev", "developer", "coding", "python", "javascript", "devops"],

    # Игры
    "gaming": ["games", "gamer", "esports", "steam", "playstation", "xbox",
               "twitch", "streamer"],

    # Криптовалюта (рус)
    "крипта": ["криптовалюта", "биткоин", "btc", "блокчейн", "крипто", "трейдинг",
               "сигналы", "альткоин", "дефи", "ндт", "нфт"],
    "биткоин": ["bitcoin", "btc", "крипта", "криптовалюта"],

    # Тематика: новости
    "news": ["novosti", "новости", "актуальное", "события", "breaking"],

    # Здоровье
    "fitness": ["gym", "workout", "health", "nutrition", "bodybuilding", "yoga"],
    "health": ["medical", "wellness", "nutrition", "fitness", "healthy"],
}


# ═══════════════════════════════════════════════════════════
# Транслитерация (cyrillic ↔ latin)
# ═══════════════════════════════════════════════════════════

CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    'і': 'i', 'ї': 'i', 'є': 'ie', 'ґ': 'g',   # украинские
}

# Обратная транслитерация latin → cyrillic (приблизительная)
LAT_TO_CYR_PAIRS = [
    ('sch', 'щ'), ('shh', 'щ'), ('ya', 'я'), ('yu', 'ю'), ('yo', 'ё'),
    ('zh', 'ж'), ('ch', 'ч'), ('sh', 'ш'), ('kh', 'х'), ('ts', 'ц'),
    ('th', 'т'),
    ('a', 'а'), ('b', 'б'), ('v', 'в'), ('g', 'г'), ('d', 'д'),
    ('e', 'е'), ('z', 'з'), ('i', 'и'), ('y', 'й'), ('k', 'к'),
    ('l', 'л'), ('m', 'м'), ('n', 'н'), ('o', 'о'), ('p', 'п'),
    ('r', 'р'), ('s', 'с'), ('t', 'т'), ('u', 'у'), ('f', 'ф'),
]


def _transliterate_cyr_to_lat(text: str) -> str:
    """Русский/украинский → латиница"""
    result = []
    for char in text.lower():
        result.append(CYR_TO_LAT.get(char, char))
    return ''.join(result)


def _transliterate_lat_to_cyr(text: str) -> str:
    """Латиница → кириллица (приблизительная)"""
    result = text.lower()
    for lat, cyr in LAT_TO_CYR_PAIRS:
        result = result.replace(lat, cyr)
    return result


def _is_cyrillic(text: str) -> bool:
    """Проверка: текст на кириллице?"""
    return any('\u0400' <= c <= '\u04ff' for c in text)


# ═══════════════════════════════════════════════════════════
# Простые переводы (только базовые темы)
# ═══════════════════════════════════════════════════════════

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "crypto": {
        "en": "crypto", "ru": "крипта", "ua": "крипта",
        "es": "cripto", "pt": "cripto", "tr": "kripto",
        "de": "krypto", "fr": "crypto", "it": "cripto",
        "pl": "krypto", "nl": "crypto",
        "ar": "كريبتو", "fa": "کریپتو",
        "hi": "क्रिप्टो", "id": "kripto", "vi": "tiền mã hóa",
        "zh": "加密货币", "ja": "仮想通貨", "ko": "암호화폐",
        "th": "คริปโต",
    },
    "bitcoin": {
        "en": "bitcoin", "ru": "биткоин", "ua": "біткоїн",
        "es": "bitcoin", "tr": "bitcoin", "de": "bitcoin",
        "fr": "bitcoin", "ar": "بيتكوين", "hi": "बिटकॉइन",
        "zh": "比特币", "ja": "ビットコイン", "ko": "비트코인",
    },
    "news": {
        "en": "news", "ru": "новости", "ua": "новини",
        "es": "noticias", "pt": "noticias", "tr": "haberler",
        "de": "nachrichten", "fr": "news", "it": "notizie",
        "pl": "wiadomosci", "ar": "أخبار", "hi": "समाचार",
        "zh": "新闻", "ja": "ニュース", "ko": "뉴스",
        "th": "ข่าว", "id": "berita", "vi": "tin tức",
    },
    "trading": {
        "en": "trading", "ru": "трейдинг", "ua": "трейдинг",
        "es": "trading", "pt": "trading", "tr": "trading",
        "de": "trading", "fr": "trading",
        "ar": "تداول", "hi": "ट्रेडिंग",
        "zh": "交易", "ja": "取引", "ko": "거래",
    },
    "signals": {
        "en": "signals", "ru": "сигналы", "ua": "сигнали",
        "es": "senales", "pt": "sinais", "tr": "sinyal",
        "de": "signale", "fr": "signaux", "it": "segnali",
        "pl": "sygnaly", "ar": "إشارات",
    },
    "business": {
        "en": "business", "ru": "бизнес", "ua": "бізнес",
        "es": "negocios", "tr": "isletme", "de": "business",
        "fr": "business", "ar": "أعمال",
    },
    "money": {
        "en": "money", "ru": "деньги", "ua": "гроші",
        "es": "dinero", "pt": "dinheiro", "tr": "para",
        "de": "geld", "fr": "argent",
        "ar": "مال", "hi": "पैसा",
    },
    "marketing": {
        "en": "marketing", "ru": "маркетинг", "ua": "маркетинг",
        "es": "marketing", "tr": "pazarlama", "de": "marketing",
    },
    "forex": {
        "en": "forex", "ru": "форекс", "ua": "форекс",
        "tr": "forex", "ar": "فوركس",
    },
    "investing": {
        "en": "investing", "ru": "инвестиции", "ua": "інвестиції",
        "es": "inversiones", "tr": "yatirim", "de": "investieren",
    },
}


# ═══════════════════════════════════════════════════════════
# Основной класс расширения
# ═══════════════════════════════════════════════════════════

@dataclass
class ExpandedKeyword:
    """Один сгенерированный keyword с метаданными."""
    keyword: str
    source_seed: str     # исходное слово
    category: str        # 'base' | 'translit' | 'translation' | 'geo_suffix' | 'prefix' | 'suffix' | 'topic'
    geo: Optional[str] = None    # код языка/гео
    rating: int = 50     # 0-100, примерный рейтинг


def _rate_keyword(kw: str, category: str, geo: Optional[str]) -> int:
    """Приблизительный рейтинг keyword. 0 = плохой, 100 = топ."""
    rating = 50

    # Длина — короткие keywords популярнее
    if 3 <= len(kw) <= 6:
        rating += 15
    elif 7 <= len(kw) <= 12:
        rating += 5
    elif len(kw) > 15:
        rating -= 10

    # Базовые категории ценнее
    if category == "base":
        rating += 20
    elif category == "translation":
        rating += 10
    elif category == "topic":
        rating += 5
    elif category == "prefix" or category == "suffix":
        rating -= 5

    # Популярные языки выше
    if geo in ("en", "ru", "es", "pt", "ar", "hi"):
        rating += 10
    elif geo in ("ua", "tr", "de", "fr", "it", "pl"):
        rating += 5

    # Underscore снижает рейтинг (менее точный search)
    if kw.count("_") >= 2:
        rating -= 10

    return max(0, min(100, rating))


def expand_keyword(
    seed: str,
    target_geos: Optional[List[str]] = None,
    include_translit: bool = True,
    include_translations: bool = True,
    include_geo_variants: bool = True,
    include_prefixes_suffixes: bool = True,
    include_topic_synonyms: bool = True,
    max_results: int = 200,
) -> List[ExpandedKeyword]:
    """
    Расширяет один seed в десятки/сотни вариантов.

    target_geos: список кодов языков (из GEO_CONFIG), для которых делать варианты.
                 None = все языки.
    """
    seed = seed.strip().lower()
    if not seed:
        return []

    if target_geos is None:
        target_geos = list(GEO_CONFIG.keys())

    results: List[ExpandedKeyword] = []
    seen = set()

    def _add(kw: str, category: str, geo: Optional[str] = None):
        kw = kw.strip().lower()
        if not kw or kw in seen or len(kw) > 64:
            return
        seen.add(kw)
        results.append(ExpandedKeyword(
            keyword=kw,
            source_seed=seed,
            category=category,
            geo=geo,
            rating=_rate_keyword(kw, category, geo),
        ))

    # 1. Базовый
    _add(seed, "base", "en" if not _is_cyrillic(seed) else "ru")

    # 2. Транслитерация
    if include_translit:
        if _is_cyrillic(seed):
            _add(_transliterate_cyr_to_lat(seed), "translit", "en")
        else:
            _add(_transliterate_lat_to_cyr(seed), "translit", "ru")

    # 3. Переводы
    if include_translations and seed in TRANSLATIONS:
        for geo, translation in TRANSLATIONS[seed].items():
            if geo in target_geos:
                _add(translation, "translation", geo)

    # 4. Тематические синонимы
    if include_topic_synonyms and seed in TOPIC_SYNONYMS:
        for syn in TOPIC_SYNONYMS[seed]:
            _add(syn, "topic", "en" if not _is_cyrillic(syn) else "ru")

    # 5. Гео-варианты с суффиксами и префиксами
    if include_geo_variants:
        base_terms = [seed]
        # Добавляем переводы как базу для гео-вариантов
        if seed in TRANSLATIONS:
            for geo in target_geos:
                if geo in TRANSLATIONS[seed]:
                    base_terms.append(TRANSLATIONS[seed][geo])

        for term in list(set(base_terms)):
            for geo in target_geos:
                if geo not in GEO_CONFIG:
                    continue
                cfg = GEO_CONFIG[geo]

                # Country codes (_ua, _ru, _tr)
                for cc in cfg["country_codes"][:3]:  # ограничим по 3 самых популярных
                    _add(f"{term}_{cc}", "geo_suffix", geo)
                    _add(f"{cc}_{term}", "geo_suffix", geo)

    # 6. Префиксы и суффиксы (только для базового и переводов)
    if include_prefixes_suffixes:
        stems = {seed}
        if seed in TRANSLATIONS:
            for geo in target_geos:
                if geo in TRANSLATIONS[seed]:
                    stems.add(TRANSLATIONS[seed][geo])

        for stem in stems:
            # Определим подходящий geo для stem
            stem_geo = "en"
            if seed in TRANSLATIONS:
                for geo, tr in TRANSLATIONS[seed].items():
                    if tr == stem:
                        stem_geo = geo
                        break

            cfg = GEO_CONFIG.get(stem_geo, GEO_CONFIG["en"])

            # Ограничим количество — берём по 4-5 самых ценных
            for suf in cfg["suffixes"][:6]:
                _add(f"{stem}{suf}", "suffix", stem_geo)

            for pref in cfg["prefixes"][:4]:
                _add(f"{pref}{stem}", "prefix", stem_geo)

    # Сортируем по рейтингу
    results.sort(key=lambda x: -x.rating)

    return results[:max_results]


def expand_keywords(
    seeds: List[str],
    target_geos: Optional[List[str]] = None,
    max_per_seed: int = 100,
    **kwargs,
) -> Dict[str, List[ExpandedKeyword]]:
    """Расширяет несколько seeds сразу. Возвращает {seed: [варианты]}."""
    result = {}
    for seed in seeds:
        seed = seed.strip()
        if not seed:
            continue
        result[seed] = expand_keyword(
            seed,
            target_geos=target_geos,
            max_results=max_per_seed,
            **kwargs,
        )
    return result


def get_geo_presets() -> Dict[str, List[str]]:
    """Готовые наборы гео для быстрого выбора."""
    return {
        "ua_ru_en": ["ua", "ru", "en"],
        "europe": ["en", "de", "fr", "it", "es", "pl", "nl", "pt"],
        "cis": ["ru", "ua"],
        "latam": ["es", "pt"],
        "asia": ["hi", "id", "vi", "zh", "ja", "ko", "th"],
        "mena": ["ar", "fa", "tr"],
        "global_en": ["en"],
        "all": list(GEO_CONFIG.keys()),
    }


def list_available_geos() -> List[dict]:
    """Список доступных гео с названиями."""
    return [
        {"code": code, "name": cfg["name"]}
        for code, cfg in GEO_CONFIG.items()
    ]
