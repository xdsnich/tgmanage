"""
web_scraper — отказоустойчивый E2E-скрейпер на Camoufox + AsyncIO.

Архитектурные принципы:
  - Микро-батчинг (3-4 воркера) против пула из 34 статических IPv4
  - WAF-friendly: per-node cooldown 15-20 мин на 403/429/timeout
  - Долгоживущий браузер + изолированные контексты с ротацией 15-35 сек на узел
  - JSONL-чекпоинты для resume без дублирования
  - Эмуляция UX: длинные паузы, движение мыши, плавный скроллинг

Точка входа: WebScraper.
"""

from .scraper import WebScraper, ScrapeResult
from .node_rotator import NodeRotator, ProxyNode
from .context_pool import BrowserContextPool
from .checkpoint import JSONLCheckpoint
from .tgstat_extractor import tgstat_channels_extractor
from .tgstat_urls import build_urls, GEO_LANGUAGES, GEO_CATEGORIES, GEO_COUNTRIES
from .tgstat_sitemap import (
    sitemap_urls_for,
    tgstat_sitemap_extractor,
    COUNTRY_SITEMAPS as SITEMAP_COUNTRIES,
)

__all__ = [
    "WebScraper",
    "ScrapeResult",
    "NodeRotator",
    "ProxyNode",
    "BrowserContextPool",
    "JSONLCheckpoint",
    "tgstat_channels_extractor",
    "tgstat_sitemap_extractor",
    "sitemap_urls_for",
    "SITEMAP_COUNTRIES",
    "build_urls",
    "GEO_LANGUAGES",
    "GEO_CATEGORIES",
    "GEO_COUNTRIES",
]
