"""
tgstat_fast.py — быстрый сбор рейтингов TGStat обычным HTTP, без браузера.

Зачем в дополнение к браузерному скрейперу: страницы рейтинга отдаются
статической разметкой, JS для их чтения не нужен. Замер 26.08 через прокси:

    браузер (Camoufox)  ~24 с на страницу   (2.5 стр/мин)
    обычный HTTP         ~0.9 с на страницу

Разница в 25 раз. Для 350 ссылок это минуты вместо пяти часов.

Две ловушки, из-за которых нельзя просто слать запросы подряд:

  1. TGStat при перегреве отвечает «429 Authentication Required» С КОДОМ 200.
     В июле на этом потеряли целые регионы: отказ выглядел как успех, данные
     молча оказывались пустыми. Поэтому успехом считается только страница,
     где реально нашлись каналы, — см. parse_ratings.

  2. Домен может отказывать целиком (tgstat.ru отдаёт 403 на всё). Один такой
     домен способен сжечь пул прокси, если продолжать в него ходить, поэтому
     здесь тот же предохранитель на домен, что и в браузерном скрейпере.
"""

import asyncio
import logging
import os
import random
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Домен, набравший столько отказов без единого успеха, перестаёт опрашиваться.
HOST_FAIL_LIMIT = int(os.getenv("TGSTAT_FAST_HOST_FAIL_LIMIT", "4"))
# Пауза между запросами одного воркера.
DELAY_MIN = float(os.getenv("TGSTAT_FAST_DELAY_MIN", "1.5"))
DELAY_MAX = float(os.getenv("TGSTAT_FAST_DELAY_MAX", "4.0"))

# Карточка канала в рейтинге: ссылка на статистику, затем название и число
# подписчиков в соседних блоках. Разметка Bootstrap-овая и стабильная.
_CARD_RE = re.compile(
    r'/channel/@(?P<user>[A-Za-z]\w{3,31})/stat"'          # юзернейм
    r'.*?font-16 text-dark[^>]*>(?P<title>.*?)</div>'      # название
    r'.*?font-14 text-dark[^>]*>\s*(?P<subs>[\d\s  ,\.]+?)\s*<span',  # подписчики
    re.S,
)
_CAT_RE = re.compile(r'border rounded bg-light px-1"?\s*>\s*(.*?)\s*</span>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _num(s: str) -> int:
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else 0


def _clean_text(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


def parse_ratings(html: str) -> list[dict]:
    """Достаёт каналы из страницы рейтинга.

    Пустой список = страница не является рейтингом. Вызывающий обязан
    считать это ОТКАЗОМ, а не «нашлось ноль каналов»: именно так выглядит
    подсунутая вместо страницы заглушка «429 Authentication Required»,
    которая приходит с кодом 200.
    """
    out = []
    seen = set()
    for m in _CARD_RE.finditer(html or ""):
        u = m.group("user").lower()
        if u in seen:
            continue
        seen.add(u)
        out.append({
            "username": u,
            "title": _clean_text(m.group("title"))[:200],
            "subscribers": _num(m.group("subs")),
        })
    return out


def looks_throttled(html: str) -> bool:
    """Признаки того, что вместо страницы пришла заглушка."""
    h = (html or "")[:4000].lower()
    return ("authentication required" in h) or ("429" in h and "tgstat" not in h)


async def fetch_one(session, url: str, proxy: str, timeout: float = 30.0):
    """(channels, error). channels непустой только при настоящем успехе."""
    import aiohttp
    try:
        async with session.get(
            url, proxy=proxy, allow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status != 200:
                return [], f"HTTP {r.status}"
            html = await r.text()
    except Exception as e:
        return [], f"{type(e).__name__}"

    if looks_throttled(html):
        return [], "throttled"
    ch = parse_ratings(html)
    if not ch:
        # Код 200, но каналов нет — та самая маскировка отказа под успех.
        return [], "no_channels"
    return ch, None


async def scrape(urls: list[str], proxies: list[str], workers: int = 6,
                 on_progress=None) -> dict:
    """Собрать каналы со списка страниц рейтинга.

    Возвращает {'channels': {username: {...}}, 'ok': n, 'failed': n,
                'dead_hosts': [...], 'errors': {...}}
    """
    import aiohttp

    queue = asyncio.Queue()
    for u in urls:
        queue.put_nowait(u)

    channels: dict[str, dict] = {}
    stats = {"ok": 0, "failed": 0, "skipped": 0}
    errors: dict[str, int] = {}
    host_fails: dict[str, int] = {}
    host_ok: dict[str, int] = {}
    dead: set[str] = set()
    lock = asyncio.Lock()

    async def worker(wid: int, session):
        while True:
            try:
                url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            host = urlparse(url).netloc
            if host in dead:
                stats["skipped"] += 1
                continue
            proxy = random.choice(proxies) if proxies else None
            ch, err = await fetch_one(session, url, proxy)
            async with lock:
                if err:
                    stats["failed"] += 1
                    errors[err] = errors.get(err, 0) + 1
                    host_fails[host] = host_fails.get(host, 0) + 1
                    if host_ok.get(host, 0) == 0 and host_fails[host] >= HOST_FAIL_LIMIT:
                        dead.add(host)
                        logger.error(
                            f"[tgstat_fast] домен {host} исключён: "
                            f"{host_fails[host]} отказов, ни одного успеха"
                        )
                else:
                    stats["ok"] += 1
                    host_ok[host] = host_ok.get(host, 0) + 1
                    for c in ch:
                        prev = channels.get(c["username"])
                        if prev is None or c["subscribers"] > prev["subscribers"]:
                            channels[c["username"]] = c
                if on_progress:
                    on_progress(dict(stats, unique=len(channels), left=queue.qsize()))
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*[worker(i, session) for i in range(workers)])

    return {"channels": channels, **stats,
            "dead_hosts": sorted(dead), "errors": errors}
