"""
Standalone test для check_only-логики:
  1. Берём 3 указанных акка + их прокси
  2. Берём 30 случайных каналов с has_comments=True из БД
  3. Распределяем каналы round-robin между акками (по 10 на акк)
  4. Async: 3 воркера параллельно (разные прокси → нет ip коллизии)
  5. Каждый воркер:
      a. connect() ОДИН РАЗ
      b. по своим 10 каналам: read_feed → post comment → save
      c. FloodWait → сразу выход, БЕЗ retry
      d. Между каналами — короткий cooldown 30-60с (тестовый)
      e. disconnect()
  6. Ждём 5 минут (модераторам/ботам время удалить)
  7. Async: 3 воркера параллельно verify — get_messages по сохранённым msg_id
  8. Обновляем ChannelBanStats + печатаем сводку

Ограничение: 60 минут ИЛИ первое удаление зафиксировано → всё равно
финализируем и печатаем сводку.

Запуск:
    cd api
    python -m scripts.test_check_only
"""
from __future__ import annotations
import asyncio
import os
import sys
import random
import time
from datetime import datetime, timedelta
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_API_DIR = os.path.abspath(os.path.join(_HERE, "..", "api"))
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_API_DIR, ".env"))


TEST_PHONES = [
    "+380660744731",
    "+380661192531",
    "+380955259581",
]
CHANNELS_TOTAL = 30
CHANNELS_PER_ACC = CHANNELS_TOTAL // len(TEST_PHONES)
MAX_TEST_MINUTES = 60
COMMENTS = [
    "интересно, спасибо",
    "полезно 👍",
    "хорошая тема",
    "надо подумать",
    "спасибо за инфу",
    "тоже об этом думал",
    "актуально",
    "правильно",
    "поддерживаю",
    "хм",
    "любопытно",
    "в тему",
]


async def _load_data():
    from sqlalchemy import select, func
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.parsed_channel import ParsedChannel
    from utils.db_pool import async_session as Session

    async with Session() as db:
        accs = []
        for phone in TEST_PHONES:
            r = await db.execute(
                select(TelegramAccount)
                .options(joinedload(TelegramAccount.api_app))
                .where(TelegramAccount.phone == phone)
            )
            a = r.scalar_one_or_none()
            if not a:
                print(f"❌ Аккаунт {phone} НЕ НАЙДЕН в БД")
                continue
            proxy = None
            if a.proxy_id:
                pr = await db.execute(select(Proxy).where(Proxy.id == a.proxy_id))
                proxy = pr.scalar_one_or_none()
            if not proxy:
                print(f"⚠️ {phone}: нет прокси, пропускаем")
                continue
            accs.append((a, proxy))
            print(f"✅ {phone} | proxy={proxy.host}:{proxy.port} | user_id={a.user_id}")

        if not accs:
            return [], []

        # 30 случайных каналов с комментами
        # Берём каналов больше — с запасом на «канал закрыт»/удалён
        ch_r = await db.execute(
            select(ParsedChannel).where(
                ParsedChannel.has_comments == True,
                ParsedChannel.user_id == accs[0][0].user_id,
            ).order_by(func.random()).limit(CHANNELS_TOTAL * 2)
        )
        ch_list = ch_r.scalars().all()
        channels = [c.username for c in ch_list if c.username][:CHANNELS_TOTAL]
        print(f"📡 Взято {len(channels)} каналов с комментами из БД юзера {accs[0][0].user_id}")
        return accs, channels


def _phone_short(phone: str) -> str:
    return "…" + phone[-6:]


