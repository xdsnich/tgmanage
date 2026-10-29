# -*- coding: utf-8 -*-
"""Каналы от 10к по странам — через поиск TGStat в открытом окне.

Как. Окно TGStat запущено ярлыком с отладочным портом, в нём наш логин.
Мы шлём из этого окна те же запросы, что уходят при «Показать больше»:
POST /ru/channels/search, ответ — JSON {status, hasMore, nextOffset, html}
по 30 каналов. Со стороны сайта это тот же браузер и тот же логин.

Предел. Одна выдача отдаёт около тысячи каналов и дальше молчит, сколько
ни жми. Поэтому режем: страна → диапазоны подписчиков → категории → ещё
уже диапазоны. Срез, который упёрся в предел, делится сам, пока каждый
кусок не пройдёт до конца.

Куда. Новые каналы — в parsed_channels с пометкой 'tgstat-geo:US' и
папкой отстойника 'new_en': после проверки они сами уедут в 'en' или
'unusable_en'. Уже известные пропускаем, но считаем — по доле известных
видно, брали ли страну раньше.

Запуск из папки api:
    python ..\\tools\\tgstat_geo.py                  # все страны по порядку
    python ..\\tools\\tgstat_geo.py --only US,GB
"""
import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ПОРТ = int(os.getenv("TGSTAT_CDP_PORT", "9222"))
ПОИСК = "https://tgstat.com/ru/channels/search"
МИН = 10000
ПРЕДЕЛ = 990          # срез кончился на стольких — упёрся в предел выдачи, режем
СОСТОЯНИЕ = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "tgstat_geo_state.json")

# id стран в фильтре TGStat (сняты с формы поиска 29.09).
# Новой Зеландии в списке нет; Ирландию добавили — родной английский.
СТРАНЫ = [
    ("GB", 29, "Великобритания"), ("CA", 54, "Канада"),
    ("AU", 74, "Австралия"), ("IE", 63, "Ирландия"),
    ("SG", 28, "Сингапур"), ("NL", 39, "Нидерланды"), ("SE", 46, "Швеция"),
    ("NO", 37, "Норвегия"), ("DK", 61, "Дания"), ("AE", 48, "ОАЭ"),
    ("DE", 16, "Германия"), ("CH", 71, "Швейцария"),
    ("MY", 18, "Малайзия"), ("ZA", 89, "ЮАР"), ("PL", 32, "Польша"),
    ("CZ", 53, "Чехия"),
    ("IN", 8, "Индия"), ("PH", 40, "Филиппины"), ("NG", 82, "Нигерия"),
    ("PK", 67, "Пакистан"), ("BD", 52, "Бангладеш"),
    # США последними: их уже брали вручную в живом поиске.
    ("US", 9, "США"),
    # Английский язык без страны. Страну TGStat ставит не всем, и у
    # западных стран её почти нет: у Великобритании 156 каналов от 10к,
    # у Австралии — два. Основная масса англоязычных сидит здесь. id со
    # знаком минус — это язык, а не страна.
    ("EN", -12, "английский язык, любая страна"),
]
# Полосы подписчиков — первый уровень нарезки.
ПОЛОСЫ = [(10000, 15000), (15000, 25000), (25000, 50000), (50000, 100000),
          (100000, 250000), (250000, 0)]
КАТЕГОРИИ = [32, 12, 4, 46, 11, 55, 34, 8, 17, 3, 45, 44, 47, 53, 49, 7, 23,
             15, 22, 48, 21, 31, 28, 27, 13, 2, 5, 41, 38, 50, 37, 14, 33, 25,
             40, 26, 36, 9, 20, 1, 29, 16, 52, 54, 10, 51, 6]

