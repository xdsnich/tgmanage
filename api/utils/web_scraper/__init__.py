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
from .tgstat_urls import build_urls, GEO_LANGUAGES, GEO_CATEGORIES

__all__ = [
    "WebScraper",
    "ScrapeResult",
    "NodeRotator",
    "ProxyNode",
    "BrowserContextPool",
    "JSONLCheckpoint",
    "tgstat_channels_extractor",
    "build_urls",
    "GEO_LANGUAGES",
    "GEO_CATEGORIES",
]
