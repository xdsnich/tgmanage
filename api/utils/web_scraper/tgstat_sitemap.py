"""
tgstat_sitemap.py — источник кандидатов из TGStat sitemap.xml.

Открытие: TGStat публикует sitemaps индекс — `/sitemaps/global/sitemap.xml`
(на любом поддомене) и национальные `/sitemaps/{cc}/sitemap.xml`.
Первый нумерованный child (`sitemap1.xml`) содержит все bare-канальные
URL этой ветки (`.../channel/@X`). Остальные — посты каналов.

Числа (проверено 2026-07-04):
  global (tgstat.com/en/ir/in/... — один и тот же файл):   ~9 852 каналов
  ir.tgstat.com/sitemaps/ir/sitemap1.xml                    ~9 852
  in.tgstat.com/sitemaps/in/sitemap1.xml                    ~3 276
  kaz.tgstat.com/sitemaps/kz/sitemap1.xml                   ~2 592
  by.tgstat.com/sitemaps/by/sitemap1.xml                      ~906
  uk.tgstat.com/sitemaps/ua/sitemap1.xml                    ~4 791
  cn.tgstat.com/sitemaps/cn/sitemap1.xml                    ~6 918
  uz.tgstat.com/sitemaps/uz/sitemap1.xml                    ~9 624
  kg.tgstat.com/sitemaps/kg/sitemap1.xml                      ~555
  et.tgstat.com/sitemaps/et/sitemap1.xml                    ~3 057

Итого до ~50k уникальных каналов за 10 HTTP-запросов.

Почему это работает: sitemap файлы — статика для SEO-краулеров, обычно
не под Cloudflare JS-challenge. Можно тянуть их даже без CloakBrowser'а,
но мы используем существующий пул чтобы не изобретать HTTP-клиент.
"""

from __future__ import annotations
import re
import asyncio
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# host → sitemap path prefix (из robots.txt каждого поддомена)
COUNTRY_SITEMAPS: dict[str, tuple[str, str]] = {
    "global": ("tgstat.com", "/sitemaps/global/sitemap1.xml"),
    "ir":     ("ir.tgstat.com", "/sitemaps/ir/sitemap1.xml"),
    "in":     ("in.tgstat.com", "/sitemaps/in/sitemap1.xml"),
    "kz":     ("kaz.tgstat.com", "/sitemaps/kz/sitemap1.xml"),
    "by":     ("by.tgstat.com", "/sitemaps/by/sitemap1.xml"),
    "ua":     ("uk.tgstat.com", "/sitemaps/ua/sitemap1.xml"),
    "cn":     ("cn.tgstat.com", "/sitemaps/cn/sitemap1.xml"),
    "uz":     ("uz.tgstat.com", "/sitemaps/uz/sitemap1.xml"),
    "kg":     ("kg.tgstat.com", "/sitemaps/kg/sitemap1.xml"),
    "et":     ("et.tgstat.com", "/sitemaps/et/sitemap1.xml"),
    # RU sitemap возвращает 403 на многих прокси — вероятно доп. защита.
    # Не включаем по умолчанию, пусть пользователь пробует руками.
    "ru":     ("tgstat.ru", "/sitemaps/ru/sitemap1.xml"),
}

_CHANNEL_RE = re.compile(r"<loc>[^<]*?/channel/@([a-zA-Z0-9_]+)</loc>")


def _url_for(country: str) -> str:
    host, path = COUNTRY_SITEMAPS[country]
    return f"https://{host}{path}"


def sitemap_urls_for(countries: Iterable[str], include_global: bool = True) -> list[str]:
    """Билдер URLs для Stage 1 pipeline.

    Возвращает список sitemap URL, каждый из которых WebScraper откроет
    как обычную страницу и прогонит через `tgstat_sitemap_extractor`.
    """
    urls: list[str] = []
    seen: set[str] = set()
    if include_global:
        u = _url_for("global")
        urls.append(u)
        seen.add(u)
    for cc in countries:
        cc = (cc or "").strip().lower()
        if not cc or cc == "global" or cc not in COUNTRY_SITEMAPS:
            continue
        u = _url_for(cc)
        if u in seen:
            continue
        seen.add(u)
        urls.append(u)
    return urls


