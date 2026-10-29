"""
GramGPT — tasks/react_subscribe.py
Аккаунты без бана — в каналы «только реакции», через ссылку на папку.

Зачем. Канал уходит в режим «только реакции», когда нашим аккаунтам там
запретили писать. Бан выдаётся на пару аккаунт-чат: забаненный не ставит
даже реакцию, а сосед без бана — ставит спокойно. Обход реакциями ходит
только туда, где аккаунт уже состоит, поэтому в каждом таком канале
должен сидеть хотя бы один-два аккаунта без бана.

Как. Не поштучными вступлениями, а папками — проверено 22.09: один
запрос заводит аккаунт сразу в пятьдесят каналов, Premium не нужен.

  1. Донор — аккаунт, который уже состоит в пачке react-каналов, —
     делает из них папку и ссылку на неё.
  2. Каждый получатель открывает ссылку и вступает ОДНИМ запросом, но
     только в свои каналы: Telegram даёт выбрать поднабор, и мы берём
     лишь те, где этого аккаунта не банили и где его ещё нет.
  3. Получатель сразу убирает папку у себя, оставаясь в каналах: без
     этого упрёмся в предел папок-по-ссылке у обычного аккаунта.
  4. Донор через несколько часов убирает ссылку и папку.

В ссылку идут только публичные каналы: приватный рушит выдачу ссылки
на всю пачку разом.

Защищённые аккаунты (RT_PROTECTED_ACCOUNTS) не трогаем никогда — ни
донорами, ни получателями.
"""

import asyncio
import logging
import os
import random
import sys
from collections import defaultdict

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

CAMPAIGN = int(os.getenv("RT_REACT_CAMPAIGN", "143"))
# Сколько аккаунтов без бана нужно в каждом react-канале.
REACTORS = int(os.getenv("RT_REACT_REACTORS", "2"))
# Каналов в одной папке. Проверено: 50 заходят одним запросом, 80 —
# выдаётся ссылка. Берём проверенное.
FOLDER_SIZE = int(os.getenv("RT_REACT_FOLDER_SIZE", "50"))
# Сколько папок за ночь.
FOLDERS_PER_NIGHT = int(os.getenv("RT_REACT_FOLDERS", "40"))
# У Telegram предел 500 каналов на аккаунт; оставляем запас.
MAX_CHANNELS = int(os.getenv("RT_REACT_MAX_CHANNELS", "450"))
# Через сколько донор убирает ссылку: получатели к этому времени прошли.
CLEANUP_AFTER = int(os.getenv("RT_REACT_CLEANUP_SEC", str(4 * 3600)))
# Сколько раз ждать занятый адрес или аккаунт.
RETRIES = 6
# Жека (@jeka_opasniy и его номер) и Мария — не трогать никогда.
PROTECTED = {int(x) for x in os.getenv(
    "RT_PROTECTED_ACCOUNTS", "48,95,217").split(",") if x.strip()}

# Бан в чате — по истории пары: «Channel private» (админ выгнал) или
# «can't write» (запретили писать), и после него ни одного удачного
# комментария. Статусу 'joined' одному верить нельзя: вступить в КАНАЛ
# можно и с баном в ЧАТЕ.
_БАН = """(q.status = 'failed'
           AND (q.error ILIKE 'Channel private%%' OR q.error ILIKE '%%can''t write%%')
           AND NOT EXISTS (
               SELECT 1 FROM comment_queue d
               WHERE d.account_id = q.account_id
                 AND lower(d.channel) = lower(q.channel)
                 AND d.status = 'done' AND d.executed_at > q.executed_at))"""


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# План
# ═══════════════════════════════════════════════════════════