# Разбор ответа: html приходит строкой, разбираем в странице же.
JS_FETCH = r"""async (p) => {
  const csrf = document.querySelector('input[name="_tgstat_csrk"]').value;
  const f = new URLSearchParams();
  f.append('_tgstat_csrk', csrf);
  f.append('q', ''); f.append('inAbout', '0'); f.append('categories', '');
  for (const c of (p.cats || [])) f.append('categories[]', String(c));
  f.append('countries', '');
  for (const c of (p.countries || [])) f.append('countries[]', String(c));
  f.append('languages', '');
  for (const l of (p.langs || [])) f.append('languages[]', String(l));
  f.append('channelType', 'public');
  f.append('age', '0-120'); f.append('err', '0-100'); f.append('er', '0');
  f.append('male', '0'); f.append('female', '0');
  f.append('participantsCountFrom', p.from ? String(p.from) : '');
  f.append('participantsCountTo', p.to ? String(p.to) : '');
  for (const k of ['avgReachFrom','avgReachTo','avgReach24From','avgReach24To','ciFrom','ciTo'])
    f.append(k, '');
  f.append('isVerified', '0'); f.append('isRknVerified', '0');
  f.append('isStoriesAvailable', '0');
  f.append('noRedLabel', '0'); f.append('noRedLabel', '1');
  f.append('noScam', '0'); f.append('noScam', '1');
  f.append('noDead', '0'); f.append('noDead', '1');
  f.append('page', String(p.page)); f.append('offset', String(p.offset));
  const r = await fetch(location.origin + location.pathname, {
    method: 'POST', body: f, credentials: 'include',
    headers: {'X-Requested-With': 'XMLHttpRequest',
              'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}});
  const txt = await r.text();
  let j;
  try { j = JSON.parse(txt); } catch (e) { return {http: r.status, bad: txt.slice(0, 300)}; }
  const doc = new DOMParser().parseFromString(j.html || '', 'text/html');
  const out = [], seen = new Set();
  for (const a of doc.querySelectorAll('a[href*="/channel/@"]')) {
    const m = (a.getAttribute('href') || '').match(/\/channel\/@([A-Za-z0-9_]{3,40})/);
    if (!m || seen.has(m[1].toLowerCase())) continue;
    seen.add(m[1].toLowerCase());
    let card = a.closest('.card, .peer-item, .channels-list-item, .col-lg-4, .col-md-6, li') || a.parentElement || a;
    const text = (card.textContent || '').replace(/\s+/g, ' ').trim();
    let subs = 0;
    // «18 114 подписчиков» или «18.1k подписчиков»
    let mm = text.match(/(\d{1,3}(?:[\s  ]\d{3})+|\d+)\s*(подписчик|subscriber|участник|member)/i);
    if (mm) subs = parseInt(mm[1].replace(/[\s  ]/g, ''), 10);
    if (!subs) {
      mm = text.match(/(\d+(?:[.,]\d+)?)\s*(k|к|тыс|m|м|млн)\.?\s*(подписчик|subscriber|участник|member)/i);
      if (mm) {
        const n = parseFloat(mm[1].replace(',', '.'));
        subs = Math.round(n * (/^[mм]/i.test(mm[2]) ? 1e6 : 1e3));
      }
    }
    if (!(subs > 0) || subs > 50000000) subs = 0;
    let title = '';
    for (const sel of ['.card-title', 'h5', 'h4', '.text-truncate', 'strong', 'b']) {
      const el = card.querySelector(sel);
      if (el && el.textContent.trim()) { title = el.textContent.trim(); break; }
    }
    out.push({u: m[1], t: title.slice(0, 120), s: subs, raw: text.slice(0, 160)});
  }
  return {http: r.status, status: j.status, hasMore: !!j.hasMore,
          nextPage: j.nextPage, nextOffset: j.nextOffset, items: out};
}"""


class Стоп(Exception):
    pass

запросов = [0]     # сколько поисковых запросов ушло за прогон


async def пауза(a, b):
    await asyncio.sleep(random.uniform(a, b))


async def записать(строки):
    """Новые каналы — в базу сразу, страницей. Лимит может оборвать
    прогон на любом запросе, и всё, что не записано, пропадёт: так ушли
    900 каналов с «акк 2» 29.09."""
    if not строки:
        return
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    async with S() as db:
        for u, title, subs, метка in строки:
            await db.execute(t("""
                INSERT INTO parsed_channels
                    (user_id, username, title, subscribers, has_comments,
                     search_query, folder, kind, added_at)
                VALUES (2, :u, :t, :s, false, :q, 'new_en', 'channel', now())"""),
                {"u": u, "t": title or "", "s": int(subs or 0), "q": метка})
        await db.commit()