async def _try_join_discussion(client, channel_entity, ps: str, ch_username: str) -> bool:
    """При ChatGuestSendForbidden надо вступить в linked discussion group.
    Возвращает True если удалось (или уже в ней), False если нет."""
    from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
    from telethon.errors import FloodWaitError, UserAlreadyParticipantError
    try:
        full = await client(GetFullChannelRequest(channel_entity))
        linked_id = getattr(full.full_chat, "linked_chat_id", 0) or 0
        if not linked_id:
            print(f"[send][{ps}]     ⚠ @{ch_username}: нет linked discussion — не можем join")
            return False
        disc = await client.get_entity(linked_id)
        try:
            await client(JoinChannelRequest(disc))
            print(f"[send][{ps}]     🔗 join OK: discussion @{ch_username}")
            await asyncio.sleep(random.uniform(2, 4))
            return True
        except UserAlreadyParticipantError:
            print(f"[send][{ps}]     🔗 уже в discussion @{ch_username}")
            return True
    except FloodWaitError:
        raise
    except Exception as e:
        print(f"[send][{ps}]     ⚠ join fail @{ch_username}: {type(e).__name__}: {str(e)[:80]}")
        return False


async def _worker_send(acc, proxy, my_channels: list[str], sent_log: dict):
    """Отправка комментов. Оборачиваем ВСЁ в try — иначе gather проглотит."""
    import traceback
    from utils.telegram import make_telethon_client
    from telethon.errors import (
        FloodWaitError, ChatGuestSendForbiddenError,
        ChatWriteForbiddenError, ChannelPrivateError,
    )

    phone = acc.phone
    ps = _phone_short(phone)
    try:
        client = make_telethon_client(acc, proxy)
    except Exception as e:
        print(f"[send][{ps}] ❌ make_client fail: {type(e).__name__}: {e}")
        traceback.print_exc()
        return
    if not client:
        print(f"[send][{ps}] ❌ клиент не создался (make_telethon_client вернул None)")
        return
    try:
        try:
            await client.connect()
        except Exception as e:
            print(f"[send][{ps}] ❌ connect() fail: {type(e).__name__}: {e}")
            traceback.print_exc()
            return
        try:
            authed = await client.is_user_authorized()
        except Exception as e:
            print(f"[send][{ps}] ❌ is_user_authorized fail: {type(e).__name__}: {e}")
            return
        if not authed:
            print(f"[send][{ps}] ❌ не авторизован (session файл невалиден или удалён на TG)")
            return
        print(f"[send][{ps}] 🔗 подключён, обрабатываю {len(my_channels)} каналов")

        for idx, ch_username in enumerate(my_channels, 1):
            try:
                chat = await client.get_entity(ch_username)
            except FloodWaitError as e:
                print(f"[send][{ps}] ⏰ FloodWait {e.seconds}с на get_entity @{ch_username} — ВЫХОД")
                return
            except Exception as e:
                print(f"[send][{ps}] ⚠️ get_entity @{ch_username} fail: {type(e).__name__}")
                continue

            # Читаем последний пост
            try:
                msgs = await client.get_messages(chat, limit=1)
                if not msgs:
                    print(f"[send][{ps}]   [{idx}] @{ch_username} — постов нет"); continue
                post = msgs[0]
                if not post or not post.id:
                    continue
            except FloodWaitError as e:
                print(f"[send][{ps}] ⏰ FloodWait {e.seconds}с — ВЫХОД")
                return
            except Exception:
                continue

            # Комментим (с auto-join retry при ChatGuestSendForbidden)
            txt = random.choice(COMMENTS)
            sent = None
            _joined_this = False
            for _attempt in range(2):
                try:
                    sent = await client.send_message(entity=chat, message=txt, comment_to=post.id)
                    break
                except FloodWaitError as e:
                    print(f"[send][{ps}] ⏰ FloodWait {e.seconds}с на send — ВЫХОД")
                    return
                except ChatGuestSendForbiddenError:
                    if _attempt == 0:
                        # Пробуем вступить в discussion и повторить
                        try:
                            ok = await _try_join_discussion(client, chat, ps, ch_username)
                        except FloodWaitError as fw:
                            print(f"[send][{ps}] ⏰ FloodWait {fw.seconds}с на join — ВЫХОД")
                            return
                        if ok:
                            _joined_this = True
                            continue
                    print(f"[send][{ps}]   [{idx}] @{ch_username} #{post.id} SEND FAIL: ChatGuestSendForbidden (join не помог)")
                    break
                except ChatWriteForbiddenError:
                    print(f"[send][{ps}]   [{idx}] @{ch_username} #{post.id} SKIP: ChatWriteForbidden (нельзя писать вообще)")
                    break
                except ChannelPrivateError:
                    print(f"[send][{ps}]   [{idx}] @{ch_username} SKIP: канал приватный")
                    break
                except Exception as e:
                    print(f"[send][{ps}]   [{idx}] @{ch_username} #{post.id} SEND FAIL: {type(e).__name__}: {str(e)[:100]}")
                    break
            if sent is None:
                continue

            peer_id = None
            msg_id = None
            try:
                msg_id = int(getattr(sent, "id", 0) or 0)
                peer = getattr(sent, "peer_id", None)
                if peer is not None:
                    peer_id = (
                        getattr(peer, "channel_id", None)
                        or getattr(peer, "chat_id", None)
                        or getattr(peer, "user_id", None)
                    )
                    if peer_id is not None:
                        peer_id = int(peer_id)
            except Exception:
                pass
            sent_log[ch_username] = {
                "phone": phone,
                "peer_id": peer_id,
                "msg_id": msg_id,
                "sent_at": time.time(),
                "comment": txt,
            }
            print(f"[send][{ps}]   [{idx}] ✅ @{ch_username} #{post.id} → {txt!r} (disc={peer_id}, msg={msg_id})")

            # Пауза 30-60с (тестовый темп)
            await asyncio.sleep(random.uniform(30, 60))
    finally:
        try: await client.disconnect()
        except Exception: pass


