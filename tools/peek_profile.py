# -*- coding: utf-8 -*-
"""Заглянуть в профиль антидетекта и показать ВСЁ хранилище Telegram Web.

Обход профилей нашёл в каждом ровно по одному аккаунту, хотя в некоторых
их точно три. Значит либо мы читаем слишком рано — веб-клиент раскладывает
дополнительные аккаунты не сразу, — либо они лежат не в localStorage, а в
IndexedDB, куда прежний скрипт не заглядывал.

Поэтому здесь: ждём дольше, показываем все ключи localStorage целиком и
перечисляем базы IndexedDB с их хранилищами. Ничего не нажимаем и не
меняем — только смотрим.
"""
import asyncio
import json
import os
import sys
import argparse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API = "http://localhost:8080"


def req(method, path, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read().decode()
        try:
            return resp.status, json.loads(body)
        except Exception:
            return resp.status, body


JS_LS = """() => {
  const out = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    const v = localStorage.getItem(k) || '';
    out.push([k, v.length > 400 ? v.slice(0, 400) + '…' : v]);
  }
  return out;
}"""

JS_IDB = """async () => {
  if (!indexedDB.databases) return [['нет API databases()', '']];
  const dbs = await indexedDB.databases();
  const out = [];
  for (const d of dbs) {
    try {
      const db = await new Promise((res, rej) => {
        const rq = indexedDB.open(d.name);
        rq.onsuccess = () => res(rq.result);
        rq.onerror = () => rej(rq.error);
      });
      out.push([d.name, Array.from(db.objectStoreNames).join(', ')]);
      db.close();
    } catch (e) { out.push([d.name, 'не открылась: ' + e]); }
  }
  return out;
}"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="начало имени, напр. tg40")
    ap.add_argument("--wait", type=int, default=25, help="сколько ждать загрузки, сек")
    a = ap.parse_args()

    from playwright.async_api import async_playwright

    _, profiles = req("GET", "/api/profiles")
    цель = next((p for p in profiles if p["name"].startswith(a.profile)), None)
    if not цель:
        print("профиль %s не найден" % a.profile)
        return
    print("профиль: %s" % цель["name"])
    print("прокси:  %s" % цель.get("proxy"))
    print()

    try:
        req("POST", "/api/profiles/%s/launch" % цель["id"], {}, timeout=90)
    except Exception as e:
        if "409" not in str(e):
            print("не запустился: %s" % e)
            return

    cdp = None
    for _ in range(25):
        try:
            _, st = req("GET", "/api/profiles/%s/status" % цель["id"])
            cdp = (st or {}).get("cdp_url")
        except Exception:
            cdp = None
        if cdp:
            break
        await asyncio.sleep(2)
    if not cdp:
        print("нет cdp_url")
        return
    if cdp.startswith("/"):
        cdp = API + cdp

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        try:
            await page.goto("https://web.telegram.org/k/",
                            wait_until="domcontentloaded", timeout=90000)
            print("страница открыта, жду %d секунд полной загрузки…" % a.wait,
                  flush=True)
            await page.wait_for_timeout(a.wait * 1000)

            print()
            print("=== localStorage ===")
            for k, v in (await page.evaluate(JS_LS)) or []:
                метка = "  ← АККАУНТ" if "user_auth" in k else ""
                print("  %-34s %s%s" % (k, v[:90], метка))

            print()
            print("=== IndexedDB ===")
            for name, stores in (await page.evaluate(JS_IDB)) or []:
                print("  %-28s %s" % (name, stores))
        except Exception as e:
            print("сбой: %s: %s" % (type(e).__name__, str(e)[:160]))
        finally:
            try:
                await page.close()
            except Exception:
                pass


asyncio.run(main())