class Прогон:
    """Состояние между запусками: где остановились в каждом срезе.

    Лимит — около тридцати страниц на аккаунт в сутки, так что страна
    проходится за несколько дней и несколько аккаунтов. Место в выдаче
    сохраняется после каждой страницы: завтра любой аккаунт продолжит
    срез с того же смещения.
    """

    def __init__(self, известные, журнал):
        self.известные = известные
        self.журнал = журнал
        try:
            s = json.load(open(СОСТОЯНИЕ, encoding="utf-8"))
            self.срезы = s.get("slices", {}) if isinstance(s, dict) else {}
        except Exception:
            self.срезы = {}
        self.новых = self.было = 0

    def сохранить(self):
        json.dump({"slices": self.срезы}, open(СОСТОЯНИЕ, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=0)

    async def срез(self, page, ключ, p, метка):
        """Пройти срез до конца или до лимита. Вернёт запись среза."""
        s = self.срезы.setdefault(ключ, {"page": 0, "offset": 0, "n": 0,
                                         "done": False, "capped": False})
        if s["done"]:
            return s
        ошибок = 0
        while True:
            try:
                r = await page.evaluate(JS_FETCH, dict(p, page=s["page"], offset=s["offset"]))
            except Exception as e:
                r = {"http": 0, "bad": str(e)[:200]}
            запросов[0] += 1
            if r.get("status") == "restricted":
                self.сохранить()
                raise Стоп("дневной лимит поиска кончился (запросов за прогон: %d)" % запросов[0])
            if r.get("status") != "ok":
                ошибок += 1
                self.журнал("   ! ответ не тот: http=%s %s" % (r.get("http"), (r.get("bad") or "")[:150]))
                if ошибок >= 2:
                    self.сохранить()
                    raise Стоп("сайт отвечает не так — останавливаюсь, чтобы не спалить логин")
                await пауза(120, 240)
                continue
            ошибок = 0
            items = r["items"]
            свежие = []
            for it in items:
                k = it["u"].lower()
                if k in self.известные:
                    self.было += 1
                    continue
                self.известные.add(k)
                self.новых += 1
                свежие.append((it["u"], it["t"], it["s"], метка))
            await записать(свежие)
            s["n"] += len(items)
            if not r["hasMore"] or not items:
                s["done"] = True
                s["capped"] = s["n"] >= ПРЕДЕЛ
                self.сохранить()
                return s
            s["page"], s["offset"] = r["nextPage"], r["nextOffset"]
            self.сохранить()
            await пауза(4, 9)

    async def страна(self, page, cc, cid):
        метка = "tgstat-geo:%s" % cc

        async def пройти(lo, hi, cat, целиком=False):
            ключ = "%s|%s-%s|%s" % (cc, lo, hi, cat or "")
            p = {"from": lo, "to": hi or None, "cats": [cat] if cat else []}
            if cid < 0:
                p["langs"] = [-cid]
            else:
                p["countries"] = [cid]
            было_done = self.срезы.get(ключ, {}).get("done")
            s = await self.срез(page, ключ, p, метка)
            if not было_done:
                self.журнал("   %-3s %-15s кат=%-4s → %4d%s   (новых всего %d, уже было %d)"
                            % (cc, "%d-%s" % (lo, hi or "∞"), cat or "все", s["n"],
                               "  УПЁРСЯ — режу" if s["capped"] else "",
                               self.новых, self.было))
                await пауза(15, 35)
            if not s["capped"]:
                return
            # Упёрлись в предел выдачи — режем дальше.
            if целиком:
                for lo2, hi2 in ПОЛОСЫ:
                    await пройти(lo2, hi2, None)
            elif not cat:
                for c in КАТЕГОРИИ:
                    await пройти(lo, hi, c)
            elif hi and hi - lo > 2000:
                mid = (lo + hi) // 2
                await пройти(lo, mid, cat)
                await пройти(mid, hi, cat)
            elif not hi:
                await пройти(lo, lo * 3, cat)
                await пройти(lo * 3, 0, cat)

        # Если страну уже начали полосами — продолжаем полосами.
        if any(k.startswith(cc + "|") and not k.startswith("%s|%d-0|" % (cc, МИН))
               for k in self.срезы):
            for lo, hi in ПОЛОСЫ:
                await пройти(lo, hi, None)
            return
        # Иначе сперва целиком. У большинства стран каналов от 10к меньше
        # сотни: целиком это 1-4 запроса, а полосами — всегда шесть, даже
        # пустые. Режем, только если страна упёрлась в предел выдачи.
        await пройти(МИН, 0, None, целиком=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="страны через запятую")
    ap.add_argument("--port", type=int, default=ПОРТ, help="окно: 9222 первое, 9223 акк 2 ...")
    a = ap.parse_args()

    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    from playwright.async_api import async_playwright

    async with S() as db:
        известные = set(r[0].lower() for r in (await db.execute(t(
            "SELECT username FROM parsed_channels WHERE username IS NOT NULL"))).all())

    лог_путь = СОСТОЯНИЕ.replace(".json", ".log")

    def журнал(s):
        строка = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), s)
        print(строка, flush=True)
        with open(лог_путь, "a", encoding="utf-8") as f:
            f.write(строка + "\n")

    страны = СТРАНЫ
    if a.only:
        нужные = [x.strip().upper() for x in a.only.split(",")]
        страны = [next(s for s in СТРАНЫ if s[0] == c) for c in нужные]

    прогон = Прогон(известные, журнал)
    журнал("══ окно %d, стран в очереди %d ══" % (a.port, len(страны)))
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp("http://localhost:%d" % a.port, timeout=15000)
        ctx = br.contexts[0]
        # Своя вкладка: в соседних может работать человек, и чужую
        # вкладку мы бы увели со страницы или слали бы запросы не туда.
        page = await ctx.new_page()
        await page.goto(ПОИСК, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        try:
            for cc, cid, имя in страны:
                журнал("── %s (%s)" % (имя, cc))
                await прогон.страна(page, cc, cid)
                await пауза(30, 60)
        except Стоп as e:
            журнал("СТОП: %s" % e)
        finally:
            try:
                await page.close()
            except Exception:
                pass
    журнал("ИТОГ прогона: новых %d, уже было в базе %d" % (прогон.новых, прогон.было))


asyncio.run(main())