async def tgstat_sitemap_extractor(page: Any) -> dict:
    """Extractor для Stage 1: читает XML sitemap и возвращает список
    `{"kind": "channel", "username": "@X"}` в поле `channels` — совместимо
    с существующим Stage 2 preparation-кодом.

    XML sitemap отдаётся как обычная страница (browsers показывают её как
    styled XML), поэтому `document.documentElement.outerHTML` работает.
    """
    body = await page.evaluate("() => document.documentElement.outerHTML")
    usernames = _CHANNEL_RE.findall(body or "")
    seen: set[str] = set()
    channels: list[dict] = []
    for u in usernames:
        low = u.lower()
        if low in seen:
            continue
        seen.add(low)
        channels.append({"kind": "channel", "username": f"@{u}"})
    return {
        "source": "tgstat_sitemap",
        "channels": channels,
        "channels_total": len(channels),
    }


async def _fetch_one(pool, proxy_url: str, url: str, timeout_sec: float = 45.0) -> list[str]:
    """Через существующий CloakBrowser пул — открывает sitemap и достаёт usernames.

    Возвращает список без @, в исходном регистре, дедуп-подобным нам порядком.
    """
    async with pool.context_for(proxy_url) as ctx:
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="load", timeout=int(timeout_sec * 1000))
            if resp is None:
                return []
            status = resp.status
            if status >= 400:
                logger.warning(f"[sitemap] {url}: HTTP {status}")
                return []
            body = await page.evaluate("() => document.documentElement.outerHTML")
        finally:
            try:
                await page.close()
            except Exception:
                pass

    usernames = _CHANNEL_RE.findall(body)
    # Порядок: сохранить как встречается (обычно от свежих/популярных к старым),
    # но дедуп.
    seen: set[str] = set()
    ordered: list[str] = []
    for u in usernames:
        key = u.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(u)
    logger.info(f"[sitemap] {url} → {len(ordered)} unique usernames")
    return ordered


async def fetch_channel_usernames(
    pool,
    proxy_url: str,
    countries: Iterable[str],
    include_global: bool = True,
    per_source_timeout_sec: float = 45.0,
) -> list[str]:
    """Основной entry point.

    Args:
        pool: CloakBrowserPool (или совместимый — .context_for(proxy) async ctx mgr)
        proxy_url: какой прокси использовать. Обычно один и тот же для всех
            sitemap'ов — их всего 10, парсим последовательно.
        countries: список кодов ISO (`ir`, `in`, `kz`, `by`, `ua`, `cn`, `uz`, `kg`, `et`).
        include_global: добавить ли global sitemap (~10k каналов, международный).

    Returns:
        Список username'ов (без @), с дедупом. Порядок:
        сначала global (если включён), потом страны в порядке передачи.

    Sitemap fetch — последовательный, не параллельный. Причины:
      1. Всего 10 HTTP-запросов, ~3с каждый = ~30с total — приемлемо.
      2. Один и тот же прокси → один и тот же CloakBrowser контекст →
         WAF видит "юзер листает страницу sitemap-каталога", а не
         "3 параллельных запроса подряд".
    """
    targets: list[str] = []
    if include_global:
        targets.append("global")
    for cc in countries:
        cc = cc.strip().lower()
        if cc and cc != "global" and cc in COUNTRY_SITEMAPS and cc not in targets:
            targets.append(cc)

    if not targets:
        return []

    all_usernames: list[str] = []
    seen: set[str] = set()

    for target in targets:
        url = _url_for(target)
        try:
            usernames = await _fetch_one(pool, proxy_url, url, per_source_timeout_sec)
        except Exception as e:
            logger.warning(f"[sitemap] {url} failed: {type(e).__name__}: {e}")
            continue
        added = 0
        for u in usernames:
            key = u.lower()
            if key in seen:
                continue
            seen.add(key)
            all_usernames.append(u)
            added += 1
        logger.info(f"[sitemap] {target}: +{added} новых (итого: {len(all_usernames)})")

    return all_usernames
