# -*- coding: utf-8 -*-
"""Весь каталог tgstat.ru — все категории, до последнего канала.

Каталог — не поиск. Поиск у бесплатного аккаунта даёт ~30 запросов в
сутки, а каталог категории (/news, /blogs, ...) листается кнопкой
«Показать больше» через POST /{категория}/items без этого лимита.
Проверено 29.09: «Рукоделие» пролистано целиком — 38 страниц, 3774
канала, до самых мелких, ни одного отказа.

Как. Окно Chrome с отладочным портом и нашим логином (у человека там же
VPN). Работаем в своей вкладке, чужие не трогаем. Страница — ~100
каналов, отсортированных по подписчикам.

Куда. parsed_channels: пометка 'tgstat.ru:{категория}', папка 'new_ru' —
после проверки годные сами уедут в 'ru'. Пишем постранично: оборвётся
прогон — ничего не пропадёт. Уже известные каналы пропускаем. Каналы без
публичного адреса (ссылка вида /channel/AbCd...) пропускаем тоже: ни
вступить по адресу, ни написать туда мы не сможем.

Место в каждой категории хранится в tgstat_ru_state.json — повторный
запуск продолжит с той же страницы.

Запуск из папки api:
    python ..\\tools\\tgstat_ru_catalog.py
    python ..\\tools\\tgstat_ru_catalog.py --only news,blogs
    python ..\\tools\\tgstat_ru_catalog.py --port 9223
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ХОСТ = "https://tgstat.ru"
# Мельче не берём. Каталог отсортирован по подписчикам, поэтому как только
# страница опустилась ниже порога, категория для нас кончилась.
МИН = int(os.getenv("TGSTAT_MIN_SUBS", "10000"))
СОСТОЯНИЕ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tgstat_ru_state.json")
ЛОГ = СОСТОЯНИЕ.replace(".json", ".log")
ПАПКА = "new_ru"
ПРЕФИКС = "tgstat.ru"

# Тот же каталог есть и на других хостах TGStat (проверено 29.09):
# tgstat.com — международный, остальное — страны. Хост → (имя для файлов
# состояния, папка отстойника). После проверки папка теряет «new_».
ХОСТЫ = {
    "tgstat.ru":      ("ru",   "new_ru"),
    "tgstat.com":     ("com",  "new_global"),
    "uk.tgstat.com":  ("uk",   "new_uk"),
    "kaz.tgstat.com": ("kaz",  "new_kz"),
    "uz.tgstat.com":  ("uz",   "new_uz"),
    "by.tgstat.com":  ("by",   "new_by"),
    "kg.tgstat.com":  ("kg",   "new_kg"),
    "in.tgstat.com":  ("in",   "new_in"),
    "ir.tgstat.com":  ("ir",   "new_ir"),
    "cn.tgstat.com":  ("cn",   "new_cn"),
    "et.tgstat.com":  ("et",   "new_et"),
}

# Категории каталога — сняты с главной tgstat.ru 29.09. Крупные и самые
# полезные для нас — первыми.
КАТЕГОРИИ = [
    "news", "blogs", "entertainment", "edutainment", "politics", "economics",
    "crypto", "business", "psychology", "health", "beauty", "food", "travels",
    "sport", "games", "video", "music", "tech", "apps", "education", "courses",
    "career", "marketing", "sales", "books", "art", "design", "pics", "quotes",
    "babies", "construction", "handmade", "nature", "religion", "esoterics",
    "medicine", "law", "language", "transport", "telegram", "instagram",
    "gambling", "shock", "adult", "erotica", "darknet", "other",
]

JS = r"""async (p) => {
  const el = document.querySelector('input[name="_tgstat_csrk"]');
  const body = new URLSearchParams({_tgstat_csrk: el ? el.value : '', peer_type: 'channel',
      sort_channel: 'members', sort_chat: 'members', page: String(p), offset: '0'});
  const r = await fetch(location.origin + location.pathname + '/items', {method: 'POST', body,
      credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest',
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}});
  const t = await r.text();
  let j; try { j = JSON.parse(t); } catch (e) { return {http: r.status, bad: t.slice(0, 200)}; }
  const doc = new DOMParser().parseFromString(j.html || '', 'text/html');
  const out = [];
  for (const box of doc.querySelectorAll('.peer-item-box')) {
    const a = box.querySelector('a[href*="/channel/"]');
    if (!a) continue;
    const m = (a.getAttribute('href') || '').match(/\/channel\/@([A-Za-z0-9_]{3,40})/);
    const title = (box.querySelector('.font-16') || {}).textContent || '';
    // Подписчики — ровно в <b> перед словом «подписчиков»; остальные
    // цифры карточки (телефоны в описании и т.п.) сюда не попадают.
    let subs = 0;
    for (const d of box.querySelectorAll('.font-12')) {
      if (/подписчик/i.test(d.textContent)) {
        const b = d.querySelector('b');
        subs = parseInt(((b ? b.textContent : d.textContent) || '').replace(/\D/g, ''), 10) || 0;
        break;
      }
    }
    const last = (box.querySelector('[data-original-title*="Последняя публикация"]') || {}).textContent || '';
    // У закрытого канала вместо @адреса в ссылке хеш — это и есть хеш его
    // инвайта: t.me/joinchat/<хеш> (проверено 01.10 на «Топор Live»).
    const hm = m ? null : (a.getAttribute('href') || '').match(/\/channel\/([A-Za-z0-9_-]{8,64})/);
    out.push({u: m ? m[1] : null, h: hm ? hm[1] : null, t: title.trim().slice(0, 120), s: subs, last: last.trim()});
  }
  return {http: r.status, status: j.status, hasMore: !!j.hasMore, next: j.nextPage, items: out};
}"""


def давно(текст: str):
    """«5 дней» → дата последнего поста. Не разобрали — None."""
    m = re.match(r"(\d+)\s*(\S+)", текст or "")
    if not m:
        return None
    n, ед = int(m.group(1)), m.group(2).lower()
    if ед.startswith("мин"):
        d = timedelta(minutes=n)
    elif ед.startswith("час"):
        d = timedelta(hours=n)
    elif ед.startswith("д"):
        d = timedelta(days=n)
    elif ед.startswith("нед"):
        d = timedelta(weeks=n)
    elif ед.startswith("мес"):
        d = timedelta(days=30 * n)
    elif ед.startswith(("год", "лет")):
        d = timedelta(days=365 * n)
    else:
        return None
    return datetime.utcnow() - d


class Стоп(Exception):
    pass


def журнал(s):
    строка = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), s)
    print(строка, flush=True)
    with open(ЛОГ, "a", encoding="utf-8") as f:
        f.write(строка + "\n")


ЗАКРЫТЫЕ = False


async def создать_таблицу_закрытых():
    """Закрытые каналы — отдельно от parsed_channels: там всё живёт на
    @адресах, и «адрес» вида joinchat/… сломал бы кампанию, вздумай она
    взять такой канал. Пригодятся, когда появится вступление по инвайту."""
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    async with S() as db:
        await db.execute(t("""
            CREATE TABLE IF NOT EXISTS tgstat_private_channels (
                id             SERIAL PRIMARY KEY,
                hash           VARCHAR(64)  NOT NULL UNIQUE,
                invite_link    VARCHAR(128) NOT NULL,
                title          VARCHAR(256) NOT NULL DEFAULT '',
                subscribers    INTEGER      NOT NULL DEFAULT 0,
                last_post_date TIMESTAMP    NULL,
                source         VARCHAR(128) NOT NULL DEFAULT '',
                added_at       TIMESTAMP    NOT NULL DEFAULT now())"""))
        await db.commit()


async def записать_закрытые(строки):
    if not строки:
        return 0
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    вставлено = 0
    async with S() as db:
        for h, title, subs, last, метка in строки:
            res = await db.execute(t("""
                INSERT INTO tgstat_private_channels
                    (hash, invite_link, title, subscribers, last_post_date, source)
                VALUES (:h, :l, :t, :s, :lp, :q)
                ON CONFLICT (hash) DO NOTHING"""),
                {"h": h, "l": "https://t.me/joinchat/" + h, "t": title or "",
                 "s": int(subs or 0), "lp": last, "q": метка})
            вставлено += res.rowcount or 0
        await db.commit()
    return вставлено


async def записать(строки):
    if not строки:
        return 0
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    вставлено = 0
    async with S() as db:
        for u, title, subs, last, метка in строки:
            # Повтор проверяем ещё и в базе, а не только по списку в
            # памяти: хосты идут параллельно, и соседний процесс мог
            # записать этот канал минуту назад.
            res = await db.execute(t("""
                INSERT INTO parsed_channels
                    (user_id, username, title, subscribers, has_comments,
                     last_post_date, search_query, folder, kind, added_at)
                SELECT 2, CAST(:u AS VARCHAR), CAST(:t AS VARCHAR), CAST(:s AS INTEGER),
                       false, CAST(:lp AS TIMESTAMP), CAST(:q AS VARCHAR),
                       CAST(:f AS VARCHAR), 'channel', now()
                WHERE NOT EXISTS (SELECT 1 FROM parsed_channels
                                  WHERE lower(username) = lower(CAST(:u AS VARCHAR)))"""),
                {"u": u, "t": title or "", "s": int(subs or 0), "lp": last, "q": метка,
                 "f": ПАПКА})
            вставлено += res.rowcount or 0
        await db.commit()
    return вставлено


КАПЧА_ЖДАТЬ_ЧАСОВ = 12


async def ждать_капчу(page, cat, стр):
    """Ждать, пока человек решит капчу в окне. Проверка — раз в 2 минуты."""
    журнал("КАПЧА на %s (%s стр %d) — нужно решить её в окне Chrome, дальше пойду сам"
           % (ПРЕФИКС, cat, стр))
    начало = datetime.now()
    while (datetime.now() - начало).total_seconds() < КАПЧА_ЖДАТЬ_ЧАСОВ * 3600:
        await asyncio.sleep(120)
        try:
            await page.goto("%s/%s" % (ХОСТ, cat), wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            r = await page.evaluate(JS, стр)
        except Exception:
            continue
        if r.get("status") == "ok":
            журнал("капча снята — продолжаю %s с %s стр %d" % (ПРЕФИКС, cat, стр))
            await asyncio.sleep(random.uniform(5, 15))
            return
    raise Стоп("капчу не решили за %d ч" % КАПЧА_ЖДАТЬ_ЧАСОВ)


async def категория(page, cat, состояние, известные, счёт):
    s = состояние.setdefault(cat, {"page": 1, "n": 0, "new": 0, "done": False})
    # Страница 0 — первая сотня, самые крупные каналы. Сайт рисует её сразу
    # при открытии категории, а «показать больше» начинается уже с 1. До
    # 29.09 21:00 мы начинали с 1 и теряли верхушку каждой категории: у
    # Украины в «Познавательном» это каналы от 99к до 8к, а записано было 0.
    if s["done"] and s.get("p0"):
        return
    await page.goto("%s/%s" % (ХОСТ, cat), wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(random.randint(1500, 2500))
    метка = "%s:%s" % (ПРЕФИКС, cat)
    ошибок = 0
    стр = s["page"] if s.get("p0") else 0
    while True:
        try:
            r = await page.evaluate(JS, стр)
        except Exception as e:
            r = {"http": 0, "bad": str(e)[:200]}
        if r.get("status") == "restricted":
            # Это капча «Подозрение на робота» (429). Решает её человек в
            # окне; мы ждём и раз в две минуты пробуем снова, а когда она
            # решена — продолжаем с той же страницы. Сами её не обходим.
            await ждать_капчу(page, cat, стр)
            continue
        if r.get("status") != "ok":
            ошибок += 1
            журнал("   ! %s стр %d: http=%s %s" % (cat, стр, r.get("http"), (r.get("bad") or "")[:120]))
            if ошибок >= 3:
                # Одна битая категория не должна хоронить остальные: у
                # tgstat.com «shock» стабильно отвечает 500 (29.09). Пропускаем
                # её, не отмечая пройденной, — следующий запуск попробует снова.
                журнал("── %s пропущена: сервер трижды ответил ошибкой, вернусь при следующем запуске" % cat)
                json.dump(состояние, open(СОСТОЯНИЕ, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
                return
            await asyncio.sleep(random.uniform(60, 150))
            # страница могла протухнуть (токен) — перезагружаем
            await page.goto("%s/%s" % (ХОСТ, cat), wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            # Капча бывает и под видом ошибки 500: помеченной сессии /items
            # отвечает падением, а сама страница — «Подозрение на робота».
            # Так было 29.09 в 22:07 на «акк 2». Это не битая категория.
            try:
                заголовок = await page.title()
            except Exception:
                заголовок = ""
            # tgstat.com под капчей уводит категорию на /ru/error?category=…
            # с заголовком «Страница не существует — 404», а не на капчу.
            if "робота" in заголовок or "/error" in (page.url or ""):
                await ждать_капчу(page, cat, стр)
                ошибок = 0
            continue
        ошибок = 0
        свежие = []
        закрытые = []
        for it in r["items"]:
            if it["s"] < МИН:
                continue
            if ЗАКРЫТЫЕ:
                # Режим закрытых: берём только каналы без адреса, с хешем.
                if not it["u"] and it.get("h"):
                    s["n"] += 1
                    закрытые.append((it["h"], it["t"], it["s"], давно(it["last"]), метка))
                continue
            s["n"] += 1
            if not it["u"]:
                счёт["приватных"] += 1
                continue
            k = it["u"].lower()
            if k in известные:
                счёт["было"] += 1
                continue
            известные.add(k)
            свежие.append((it["u"], it["t"], it["s"], давно(it["last"]), метка))
        вставлено_откр = await записать(свежие)
        вставлено_закр = await записать_закрытые(закрытые)
        вставлено = вставлено_откр + вставлено_закр
        s["new"] += вставлено
        счёт["новых"] += вставлено
        счёт["было"] += (len(свежие) - вставлено_откр) + (len(закрытые) - вставлено_закр)
        мин = min((x["s"] for x in r["items"] if x["s"]), default=0)
        ниже = bool(r["items"]) and мин < МИН
        конец = not r["hasMore"] or not r["items"] or ниже
        if стр % 10 == 0 or конец:
            журнал("   %-14s стр %-4d от 10к %-6d новых %-6d   (размер каналов уже ~%d)   всего новых %d"
                   % (cat, стр, s["n"], s["new"], мин, счёт["новых"]))
        if стр == 0:
            s["p0"] = True
            if s["done"] or конец:
                s["done"] = True
                json.dump(состояние, open(СОСТОЯНИЕ, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
                журнал("── %s готова: %d каналов, новых %d" % (cat, s["n"], s["new"]))
                return
            стр = s["page"]      # дальше — откуда остановились (свежая: 1)
        elif конец:
            s["done"] = True
            json.dump(состояние, open(СОСТОЯНИЕ, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            журнал("── %s готова: %d каналов, новых %d" % (cat, s["n"], s["new"]))
            return
        else:
            s["page"] = стр = r["next"] or стр + 1
        json.dump(состояние, open(СОСТОЯНИЕ, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        # Как человек, который жмёт «показать больше»: секунда-три, и
        # время от времени отвлекается.
        # Пять полос с паузой в полсекунды поймали капчу за 15 минут
        # (29.09 21:07). Одна полоса с паузой в 1.5-3.5 с до этого шла без
        # неё. Держим середину и две полосы.
        # 01.10: на 1.5-3 с капча прилетала всем окнам каждые 7-10 минут.
        await asyncio.sleep(random.uniform(3.0, 6.0))
        if стр % 25 == 0:
            await asyncio.sleep(random.uniform(30, 60))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--only", help="категории через запятую")
    ap.add_argument("--host", default="tgstat.ru", choices=sorted(ХОСТЫ),
                    help="какой каталог: tgstat.ru, tgstat.com, uk.tgstat.com ...")
    ap.add_argument("--part", default="",
                    help="метка половины, когда один хост идёт в два окна: свой файл "
                         "состояния, чтобы процессы не затирали прогресс друг друга")
    ap.add_argument("--private", action="store_true",
                    help="собрать только закрытые каналы (без @адреса) — с хешем инвайта")
    a = ap.parse_args()

    global ХОСТ, СОСТОЯНИЕ, ЛОГ, ПАПКА, ПРЕФИКС, ЗАКРЫТЫЕ
    if a.private:
        ЗАКРЫТЫЕ = True
        a.part = "priv" + a.part      # свой файл состояния: свой проход
        await создать_таблицу_закрытых()
    имя, ПАПКА = ХОСТЫ[a.host]
    ХОСТ = "https://" + a.host
    ПРЕФИКС = a.host
    СОСТОЯНИЕ = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tgstat_%s%s_state.json" % (имя, a.part))
    ЛОГ = СОСТОЯНИЕ.replace(".json", ".log")

    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    from playwright.async_api import async_playwright

    async with S() as db:
        известные = set(r[0].lower() for r in (await db.execute(t(
            "SELECT username FROM parsed_channels WHERE username IS NOT NULL"))).all())
    try:
        состояние = json.load(open(СОСТОЯНИЕ, encoding="utf-8"))
    except Exception:
        состояние = {}
    cats = [c.strip() for c in a.only.split(",")] if a.only else КАТЕГОРИИ
    счёт = {"новых": 0, "было": 0, "приватных": 0}
    журнал("══ каталог %s: категорий %d, в базе уже %d каналов ══" % (a.host, len(cats), len(известные)))

    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp("http://localhost:%d" % a.port, timeout=20000)
        page = await br.contexts[0].new_page()
        try:
            for cat in cats:
                с = состояние.get(cat, {})
                if not (с.get("done") and с.get("p0")):
                    await категория(page, cat, состояние, известные, счёт)
                    await asyncio.sleep(random.uniform(1, 3))
        except Стоп as e:
            журнал("СТОП: %s" % e)
        finally:
            json.dump(состояние, open(СОСТОЯНИЕ, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            try:
                await page.close()
            except Exception:
                pass
    журнал("ИТОГ прогона: новых %d, уже было %d, без адреса %d"
           % (счёт["новых"], счёт["было"], счёт["приватных"]))


asyncio.run(main())
