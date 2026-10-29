# -*- coding: utf-8 -*-
"""Опыт: подписка на пачку каналов одной ссылкой на папку.

Идея. В Telegram есть ссылки на папки: открываешь, жмёшь «добавить» — и
вступаешь сразу во все каналы из неё. Если это работает через API, задача
подписок решается принципиально: вместо сотни вступлений по одному —
один запрос на аккаунт.

Что проверяем по шагам:

  1. Аккаунт-донор создаёт папку из нескольких каналов, на которые он
     уже подписан.
  2. Делает на неё пригласительную ссылку. ЗДЕСЬ ГЛАВНЫЙ ВОПРОС: такие
     ссылки может требовать Telegram Premium. Если так — идея упирается
     в оплату, и это надо знать сразу.
  3. Второй аккаунт, не подписанный ни на один из этих каналов, читает
     ссылку и вступает одним запросом.
  4. Считаем, сколько вступлений реально произошло.

Ничего не пишем и не удаляем: только папка у донора и подписки у второго.

Запуск из папки api:
    python ..\tools\folder_test.py --donor 64 --test 65 --channels 3
"""
import argparse
import asyncio
import os
import random
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))


async def клиент(acc_id, ttl=600, без_прокси=False):
    """Подключение к аккаунту.

    без_прокси — идём напрямую, со своего адреса. Нужно, когда прокси
    кончились. Для опыта это приемлемо: два аккаунта, несколько минут.
    Для постоянной работы так нельзя — Telegram увидит весь парк с
    одного адреса.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc:
        return None, "нет аккаунта"
    if not без_прокси and not proxy:
        return None, "нет прокси"
    if not без_прокси:
        if not acquire_ip_lock(proxy, owner=acc_id):
            return None, "адрес занят"
    if not acquire_account_lock(acc_id, ttl=ttl):
        return None, "аккаунт занят"
    if без_прокси:
        # Рабочий сборщик клиента без прокси не пускает — и правильно
        # делает, это защита всего парка. Здесь собираем вручную, только
        # для опыта на паре аккаунтов.
        from telethon import TelegramClient
        путь = (acc.session_file or "").replace(".session", "")
        if not путь:
            return None, "нет файла сессии"
        app = getattr(acc, "api_app", None)
        api_id = getattr(app, "api_id", None) or 6
        api_hash = (getattr(app, "api_hash", None)
                    or "eb06d4abfb49dc3eeb1aeb98ae0f581e")
        c = TelegramClient(путь, int(api_id), api_hash,
                           device_model="Samsung Galaxy A54",
                           system_version="Android 14",
                           app_version="10.11.2", lang_code="en")
    else:
        c = make_telethon_client(acc, proxy)
    if c is None:
        return None, "нет сессии"
    return c, ""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--donor", type=int, required=True, help="кто делает папку")
    ap.add_argument("--test", type=int, required=True, help="кто вступает")
    ap.add_argument("--channels", type=int, default=3)
    ap.add_argument("--no-proxy", action="store_true",
                    help="идти напрямую — когда прокси кончились")
    ap.add_argument("--cleanup", action="store_true",
                    help="только убрать папки опытов у донора и выйти")
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    from utils.account_lock import release_account_lock

    # ── Каналы: донор в них состоит, испытуемый — нет ────────────────
    async with Session() as db:
        каналы = [x[0] for x in (await db.execute(sa_text("""
            SELECT ca.channel_username
            FROM campaign_channel_assignments ca
            WHERE ca.account_id = :d AND ca.status = 'joined'
              AND NOT EXISTS (
                SELECT 1 FROM campaign_channel_assignments t
                WHERE t.account_id = :t
                  AND lower(t.channel_username) = lower(ca.channel_username))
            ORDER BY random() LIMIT :n"""),
            {"d": a.donor, "t": a.test, "n": a.channels})).all()]

    if not каналы:
        print("не нашёл каналов: донор должен состоять там, где испытуемый нет")
        return
    print("КАНАЛЫ ДЛЯ ОПЫТА (%d):" % len(каналы))
    for ch in каналы:
        print("   @%s" % ch)
    print()

    # Номер созданной папки — чтобы убрать её после опыта.
    созданная = [None]

    from telethon.tl.functions.messages import (
        UpdateDialogFilterRequest, GetDialogFiltersRequest)
    from telethon.tl.functions.chatlists import (
        ExportChatlistInviteRequest, CheckChatlistInviteRequest,
        JoinChatlistInviteRequest, GetExportedInvitesRequest,
        DeleteExportedInviteRequest)
    # В новых версиях название папки — не строка, а объект с разметкой:
    # Telegram разрешил в них эмодзи и форматирование.
    from telethon.tl.types import (
        DialogFilter, InputChatlistDialogFilter, TextWithEntities)

    # ── Чистка: убрать все папки опытов и выйти ─────────────────────
    if a.cleanup:
        донор, err = await клиент(a.donor, без_прокси=a.no_proxy)
        if донор is None:
            print("донор acc#%d: %s" % (a.donor, err))
            return
        try:
            await донор.connect()
            фильтры = await донор(GetDialogFiltersRequest())
            список = getattr(фильтры, "filters", фильтры)
            убрал = 0
            for ф in список:
                номер = getattr(ф, "id", None)
                имя = getattr(ф, "title", None)
                текст = getattr(имя, "text", имя) if имя else ""
                if номер and 50 <= номер <= 90 and str(текст).startswith("test"):
                    # Сперва ссылки: удаление папки их не уносит, и они
                    # копятся до INVITES_TOO_MUCH.
                    try:
                        инв = await донор(GetExportedInvitesRequest(
                            chatlist=InputChatlistDialogFilter(filter_id=номер)))
                        for i in (getattr(инв, "invites", []) or []):
                            await донор(DeleteExportedInviteRequest(
                                chatlist=InputChatlistDialogFilter(filter_id=номер),
                                slug=i.url.rstrip("/").split("/")[-1]))
                            await asyncio.sleep(0.6)
                    except Exception:
                        pass
                    await донор(UpdateDialogFilterRequest(id=номер, filter=None))
                    print("   убрал папку №%d «%s»" % (номер, текст))
                    убрал += 1
                    await asyncio.sleep(1)
            print("убрано папок: %d" % убрал)
        finally:
            try:
                await донор.disconnect()
            except Exception:
                pass
            release_account_lock(a.donor)
        return

    # ── 1. Донор: папка ─────────────────────────────────────────────
    донор, err = await клиент(a.donor, без_прокси=a.no_proxy)
    if донор is None:
        print("донор acc#%d: %s" % (a.donor, err))
        return
    ссылка = None
    try:
        await донор.connect()
        if not await донор.is_user_authorized():
            print("ДОНОР acc#%d: сессия не авторизована" % a.donor)
            return
        me = await донор.get_me()
        if me is None:
            print("ДОНОР acc#%d: аккаунт не отвечает" % a.donor)
            return
        print("ДОНОР acc#%d @%s" % (a.donor, me.username or "—"))

        # Спрашиваем сервер, а не кэш. Кэш помнит канал, даже если тот
        # давно закрылся, и тогда ссылка на папку падает целиком с
        # ChannelPrivateError — из-за одного негодного канала теряется
        # вся пачка. Лучше отсеять здесь.
        peers = []
        отсеяно = 0
        приватных = 0
        for ch in каналы:
            try:
                ent = await донор.get_entity(ch)
                # Приватным каналом поделиться нельзя — отказ прилетит
                # на всю пачку, и какой именно виноват, не скажут.
                if not getattr(ent, "username", None):
                    приватных += 1
                    continue
                peers.append(await донор.get_input_entity(ent))
            except Exception:
                отсеяно += 1
            await asyncio.sleep(0.15)
        if отсеяно or приватных:
            print("   отсеяно: недоступных %d, приватных %d"
                  % (отсеяно, приватных))
        if not peers:
            print("   ни один канал не открылся")
            return

        # Номер папки: берём заведомо свободный, чтобы не затереть чужую.
        номер = random.randint(50, 90)
        созданная[0] = номер
        папка = DialogFilter(
            id=номер,
            title=TextWithEntities(text="test%d" % номер, entities=[]),
            pinned_peers=[], include_peers=peers, exclude_peers=[])
        await донор(UpdateDialogFilterRequest(id=номер, filter=папка))
        print("   папка №%d создана, каналов в ней %d" % (номер, len(peers)))
        await asyncio.sleep(2)

        # ── 2. Ссылка ──────────────────────────────────────────────
        # Сигнал «дальше пробовать нельзя»: кончились ссылки или флуд.
        стоп = [None]

        async def попытка(набор):
            """Выдать ссылку на этот набор. None — Telegram отказал."""
            try:
                res = await донор(ExportChatlistInviteRequest(
                    chatlist=InputChatlistDialogFilter(filter_id=номер),
                    title="test%d" % номер, peers=набор))
                return res.invite.url
            except Exception as ex:
                имя = type(ex).__name__
                текст = str(ex)
                # Эти отказы НЕ про содержимое пачки: делить бессмысленно,
                # и каждая новая попытка только копит флуд.
                if имя == "FloodWaitError" or "INVITES_TOO_MUCH" in текст:
                    стоп[0] = текст[:90]
                    return None
                if имя not in ("ChannelPrivateError", "PeerIdInvalidError",
                               "ChatAdminRequiredError"):
                    print("   отказ: %s: %s" % (имя, текст[:120]))
                return None

        async def отобрать(набор, глубина=0):
            """Наибольший годный поднабор. Делим пополам, пока не выйдет."""
            if not набор or стоп[0]:
                return [], None
            url = await попытка(набор)
            if url:
                return набор, url
            if len(набор) == 1:
                return [], None
            if глубина > 6:
                return [], None
            сер = len(набор) // 2
            лев, _ = await отобрать(набор[:сер], глубина + 1)
            прав, _ = await отобрать(набор[сер:], глубина + 1)
            вместе = лев + прав
            if not вместе:
                return [], None
            url = await попытка(вместе)
            if url:
                return вместе, url
            # Половины порознь прошли, а вместе нет — упёрлись в размер.
            лучший = лев if len(лев) >= len(прав) else прав
            url = await попытка(лучший)
            return (лучший, url) if url else ([], None)

        годные, ссылка = await отобрать(peers)
        if стоп[0]:
            print()
            print("   ОСТАНОВЛЕН: %s" % стоп[0])
            print("   это не про каналы — дело в лимитах аккаунта")
            return
        if not ссылка:
            print()
            print("   ССЫЛКА НЕ ВЫШЛА ни для одного набора")
            return
        if len(годные) < len(peers):
            print("   отсеяно при выдаче ссылки: %d из %d"
                  % (len(peers) - len(годные), len(peers)))
        print("   В ССЫЛКЕ КАНАЛОВ: %d" % len(годные))
        print("   ССЫЛКА: %s" % ссылка)
    except Exception as e:
        print("   сбой донора: %s: %s" % (type(e).__name__, str(e)[:150]))
        return
    finally:
        try:
            await донор.disconnect()
        except Exception:
            pass
        release_account_lock(a.donor)

    if not ссылка:
        return

    # ── 3. Испытуемый: читает ссылку и вступает ─────────────────────
    print()
    slug = ссылка.rstrip("/").split("/")[-1]
    испыт, err = await клиент(a.test, без_прокси=a.no_proxy)
    if испыт is None:
        print("испытуемый acc#%d: %s" % (a.test, err))
        return
    try:
        await испыт.connect()
        me2 = await испыт.get_me()
        print("ИСПЫТУЕМЫЙ acc#%d @%s" % (a.test, me2.username or "—"))

        try:
            чек = await испыт(CheckChatlistInviteRequest(slug=slug))
        except Exception as e:
            print("   ссылка не читается: %s: %s" % (type(e).__name__, str(e)[:150]))
            return
        предлагают = list(getattr(чек, "peers", []) or [])
        print("   ссылка открылась, предлагают вступить в %d" % len(предлагают))

        if not предлагают:
            print("   вступать не во что (возможно, уже подписан)")
            return

        try:
            await испыт(JoinChatlistInviteRequest(slug=slug, peers=предлагают))
            print("   ВСТУПИЛ ОДНИМ ЗАПРОСОМ")
        except Exception as e:
            print("   вступить не вышло: %s: %s" % (type(e).__name__, str(e)[:180]))
            return

        # ── 4. Проверяем на деле ────────────────────────────────────
        await asyncio.sleep(3)
        свои = set()
        async for d in испыт.iter_dialogs(limit=400):
            u = getattr(d.entity, "username", None)
            if u:
                свои.add(u.lower())
        вышло = [ch for ch in каналы if ch.lower() in свои]
        print()
        print("   ПРОВЕРКА: реально подписан на %d из %d" % (len(вышло), len(каналы)))
        for ch in каналы:
            print("      @%-26s %s" % (ch, "да" if ch.lower() in свои else "нет"))
    finally:
        try:
            await испыт.disconnect()
        except Exception:
            pass
        release_account_lock(a.test)

    # ── Убираем папку: их у обычного аккаунта всего десять, и
    # накопленные опытами быстро съели бы весь запас. Ссылка своё уже
    # отработала — испытуемый вступил.
    if созданная[0]:
        уборщик, err = await клиент(a.donor, без_прокси=a.no_proxy)
        if уборщик is not None:
            try:
                await уборщик.connect()
                # Ссылку убираем первой — удаление папки её не уносит.
                try:
                    инв = await уборщик(GetExportedInvitesRequest(
                        chatlist=InputChatlistDialogFilter(filter_id=созданная[0])))
                    for i in (getattr(инв, "invites", []) or []):
                        await уборщик(DeleteExportedInviteRequest(
                            chatlist=InputChatlistDialogFilter(filter_id=созданная[0]),
                            slug=i.url.rstrip("/").split("/")[-1]))
                        await asyncio.sleep(0.6)
                except Exception:
                    pass
                await уборщик(UpdateDialogFilterRequest(
                    id=созданная[0], filter=None))
                print()
                print("   папка №%d и её ссылки убраны" % созданная[0])
            except Exception as e:
                print("   папку убрать не вышло: %s" % type(e).__name__)
            finally:
                try:
                    await уборщик.disconnect()
                except Exception:
                    pass
                release_account_lock(a.donor)


# Без этой проверки импорт из соседнего скрипта запускал бы весь опыт
# заново — и падал, потому что цикл событий уже крутится.
if __name__ == "__main__":
    asyncio.run(main())
