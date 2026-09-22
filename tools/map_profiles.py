# -*- coding: utf-8 -*-
"""Кто залогинен в профиле CloakBrowser.

Telegram Web держит авторизацию в localStorage своего origin: основной
аккаунт под ключом `user_auth`, дополнительные — `account2_user_auth`,
`account3_user_auth` и так далее. Внутри лежит id пользователя, а он у нас
есть в accounts.tg_id — значит соответствие «профиль ↔ аккаунт» читается
без единого клика по интерфейсу.

Порядок на профиль: запустить, подключиться по CDP, открыть web.telegram.org,
прочитать ключи, погасить. Ничего не нажимаем и ничего не меняем.

Профиль на мёртвом прокси открыть страницу не сможет — origin не создастся,
и localStorage будет недоступен. Такие сначала надо перевести на живой адрес.
"""
import asyncio
import json
import sys
import os
import argparse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "http://localhost:8080"
OUT = r"C:\Users\ufo001\AppData\Local\Temp\profile_map.json"


def req(method, path, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return resp.status, (json.loads(body) if body.strip() else None)


JS = """() => {
  // Telegram Web держит АКТИВНЫЙ аккаунт под ключом user_auth, а все
  // остальные — под account1, account2, account3… И слова user_auth в
  // их названии нет. Прежнее чтение искало только user_auth и потому
  // видело в каждом профиле ровно один аккаунт, хотя их бывает три.
  //
  // Сколько всего — написано в number_of_accounts. Идентификатор внутри
  // accountN лежит в поле userId (у user_auth оно называется id).
  const out = [];
  const n = parseInt(localStorage.getItem('number_of_accounts') || '0', 10);
  for (let i = 1; i <= Math.max(n, 1); i++) {
    const raw = localStorage.getItem('account' + i);
    if (raw) out.push(['account' + i, raw]);
  }
  const ua = localStorage.getItem('user_auth');
  if (ua) out.push(['user_auth', ua]);
  return out;
}"""


async def read_profile(pid, name, timeout_sec=70):
    from playwright.async_api import async_playwright
    try:
        req("POST", "/api/profiles/%s/launch" % pid, {}, timeout=90)
    except Exception as e:
        # 409 значит «уже запущен» — это не ошибка, работаем дальше.
        if "409" not in str(e):
            return {"profile": name, "error": "не запустился: %s" % e}
    try:
        # cdp_url приходит относительным путём — playwright такой не берёт,
        # достраиваем до полного адреса.
        cdp = None
        for _ in range(20):
            try:
                _, st = req("GET", "/api/profiles/%s/status" % pid)
                cdp = (st or {}).get("cdp_url")
            except Exception:
                cdp = None
            if cdp:
                break
            await asyncio.sleep(2)
        if not cdp:
            return {"profile": name, "error": "нет cdp_url"}
        if cdp.startswith("/"):
            cdp = API + cdp

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp, timeout=30000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            ids = []
            try:
                # Прокси бывают медленными: даём вторую попытку, прежде чем
                # записывать профиль в неудачные.
                for attempt in (1, 2):
                    try:
                        await page.goto("https://web.telegram.org/k/",
                                        wait_until="domcontentloaded",
                                        timeout=timeout_sec * 1000)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        await page.wait_for_timeout(3000)
                await page.wait_for_timeout(4000)
                for key, raw in (await page.evaluate(JS)) or []:
                    try:
                        v = json.loads(raw)
                        uid = v.get("id") or v.get("userId") or v.get("user_id")
                    except Exception:
                        uid = raw
                    if uid:
                        ids.append({"key": key, "id": str(uid)})
            except Exception as e:
                return {"profile": name,
                        "error": "страница не открылась: %s"
                                 % str(e).splitlines()[0][:90]}
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
            return {"profile": name, "accounts": ids}
    finally:
        try:
            req("POST", "/api/profiles/%s/stop" % pid, {}, timeout=60)
        except Exception:
            pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--only", default="", help="имена профилей через запятую")
    a = ap.parse_args()

    import asyncpg
    _, profiles = req("GET", "/api/profiles")
    want = set(x.strip() for x in a.only.split(",") if x.strip())
    if want:
        profiles = [p for p in profiles
                    if any(p["name"].startswith(w) for w in want)]
    profiles = profiles[:a.limit]

    c = await asyncpg.connect("postgresql://gramgpt:gramgpt@localhost:5432/gramgpt")
    by_tg = {}
    for r in await c.fetch("SELECT id, phone, tg_id, status::text s FROM accounts WHERE tg_id IS NOT NULL"):
        by_tg[str(r["tg_id"])] = (r["id"], r["phone"], r["s"])
    await c.close()

    results = []
    for p in profiles:
        r = await read_profile(p["id"], p["name"])
        short = (p["name"] or "").replace(p["proxy"] or "", "").rstrip("/").strip()
        print("── %s  (%s)" % (short or p["name"][:20], (p.get("proxy") or "").split(":")[0]))
        if r.get("error"):
            print("   %s" % r["error"])
        for acc in r.get("accounts", []):
            m = by_tg.get(acc["id"])
            if m:
                print("   %-22s tg_id=%-13s → acc#%-5d %-16s %s"
                      % (acc["key"], acc["id"], m[0], m[1], m[2]))
            else:
                print("   %-22s tg_id=%-13s → в базе нет" % (acc["key"], acc["id"]))
        if not r.get("accounts") and not r.get("error"):
            print("   залогиненных аккаунтов не найдено")
        r["proxy"] = p.get("proxy")
        r["id"] = p["id"]
        results.append(r)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("\nсохранено: %s" % OUT)


asyncio.run(main())