async def _worker_verify(acc, proxy, my_channels: list[str], sent_log: dict, results: dict):
    """Верификация после паузы. Возвращает {channel: 'ok'|'deleted'|'unknown'}"""
    from utils.telegram import make_telethon_client
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.types import MessageEmpty
    from telethon.errors import FloodWaitError

    phone = acc.phone
    ps = _phone_short(phone)
    client = make_telethon_client(acc, proxy)
    if not client:
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return
        print(f"[verify][{ps}] 🔍 подключён, проверяю {len(my_channels)} комментов")

        disc_cache: dict = {}
        for idx, ch_username in enumerate(my_channels, 1):
            info = sent_log.get(ch_username)
            if not info or not info.get("msg_id"):
                results[ch_username] = ("skipped", "не отправлялся или msg_id пусто")
                continue
            try:
                disc = disc_cache.get(ch_username)
                if disc is None:
                    ch = await client.get_entity(ch_username)
                    full = await client(GetFullChannelRequest(ch))
                    linked = getattr(full.full_chat, "linked_chat_id", 0) or 0
                    disc = await client.get_entity(linked) if linked else ch
                    disc_cache[ch_username] = disc
                msgs = await client.get_messages(disc, ids=[info["msg_id"]])
                m = msgs[0] if msgs else None
                is_del = (
                    m is None
                    or isinstance(m, MessageEmpty)
                    or (getattr(m, "message", None) is None
                        and getattr(m, "media", None) is None)
                )
                results[ch_username] = ("deleted" if is_del else "ok", info["msg_id"])
                icon = "❌" if is_del else "✓"
                print(f"[verify][{ps}]   [{idx}] {icon} @{ch_username} msg#{info['msg_id']}")
            except FloodWaitError as e:
                print(f"[verify][{ps}] ⏰ FloodWait {e.seconds}с — стоп")
                results[ch_username] = ("flood", str(e.seconds))
                return
            except Exception as e:
                results[ch_username] = ("unknown", f"{type(e).__name__}: {str(e)[:80]}")
                print(f"[verify][{ps}]   [{idx}] ⚠️ @{ch_username}: {type(e).__name__}")
            await asyncio.sleep(random.uniform(1.5, 3.5))
    finally:
        try: await client.disconnect()
        except Exception: pass