async def build_plan(db) -> dict:
    """Какие папки сделать, кто донор и кто куда вступает.

    Ничего не отправляет. Возвращает {"folders": [...], "stats": {...}},
    папка — {"donor": id, "channels": [...], "targets": {acc: [...]}}.
    """
    from sqlalchemy import text as t

    ids = (await db.execute(t(
        "SELECT account_ids FROM campaigns WHERE id = :c"), {"c": CAMPAIGN})).scalar() or []
    ids = [int(x) for x in ids if int(x) not in PROTECTED]

    # Живые аккаунты с прокси и сколько каналов им ещё можно.
    свободно = {}
    for r in (await db.execute(t("""
        SELECT a.id,
               (SELECT count(*) FROM campaign_channel_assignments ca
                WHERE ca.account_id = a.id AND ca.status = 'joined')
        FROM accounts a
        WHERE a.id = ANY(:ids) AND a.status = 'active' AND a.proxy_id IS NOT NULL"""),
            {"ids": ids})).all():
        свободно[r[0]] = max(0, MAX_CHANNELS - int(r[1] or 0))

    # React-каналы, крупные первыми.
    каналы = [r[0].lower() for r in (await db.execute(t("""
        SELECT tc.username FROM target_channels tc
        LEFT JOIN parsed_channels pc ON lower(pc.username) = lower(tc.username)
        WHERE tc.campaign_id = :c AND tc.mode = 'react' AND tc.is_active
        ORDER BY pc.subscribers DESC NULLS LAST"""), {"c": CAMPAIGN})).all()]
    if not каналы:
        return {"folders": [], "stats": {"react_channels": 0}}

    # Кто где состоит и кого где банили — одним заходом.
    состоит = defaultdict(set)          # канал → аккаунты (без бана тоже)
    в_канале = defaultdict(set)         # канал → кто в самом канале
    for r in (await db.execute(t("""
        SELECT lower(channel_username), account_id, status
        FROM campaign_channel_assignments
        WHERE status IN ('joined', 'failed') AND lower(channel_username) = ANY(:chs)"""),
            {"chs": каналы})).all():
        # Бан в чате не выкидывает из канала: донору этого хватает, чтобы
        # поделиться каналом. Сам донор проверит членство перед ссылкой.
        в_канале[r[0]].add(r[1])
        if r[2] == "joined":
            состоит[r[0]].add(r[1])

    # Донором может быть любой живой аккаунт с прокси, не только
    # аккаунты кампании: ему не надо ничего писать и ставить.
    доноры = {r[0] for r in (await db.execute(t("""
        SELECT id FROM accounts
        WHERE status = 'active' AND proxy_id IS NOT NULL AND NOT (id = ANY(:p))"""),
        {"p": list(PROTECTED)})).all()}
    бан = defaultdict(set)              # канал → аккаунты
    for r in (await db.execute(t("""
        SELECT DISTINCT lower(q.channel), q.account_id
        FROM comment_queue q
        WHERE lower(q.channel) = ANY(:chs) AND """ + _БАН),
            {"chs": каналы})).all():
        бан[r[0]].add(r[1])
    неудачно = defaultdict(set)         # сняты раньше — туда не ведём
    for r in (await db.execute(t("""
        SELECT lower(channel_username), account_id
        FROM campaign_channel_assignments
        WHERE status = 'failed' AND lower(channel_username) = ANY(:chs)"""),
            {"chs": каналы})).all():
        неудачно[r[0]].add(r[1])
    живые = set(свободно)

    нужно = {}
    for ch in каналы:
        чистых = len((состоит[ch] - бан[ch]) & живые)
        if чистых < REACTORS:
            нужно[ch] = REACTORS - чистых

    # Папки: берём донора, который состоит в наибольшем числе нуждающихся
    # каналов, режем до FOLDER_SIZE. Донору бан в чате не мешает — ему
    # нужно лишь состоять в канале, чтобы им поделиться.
    доноры_занятые = set()
    папки = []
    осталось = dict(нужно)
    получил = defaultdict(int)          # сколько вступлений аккаунту за ночь
    while осталось and len(папки) < FOLDERS_PER_NIGHT:
        счёт = defaultdict(list)
        for ch in осталось:
            for a in в_канале[ch] & доноры:
                if a not in доноры_занятые:
                    счёт[a].append(ch)
        if not счёт:
            break
        донор = max(счёт, key=lambda a: len(счёт[a]))
        пачка = счёт[донор][:FOLDER_SIZE]
        доноры_занятые.add(донор)

        # Получатели: жадно — тот, кто закрывает больше всего каналов
        # пачки. Один аккаунт — одна папка за ночь.
        цели = {}
        потребность = {ch: осталось[ch] for ch in пачка}
        while any(потребность.values()):
            лучший, его = None, []
            for a in живые:
                if a == донор or a in цели or получил[a]:
                    continue
                годные = [ch for ch, n in потребность.items() if n > 0
                          and a not in состоит[ch] and a not in бан[ch]
                          and a not in неудачно[ch]]
                годные = годные[:свободно[a]]
                if len(годные) > len(его):
                    лучший, его = a, годные
            if not лучший:
                break
            цели[лучший] = его
            получил[лучший] += 1
            for ch in его:
                потребность[ch] -= 1
        for ch in пачка:
            if потребность[ch] > 0:
                осталось[ch] = потребность[ch]
            else:
                осталось.pop(ch, None)
        if цели:
            папки.append({"donor": донор, "channels": пачка, "targets": цели})
        else:
            # Этим каналам некого дать — дальше их не крутим.
            for ch in пачка:
                осталось.pop(ch, None)

    return {
        "folders": папки,
        "stats": {
            "react_channels": len(каналы),
            "need_channels": len(нужно),
            "need_slots": sum(нужно.values()),
            "folders": len(папки),
            "joins": sum(len(v) for f in папки for v in f["targets"].values()),
            "targets": sum(len(f["targets"]) for f in папки),
            "left_channels": len(осталось),
            "pool": len(живые),
        },
    }


