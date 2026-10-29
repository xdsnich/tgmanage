# -*- coding: utf-8 -*-
"""Язык и число подписчиков каналов — по веб-странице t.me/s/<канал>.

Аккаунты не нужны: страница публичная. Язык — langdetect по последним
постам (с названием и описанием), как в существующем определителе
парсера. Подписчиков Telegram пишет в шапке той же страницы.

Берём каналы без языка, крупные первыми. Пишем language и, если удалось
прочитать, subscribers. Ничего не удаляем.

Запуск из папки api:
    python ..\\tools\\channel_lang.py            # все без языка, от 10к
    python ..\\tools\\channel_lang.py --min 0    # и мелкие тоже
"""
import argparse, asyncio, html as H, os, re, sys
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

ЛОГ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_lang.log")
ПОТОКОВ = 6
ПАЧКА = 300


def журнал(s):
    строка = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), s)
    print(строка, flush=True)
    with open(ЛОГ, "a", encoding="utf-8") as f:
        f.write(строка + "\n")


def подписчики(page: str):
    # <div class="tgme_header_counter">12.3K subscribers</div> или
    # <span class="counter_value">12.3K</span> <span class="counter_type">subscribers</span>
    m = re.search(r'counter_value">([\d.,]+)\s*([KM]?)</span>\s*<span class="counter_type">subscribers', page)
    if not m:
        m = re.search(r'tgme_page_extra">\s*([\d\s.,]+)\s*([KM]?)\s*subscribers', page)
    if not m:
        return None
    n = float(m.group(1).replace(" ", "").replace(",", ""))
    return int(n * {"K": 1e3, "M": 1e6}.get(m.group(2), 1))


def текст(page: str) -> str:
    части = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', page, re.S)[-15:]
    шапка = re.findall(r'<div class="tgme_channel_info_description">(.*?)</div>', page, re.S)
    t = " ".join(шапка + части)
    t = re.sub(r"<[^>]+>", " ", t)
    t = H.unescape(t)
    t = re.sub(r"https?://\S+|@\w+|#\w+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


async def один(s, u):
    from langdetect import detect, LangDetectException
    try:
        async with s.get("https://t.me/s/%s" % u, timeout=20) as r:
            if r.status != 200:
                return u, None, None, "http %d" % r.status
            page = await r.text()
    except Exception as e:
        return u, None, None, type(e).__name__
    subs = подписчики(page)
    t = текст(page)
    if len(t) < 40:
        return u, None, subs, "мало текста"
    try:
        return u, detect(t), subs, "ok"
    except LangDetectException:
        return u, None, subs, "не определён"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=10000)
    a = ap.parse_args()

    import aiohttp
    from collections import Counter
    from langdetect import DetectorFactory
    DetectorFactory.seed = 0          # иначе langdetect отвечает по-разному на один текст
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S

    итого = Counter()
    журнал("══ язык каналов: от %d подписчиков ══" % a.min)
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
        while True:
            async with S() as db:
                пачка = [r[0] for r in (await db.execute(t("""
                    SELECT username FROM parsed_channels
                    WHERE (language IS NULL OR language = '') AND username IS NOT NULL AND username <> ''
                      AND COALESCE(subscribers, 0) >= :m
                      AND username NOT IN (SELECT unnest(CAST(:skip AS text[])))
                    ORDER BY subscribers DESC NULLS LAST LIMIT :n"""),
                    {"m": a.min, "n": ПАЧКА, "skip": list(итого.get("_skip", []))})).all()]
            if not пачка:
                break
            sem = asyncio.Semaphore(ПОТОКОВ)

            async def с_ограничением(u):
                async with sem:
                    r = await один(s, u)
                    await asyncio.sleep(0.3)
                    return r
            рез = await asyncio.gather(*(с_ограничением(u) for u in пачка))
            async with S() as db:
                for u, lang, subs, why in рез:
                    итого[why] += 1
                    # Не определили — пишем «?», чтобы не брать канал по кругу.
                    await db.execute(t("""
                        UPDATE parsed_channels SET language = :l,
                               subscribers = COALESCE(:s, subscribers)
                        WHERE username = :u"""), {"l": lang or "?", "s": subs, "u": u})
                    if lang:
                        итого["lang:" + lang] += 1
                await db.commit()
            языки = sorted(((k[5:], v) for k, v in итого.items() if k.startswith("lang:")), key=lambda x: -x[1])[:8]
            журнал("пройдено %d: ok %d, мало текста %d, сбоев %d | %s" % (
                sum(v for k, v in итого.items() if not k.startswith("lang:") and k != "_skip"),
                итого["ok"], итого["мало текста"],
                sum(v for k, v in итого.items() if k not in ("ok", "мало текста") and not k.startswith("lang:")),
                ", ".join("%s %d" % x for x in языки)))
    журнал("ИТОГ: каналы без языка кончились")


asyncio.run(main())