async def _write_stats(user_id: int, results: dict):
    """Пишем ChannelBanStats: total_attempts++, если deleted → banned_count++."""
    from sqlalchemy import select
    from models.channel_ban_stats import ChannelBanStats
    from utils.db_pool import async_session as Session

    async with Session() as db:
        for ch_username, (status, extra) in results.items():
            if status not in ("ok", "deleted"):
                continue
            r = await db.execute(
                select(ChannelBanStats).where(
                    ChannelBanStats.user_id == user_id,
                    ChannelBanStats.channel_username == ch_username,
                )
            )
            s = r.scalar_one_or_none()
            if s is None:
                s = ChannelBanStats(user_id=user_id, channel_username=ch_username)
                db.add(s)
            s.total_attempts = (s.total_attempts or 0) + 1
            if status == "deleted":
                s.banned_count = (s.banned_count or 0) + 1
                s.last_ban_reason = f"test_check_only: msg удалён"
        await db.commit()


async def main():
    started = time.time()
    print("=" * 70)
    print(f"TEST check_only — {datetime.now()}")
    print(f"  accs: {TEST_PHONES}")
    print(f"  channels: {CHANNELS_TOTAL} ({CHANNELS_PER_ACC}/acc)")
    print(f"  time-cap: {MAX_TEST_MINUTES} мин")
    print("=" * 70)

    accs, channels = await _load_data()
    if not accs or not channels:
        print("❌ Не хватает данных для теста")
        return

    if len(channels) < CHANNELS_TOTAL:
        print(f"⚠️ Взято только {len(channels)} каналов (запрашивалось {CHANNELS_TOTAL})")

    # Распределение round-robin
    by_acc = defaultdict(list)
    for i, ch in enumerate(channels):
        acc_i = i % len(accs)
        by_acc[accs[acc_i][0].id].append(ch)
    for a, _ in accs:
        print(f"  → {_phone_short(a.phone)}: {len(by_acc[a.id])} каналов")

    sent_log: dict = {}
    print("\n[phase 1] SEND — 3 воркера параллельно")
    tasks = [
        _worker_send(a, p, by_acc[a.id], sent_log)
        for a, p in accs
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n[phase 1] отправлено: {len(sent_log)} комментов")
    if not sent_log:
        print("❌ Ни один коммент не отправился — тест закончен")
        return

    # Пауза перед verify — 5 минут (модераторам / ботам время)
    _wait = min(300, MAX_TEST_MINUTES * 60 - int(time.time() - started) - 30)
    if _wait > 0:
        print(f"\n[wait] спим {_wait}с (модерации время удалить)")
        await asyncio.sleep(_wait)
    else:
        print("[wait] лимит времени — пропускаем паузу, сразу verify")

    print("\n[phase 2] VERIFY — 3 воркера параллельно")
    results: dict = {}
    tasks = [
        _worker_verify(a, p, by_acc[a.id], sent_log, results)
        for a, p in accs
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    # Сводка
    print("\n" + "=" * 70)
    ok_c = sum(1 for v in results.values() if v[0] == "ok")
    del_c = sum(1 for v in results.values() if v[0] == "deleted")
    unk_c = sum(1 for v in results.values() if v[0] not in ("ok", "deleted"))
    print(f"RESULTS: ✓ ok={ok_c}   ❌ deleted={del_c}   ⚠ unknown={unk_c}")
    print("=" * 70)
    for ch, (st, ex) in sorted(results.items()):
        icon = {"ok": "✓", "deleted": "❌", "flood": "⏰"}.get(st, "?")
        print(f"  {icon} @{ch:35s}  {st:10s}  {ex}")

    # Записываем в ChannelBanStats
    user_id = accs[0][0].user_id
    await _write_stats(user_id, results)
    print(f"\n💾 ChannelBanStats обновлён для user_id={user_id}")

    total = int(time.time() - started)
    print(f"\n⏱ полное время: {total // 60}м {total % 60}с")


if __name__ == "__main__":
    asyncio.run(main())