# ═══════════════════════════════════════════════════════════
# Подключение
# ═══════════════════════════════════════════════════════════

async def _connect(acc_id: int, ttl: int = 600):
    """Клиент аккаунта через его прокси, с замками адреса и аккаунта.

    Без прокси не подключаемся никогда: весь парк с одного адреса —
    верный способ потерять его целиком.
    Возвращает (client, причина_отказа).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock

    if acc_id in PROTECTED:
        return None, "защищённый аккаунт"
    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy).where(
            Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc and acc.proxy_id else None
    if not acc or acc.status != "active":
        return None, "аккаунт не активен"
    if not proxy:
        return None, "нет прокси"
    if not acquire_ip_lock(proxy, owner=acc_id):
        return None, "занят"
    if not acquire_account_lock(acc_id, ttl=ttl):
        return None, "занят"
    c = make_telethon_client(acc, proxy)
    if c is None:
        from utils.account_lock import release_account_lock
        release_account_lock(acc_id)
        return None, "нет сессии"
    return c, ""


async def _close(client, acc_id: int):
    from utils.account_lock import release_account_lock
    try:
        await client.disconnect()
    except Exception:
        pass
    release_account_lock(acc_id)


# ═══════════════════════════════════════════════════════════
# Донор: папка и ссылка
# ═══════════════════════════════════════════════════════════

async def _make_folder(folder: dict) -> dict:
    from sqlalchemy import text as t
    from utils.db_pool import async_session as Session
    from telethon.tl.functions.messages import UpdateDialogFilterRequest
    from telethon.tl.functions.chatlists import ExportChatlistInviteRequest
    from telethon.tl.types import (
        DialogFilter, InputChatlistDialogFilter, TextWithEntities)

    донор = folder["donor"]
    client, err = await _connect(донор)
    if client is None:
        return {"retry": err in ("занят",), "error": err}

    мёртвые = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"error": "донор не авторизован"}

        # Спрашиваем сервер, а не кэш: закрывшийся канал в пачке роняет
        # выдачу ссылки целиком. Без адреса канал поделить нельзя.
        peers, имена = [], []
        for ch in folder["channels"]:
            try:
                ent = await client.get_entity(ch)
                if not getattr(ent, "username", None):
                    мёртвые.append(ch)
                    continue
                # Донор вышел из канала — поделиться им не сможет, а один
                # такой канал роняет выдачу ссылки на всю пачку.
                if getattr(ent, "left", False):
                    continue
                peers.append(await client.get_input_entity(ent))
                имена.append(ch)
            except Exception as e:
                if type(e).__name__ in ("UsernameInvalidError", "UsernameNotOccupiedError") \
                        or "No user has" in str(e):
                    мёртвые.append(ch)
            await asyncio.sleep(random.uniform(0.3, 0.8))
        if not peers:
            return {"error": "ни один канал не открылся", "dead": мёртвые}

        номер = random.randint(50, 90)
        название = random.choice(["Новости", "Каналы", "Читаю", "Разное", "Лента"])
        await client(UpdateDialogFilterRequest(id=номер, filter=DialogFilter(
            id=номер, title=TextWithEntities(text=название, entities=[]),
            pinned_peers=[], include_peers=peers, exclude_peers=[])))
        await asyncio.sleep(random.uniform(2, 4))

        # Ссылка. Если пачку отвергли за один негодный канал — делим
        # пополам, но не больше трёх раз: каждая попытка копит флуд, и
        # мы уже раз сами себе заработали ожидание на четыре минуты.
        набор, имена_в = list(peers), list(имена)
        url = None
        for _ in range(4):
            try:
                res = await client(ExportChatlistInviteRequest(
                    chatlist=InputChatlistDialogFilter(filter_id=номер),
                    title=название, peers=набор))
                url = res.invite.url
                break
            except Exception as e:
                имя, текст = type(e).__name__, str(e)
                if имя == "FloodWaitError" or "INVITES_TOO_MUCH" in текст:
                    logger.warning("[react_sub] донор acc#%s: %s", донор, текст[:80])
                    break
                половина = max(1, len(набор) // 2)
                набор, имена_в = набор[:половина], имена_в[:половина]
                await asyncio.sleep(random.uniform(4, 8))
        if not url:
            try:
                await client(UpdateDialogFilterRequest(id=номер, filter=None))
            except Exception:
                pass
            return {"error": "ссылка не выдана", "dead": мёртвые}

        logger.info("[react_sub] донор acc#%s: папка №%d, каналов %d, %s",
                    донор, номер, len(имена_в), url)
        return {"url": url, "filter_id": номер, "channels": имена_в, "dead": мёртвые}
    except Exception as e:
        logger.warning("[react_sub] донор acc#%s сбой: %s: %s",
                       донор, type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__, "dead": мёртвые}
    finally:
        await _close(client, донор)
        if мёртвые:
            try:
                async with Session() as db:
                    await db.execute(t("""
                        UPDATE target_channels SET mode = 'dead'
                        WHERE lower(username) = ANY(:chs) AND mode = 'react'"""),
                        {"chs": [c.lower() for c in мёртвые]})
                    await db.commit()
                logger.warning("[react_sub] закрыты или исчезли, сняты: %s",
                               " ".join("@" + c for c in мёртвые))
            except Exception:
                logger.debug("[react_sub] не удалось снять мёртвые", exc_info=True)


# ═══════════════════════════════════════════════════════════
# Получатель: вступить в свои каналы и убрать папку
# ═══════════════════════════════════════════════════════════

async def _join_folder(acc_id: int, url: str, свои: list) -> dict:
    from sqlalchemy import text as t
    from utils.db_pool import async_session as Session
    from tasks.subscribe_tasks import _отметить_вступление
    from telethon.tl.functions.chatlists import (
        CheckChatlistInviteRequest, JoinChatlistInviteRequest, LeaveChatlistRequest)
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    from telethon.tl.types import (
        InputPeerChannel, InputChatlistDialogFilter, DialogFilterChatlist)
    from telethon.tl.types.chatlists import ChatlistInviteAlready

    client, err = await _connect(acc_id)
    if client is None:
        return {"retry": err in ("занят",), "error": err}

    slug = url.rstrip("/").split("/")[-1]
    нужные = {c.lower() for c in свои}

    # Перед вступлением — ещё раз свежая проверка бана: с момента плана
    # аккаунт мог словить его в одном из каналов.
    async with Session() as db:
        забанен = {r[0] for r in (await db.execute(t("""
            SELECT DISTINCT lower(q.channel) FROM comment_queue q
            WHERE q.account_id = :a AND lower(q.channel) = ANY(:chs) AND """ + _БАН),
            {"a": acc_id, "chs": list(нужные)})).all()}
    нужные -= забанен

    вступил = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"error": "не авторизован"}
        до = set()
        try:
            до = {getattr(f, "id", None) for f in
                  getattr(await client(GetDialogFiltersRequest()), "filters", [])}
        except Exception:
            pass

        чек = await client(CheckChatlistInviteRequest(slug=slug))
        if isinstance(чек, ChatlistInviteAlready):
            return {"error": "папка уже добавлена"}

        # Из всех каналов папки берём только свои: Telegram разрешает
        # вступить в любой поднабор.
        peers = []
        for chat in (getattr(чек, "chats", []) or []):
            u = (getattr(chat, "username", None) or "").lower()
            if u and u in нужные and getattr(chat, "access_hash", None) is not None:
                peers.append((u, InputPeerChannel(chat.id, chat.access_hash)))
        if not peers:
            return {"error": "своих каналов в папке нет"}

        await asyncio.sleep(random.uniform(3, 9))      # «посмотрел список»
        ответ = await client(JoinChatlistInviteRequest(
            slug=slug, peers=[p for _, p in peers]))

        # Кто реально вступил — по каналам в ответе; если ответ их не
        # перечислил, верим списку, в который просились.
        в_ответе = {(getattr(c, "username", None) or "").lower()
                    for c in (getattr(ответ, "chats", []) or [])}
        вступил = [u for u, _ in peers if not в_ответе or u in в_ответе]

        # Папку у себя убираем, а каналы оставляем: leaveChatlist с пустым
        # списком — это «удалить папку, не выходя из чатов». Иначе
        # упрёмся в предел папок-по-ссылке у обычного аккаунта.
        await asyncio.sleep(random.uniform(4, 10))
        try:
            фильтры = getattr(await client(GetDialogFiltersRequest()), "filters", [])
            for f in фильтры:
                if isinstance(f, DialogFilterChatlist) and f.id not in до:
                    await client(LeaveChatlistRequest(
                        chatlist=InputChatlistDialogFilter(filter_id=f.id), peers=[]))
        except Exception as e:
            logger.info("[react_sub] acc#%s папку не убрал: %s", acc_id, type(e).__name__)

        logger.info("[react_sub] acc#%s вступил в %d каналов одним запросом",
                    acc_id, len(вступил))
        return {"joined": len(вступил)}
    except Exception as e:
        имя = type(e).__name__
        logger.warning("[react_sub] acc#%s вступить не вышло: %s: %s",
                       acc_id, имя, str(e)[:120])
        return {"error": имя, "retry": имя == "FloodWaitError"}
    finally:
        await _close(client, acc_id)
        if вступил:
            async with Session() as db:
                for u in вступил:
                    await _отметить_вступление(db, acc_id, u)


async def _cleanup_folder(донор: int, filter_id: int) -> dict:
    from telethon.tl.functions.messages import UpdateDialogFilterRequest
    from telethon.tl.functions.chatlists import (
        GetExportedInvitesRequest, DeleteExportedInviteRequest)
    from telethon.tl.types import InputChatlistDialogFilter

    client, err = await _connect(донор)
    if client is None:
        return {"retry": err in ("занят",), "error": err}
    try:
        await client.connect()
        # Сначала ссылки: удаление папки их не уносит, и они копятся до
        # INVITES_TOO_MUCH.
        try:
            инв = await client(GetExportedInvitesRequest(
                chatlist=InputChatlistDialogFilter(filter_id=filter_id)))
            for i in (getattr(инв, "invites", []) or []):
                await client(DeleteExportedInviteRequest(
                    chatlist=InputChatlistDialogFilter(filter_id=filter_id),
                    slug=i.url.rstrip("/").split("/")[-1]))
                await asyncio.sleep(random.uniform(0.6, 1.5))
        except Exception:
            pass
        await client(UpdateDialogFilterRequest(id=filter_id, filter=None))
        return {"cleaned": filter_id}
    except Exception as e:
        return {"error": type(e).__name__}
    finally:
        await _close(client, донор)


# ═══════════════════════════════════════════════════════════
# Задачи Celery
# ═══════════════════════════════════════════════════════════

def _повторить(task, res: dict, попытка: int):
    """Адрес или аккаунт заняты — вернуться позже, а не терять работу."""
    if res.get("retry") and попытка < RETRIES:
        raise task.retry(countdown=random.randint(600, 1200))
    return res


@celery_app.task(bind=True, name="tasks.react_subscribe.make_folder", max_retries=RETRIES)
def make_folder(self, folder: dict):
    res = run_async(_make_folder(folder))
    if res.get("url"):
        пустили = set(res["channels"])
        # Получатели идут вразнобой, по одному на 3-8 минут: адреса у
        # аккаунтов общие, и пачка разом всё равно упрётся в замки.
        задержка = 0
        for acc, chs in folder["targets"].items():
            свои = [c for c in chs if c in пустили]
            if not свои:
                continue
            задержка += random.randint(180, 480)
            join_folder.apply_async(args=[int(acc), res["url"], свои],
                                    countdown=задержка)
        cleanup_folder.apply_async(args=[folder["donor"], res["filter_id"]],
                                   countdown=max(CLEANUP_AFTER, задержка + 3600))
        return {"url": res["url"], "channels": len(res["channels"]),
                "targets": len(folder["targets"]), "dead": len(res.get("dead", []))}
    return _повторить(self, res, self.request.retries)


@celery_app.task(bind=True, name="tasks.react_subscribe.join_folder", max_retries=RETRIES)
def join_folder(self, acc_id: int, url: str, channels: list):
    return _повторить(self, run_async(_join_folder(acc_id, url, channels)),
                      self.request.retries)


@celery_app.task(bind=True, name="tasks.react_subscribe.cleanup_folder", max_retries=RETRIES)
def cleanup_folder(self, donor: int, filter_id: int):
    return _повторить(self, run_async(_cleanup_folder(donor, filter_id)),
                      self.request.retries)


async def _plan_and_dispatch(dry_run: bool) -> dict:
    from utils.db_pool import async_session as Session
    async with Session() as db:
        план = await build_plan(db)
    st = план["stats"]
    logger.info("[react_sub] react-каналов %s, недобор в %s (мест %s); папок %s, "
                "вступлений %s на %s акк.; не покрыто %s%s",
                st.get("react_channels"), st.get("need_channels"), st.get("need_slots"),
                st.get("folders"), st.get("joins"), st.get("targets"),
                st.get("left_channels"), " — СУХОЙ ПРОГОН" if dry_run else "")
    if dry_run:
        return план
    # Доноры стартуют с разносом в 5-15 минут: у них тоже общие адреса.
    задержка = 0
    for f in план["folders"]:
        make_folder.apply_async(args=[f], countdown=задержка)
        задержка += random.randint(300, 900)
    return план


@celery_app.task(name="tasks.react_subscribe.plan_react_subscriptions")
def plan_react_subscriptions(dry_run: bool = False):
    if os.getenv("RT_REACT_SUBSCRIBE", "1") != "1":
        return {"skipped": "RT_REACT_SUBSCRIBE выключен"}
    return run_async(_plan_and_dispatch(dry_run))["stats"]
