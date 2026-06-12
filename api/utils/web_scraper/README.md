# web_scraper — Camoufox + asyncio + cooldown pool

Отказоустойчивый E2E-скрейпер для парсинга публичной статистики с
SPA-интерфейсов через ограниченный пул статических IPv4-прокси.

## Архитектура

```
                       ┌──────────────────────────┐
   urls + proxies ───▶ │   asyncio.Queue (URLs)   │
                       └──────────┬───────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        [worker 0]           [worker 1]          [worker 2]   (max 3-4)
              │                   │                   │
              └────────┬──────────┴──────────┬────────┘
                       ▼                     ▼
              ┌─────────────────┐   ┌────────────────────┐
              │  NodeRotator    │   │ BrowserContextPool │
              │  (34 proxies +  │   │ (1 Camoufox +      │
              │   per-node      │   │  изолированные     │
              │   cooldown)     │   │  контексты)        │
              └─────────────────┘   └────────────────────┘
                       │                     │
                       └──────────┬──────────┘
                                  ▼
                       [user extractor(page)]
                                  │
                                  ▼
                       ┌────────────────────┐
                       │ JSONLCheckpoint    │
                       │ (atomic write +    │
                       │  resume support)   │
                       └────────────────────┘
```

## Установка

```powershell
# 1. Camoufox + GeoIP-патч (для realistic locale/timezone)
pip install -U "camoufox[geoip]"

# 2. Скачать патченный Firefox-бинарник
camoufox fetch

# 3. Playwright Browsers — Camoufox использует его API
python -m playwright install firefox
```

После установки Camoufox можно тестировать так:

```python
from camoufox.async_api import AsyncCamoufox
import asyncio

async def main():
    async with AsyncCamoufox(humanize=True, headless=True) as browser:
        page = await browser.new_page()
        await page.goto("https://abrahamjuliot.github.io/creepjs/")
        print(await page.title())

asyncio.run(main())
```

## Использование из кода

```python
from utils.web_scraper import WebScraper

PROXIES = [
    "http://user:pass@1.2.3.4:8080",
    "socks5://user:pass@1.2.3.5:1080",
    # ... 34 узла
]

async def my_extractor(page):
    return {
        "title": await page.title(),
        "h1": await page.locator("h1").first.text_content(),
    }

async def main():
    scraper = WebScraper(
        proxies=PROXIES,
        checkpoint_path="data/results.jsonl",
        extractor=my_extractor,
        max_workers=3,
    )
    async with scraper:
        await scraper.run([
            "https://example.com/page1",
            "https://example.com/page2",
        ])
```

## Использование через UI

1. Открой **Парсер → 🛰️ Web-парсер**
2. Вставь URL (один на строку)
3. Вставь 34 прокси (одна строка = один прокси, формат `scheme://user:pass@host:port`)
4. Расширенные настройки — опционально
5. Нажми **🚀 Запустить скрейпинг**
6. Жди или останови через **⏹ Остановить**
7. Скачай результаты — **📥 Скачать JSONL**

## Smoke-тест

```powershell
cd api
python -m utils.web_scraper.test_smoke
```

Проверяет NodeRotator, JSONLCheckpoint, retry.backoff — без запуска Camoufox.
Длительность ~2 сек.

## Параметры по умолчанию

| Параметр | Значение | Зачем |
|---|---|---|
| `max_workers` | 3 | Микро-батчинг под пул 34 узлов |
| `max_retries` | 3 | Дальше URL считается мёртвым |
| `page_timeout_sec` | 60 | Camoufox с JS-челленджами медленный |
| `cooldown_min/max_sec` | 900-1200 | 15-20 мин для trust score recovery |
| `node_rotation_min/max_sec` | 15-35 | Пауза между сессиями на одном IP |

## Когда что в cooldown

| Событие | Действие |
|---|---|
| HTTP 403 / 429 | Узел в cooldown 15-20 мин, **retry с другим узлом** (без backoff) |
| HTTP 5xx | Узел НЕ в cooldown (сервер моргнул), backoff retry |
| Timeout / network error | Узел в cooldown, backoff retry |
| 4xx (кроме 429) | Запись как failed, retry не делаем |

## Файлы

- `node_rotator.py` — пул прокси с per-node lock и cooldown
- `context_pool.py` — Camoufox + изолированные контексты + пауза 15-35 сек на IP
- `checkpoint.py` — JSONL persistence с resume
- `retry.py` — exponential backoff с full jitter
- `ux_emulator.py` — мышь / скроллинг / human_pause
- `scraper.py` — orchestrator
- `test_smoke.py` — лёгкие проверки без Camoufox
