# -*- coding: utf-8 -*-
"""Сборщик каналов с той страницы TGStat, которую листает человек.

Идея. У живого пользователя, зашедшего под своим аккаунтом, нет тех
лимитов, в которые упирается скрейпер: он листает сколько хочет и жмёт
«показать больше» сколько хочет. Поэтому мы не ходим на сайт сами, а
подключаемся к его же браузеру и снимаем то, что уже открыто на экране.

Со стороны TGStat это один обычный браузер с обычным логином — никаких
запросов от нас, а значит ни 429, ни капчи, ни блокировок.

Работает так: раз в пару секунд читаем карточки с живой страницы,
отбрасываем уже виденные и дописываем новые в parsed_channels. Ничего не
нажимаем и не прокручиваем — это делает человек.

Запуск:
    python tgstat_harvest.py                 # смотреть и собирать
    python tgstat_harvest.py --dry           # только смотреть, не писать
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

# Четыре окна — по одному на аккаунт TGStat, у каждого свой порт. Под
# логином глубина выдачи ограничена, поэтому четыре аккаунта дают вчетверо
# больше страниц, чем один. Снимаем со всех сразу: человек листает то
# одно окно, то другое, а сборщику всё равно, откуда пришла карточка.
CDP_ПОРТЫ = [int(x) for x in
             os.getenv("TGSTAT_CDP_PORTS", "9222,9223,9224,9225").split(",") if x.strip()]
ОПРОС_СЕК = 2.0

# Чтение карточек с живой страницы. Опираемся на ссылку вида /channel/@ник
# — она есть в любой раскладке TGStat, и уже от неё поднимаемся к карточке.
JS = r"""() => {
  const out = [];
  const seen = new Set();
  const anchors = document.querySelectorAll('a[href*="/channel/@"]');
  for (const a of anchors) {
    const m = (a.getAttribute('href') || '').match(/\/channel\/@([A-Za-z0-9_]{3,40})/);
    if (!m) continue;
    const uname = m[1];
    if (seen.has(uname.toLowerCase())) continue;
    seen.add(uname.toLowerCase());

    let card = a.closest('.card, .peer-item, .channels-list-item, .col-lg-4, .col-md-6, li');
    if (!card) card = a.parentElement || a;
    const text = (card.innerText || '').replace(/\s+/g, ' ').trim();

    // Подписчики. Число ОБЯЗАНО быть разбито на разряды по три цифры —
    // иначе разбор ползёт влево и хватает цифры из названия канала: на
    // «The Walking Dead Season 1-11 18 114 подписчиков» прежнее выражение
    // склеивало 1 + 18 + 114 и выдавало 1 118 114 вместо 18 114.
    let subs = 0;
    const RE = /(\d{1,3}(?:[\s\u00a0\u202f.,]\d{3})*)\s*(k|к|тыс|m|м|млн)?\s*(подписчик|subscriber|участник|member)/i;
    const mm = text.match(RE);
    if (mm) {
      let n = parseInt(mm[1].replace(/[\s\u00a0\u202f.,]/g, ''), 10);
      const suf = (mm[2] || '').toLowerCase();
      if (suf.startsWith('k') || suf.startsWith('к') || suf.startsWith('тыс')) n *= 1000;
      else if (suf.startsWith('m') || suf.startsWith('м')) n *= 1000000;
      subs = n;
    }
    // Столько подписчиков нет ни у одного канала Telegram — значит
    // схватили не то число. Лучше ноль, чем выдумка: ноль потом уточнит
    // проверка канала, а выдумка пролезет в отбор для подписок.
    if (!(subs > 0) || subs > 50000000) subs = 0;

    // Название
    let title = '';
    for (const sel of ['.card-title', 'h5', 'h4', 'strong', 'b', '.text-truncate']) {
      const el = card.querySelector(sel);
      if (el && (el.innerText || '').trim()) { title = el.innerText.trim(); break; }
    }
    if (!title) title = (a.innerText || '').trim();

    // Признак чата у канала — значит комментарии открыты.
    const чат = /чат\s*канала|комментар|обсужд|linked\s*chat|discussion/i.test(text)
             || !!card.querySelector('a[href*="/chat/@"]');

    out.push({u: uname, t: title.slice(0, 120), s: subs, c: чат});
  }
  return out;
}"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="не писать в базу")
    ap.add_argument("--min-subs", type=int, default=0,
                    help="не сохранять мельче этого")
    a = ap.parse_args()

    from playwright.async_api import async_playwright
    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    async with Session() as db:
        известные = set(r[0].lower() for r in (await db.execute(sa_text(
            "SELECT username FROM parsed_channels WHERE username IS NOT NULL"))).all())
    print("в базе уже: %d каналов" % len(известные), flush=True)

    async def подключиться():
        """Собрать связи со всеми открытыми окнами. Закрытые пропускаем."""
        живые = {}
        for порт in CDP_ПОРТЫ:
            try:
                живые[порт] = await pw.chromium.connect_over_cdp(
                    "http://localhost:%d" % порт, timeout=8000)
            except Exception:
                pass
        return живые

    async with async_playwright() as pw:
        окна = await подключиться()
        if not окна:
            print()
            print("не вижу ни одного окна.")
            print("Запусти ярлыки «TGStat акк 2/3/4.bat» с рабочего стола,")
            print("залогинься в каждом под своим аккаунтом и запусти меня снова.")
            return
        print("подключился к окнам: %s" % ", ".join(str(p) for p in sorted(окна)),
              flush=True)
        print("листай и жми «показать больше» — я подбираю. Ctrl+C чтобы закончить.",
              flush=True)
        print(flush=True)

        новых = 0
        битых = [0]
        мелких = 0
        с_чатом = 0
        крупных = 0
        пусто_подряд = 0
        try:
            круг = 0
            while True:
                круг += 1
                # Окна появляются и исчезают: человек запускает новое,
                # перезапускает старое. Раз в полминуты проверяем все
                # порты заново. Раньше эта проверка стояла ниже и при
                # закрытых окнах была недостижима — сборщик слеп навсегда.
                if круг % 15 == 1 and len(окна) < len(CDP_ПОРТЫ):
                    for порт in CDP_ПОРТЫ:
                        if порт in окна and окна[порт].is_connected():
                            continue
                        окна.pop(порт, None)
                        try:
                            окна[порт] = await pw.chromium.connect_over_cdp(
                                "http://localhost:%d" % порт, timeout=5000)
                            print("… подключился к окну %d" % порт, flush=True)
                        except Exception:
                            pass

                страницы = []
                for порт, br in list(окна.items()):
                    if not br.is_connected():
                        окна.pop(порт, None)
                        continue
                    for ctx in br.contexts:
                        for p in ctx.pages:
                            try:
                                if "tgstat" in (p.url or ""):
                                    страницы.append(p)
                            except Exception:
                                pass
                if not страницы:
                    пусто_подряд += 1
                    if пусто_подряд % 10 == 1:
                        print("… вкладки с TGStat не вижу, жду", flush=True)
                    await asyncio.sleep(ОПРОС_СЕК)
                    continue
                пусто_подряд = 0

                партия = []
                for p in страницы:
                    try:
                        for c in (await p.evaluate(JS)) or []:
                            if c["u"].lower() in известные:
                                continue
                            известные.add(c["u"].lower())
                            партия.append(c)
                    except Exception:
                        continue

                if партия:
                    годные = [c for c in партия if c["s"] >= a.min_subs]
                    мелких += len(партия) - len(годные)
                    if годные and not a.dry:
                        for c in годные:
                            # Каждая карточка отдельной сессией: кривая
                            # строка не должна уносить с собой партию, как
                            # это случилось с @alkhim6666.
                            try:
                                s_ = int(c.get("s") or 0)
                                if s_ < 0 or s_ > 50000000:
                                    s_ = 0
                                async with Session() as db:
                                    await db.execute(sa_text("""
                                        INSERT INTO parsed_channels
                                            (user_id, username, title, subscribers,
                                             has_comments, search_query, added_at)
                                        VALUES (2, :u, :t, :s, :c,
                                                'tgstat-живой-поиск', now())
                                        ON CONFLICT DO NOTHING"""),
                                        {"u": c["u"], "t": (c.get("t") or "")[:120],
                                         "s": s_, "c": bool(c.get("c"))})
                                    await db.commit()
                            except Exception:
                                битых[0] += 1
                    новых += len(годные)
                    крупных += sum(1 for c in годные if c["s"] >= 100000)
                    с_чатом += sum(1 for c in годные if c["c"] and c["s"] >= 100000)
                    самый = max(годные, key=lambda x: x["s"]) if годные else None
                    print("[%s] +%-3d   всего %-5d   от 100к %-4d   из них с чатом %-4d   %s"
                          % (datetime.now().strftime("%H:%M:%S"), len(годные), новых,
                             крупных, с_чатом,
                             ("@%s %d" % (самый["u"], самый["s"])) if самый else ""),
                          flush=True)
                await asyncio.sleep(ОПРОС_СЕК)
        except KeyboardInterrupt:
            pass
        finally:
            print()
            print("=" * 56)
            print("  собрано новых каналов:   %d" % новых)
            print("  из них от 100к:          %d" % крупных)
            print("  из них с чатом:          %d  ← в эти можно писать" % с_чатом)
            if мелких:
                print("  отсеяно мелких:          %d" % мелких)
            if битых[0]:
                print("  пропущено кривых:        %d" % битых[0])
            if a.dry:
                print("  (пробный прогон — в базу ничего не записано)")


asyncio.run(main())
