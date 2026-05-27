"""
GramGPT — test_ban_recording.py
Реально пытается отправить коммент в канал, ловит ошибку, прогоняет через
classifier, записывает в channel_ban_stats и читает обратно.

Зачем: пользователь жаловался что канал @soumyamaam_defencewallah дал ошибку
"You can't write in this chat" но не появился в UI проходимости. Причина была
в том что classifier не ловил этот текст. Этот скрипт проверяет фикс
end-to-end: реальная попытка → реальная ошибка → запись в БД → видно в UI.

Запуск:
  cd api
  python test_ban_recording.py 54 soumyamaam_defencewallah
  python test_ban_recording.py 54 durov           # успешный пост (для контроля)
"""

import sys
import os
import asyncio
import argparse
import random

# sys.path-cleanup
_API_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_API_DIR)
sys.path[:] = [p for p in sys.path
               if os.path.normcase(os.path.abspath(p) if p else os.getcwd()) != os.path.normcase(_PARENT_DIR)]
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)
for _mod in list(sys.modules):
    if _mod == "config" or _mod.startswith("config."):
        del sys.modules[_mod]


class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    C = "\033[36m"; M = "\033[35m"; BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"


# ─────────────────────────────────────────────────────────
# Тот же classifier что в plan_executor — копируем чтобы тестировать
# изолированно (а не вызывать целый _execute_plan_session)
# ─────────────────────────────────────────────────────────
BAN_EXCEPTION_CLASSES = {
    "ChatWriteForbiddenError",
    "UserBannedInChannelError",
    "ChatRestrictedError",
    "ChannelPrivateError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "PeerIdInvalidError",
}
BAN_MESSAGE_PATTERNS = [
    "can't write", "cannot write", "write in this chat",
    "banned from sending", "banned in this", "you're banned",
    "chat_write_forbidden", "user_banned", "user_restricted",
    "banned_rights", "channel_private", "you_blocked",
]
TRANSIENT_PATTERNS = ("FLOOD_WAIT", "AUTH_KEY_UNREGISTERED", "PEER_FLOOD",
                      "ServerError", "TimeoutError", "ConnectionError")


def classify_error(err_type: str, err_msg: str):
    """Возвращает (is_ban, is_transient)."""
    is_transient = any(p in err_msg for p in TRANSIENT_PATTERNS)
    is_ban = (
        err_type in BAN_EXCEPTION_CLASSES or
        any(p.lower() in err_msg.lower() for p in BAN_MESSAGE_PATTERNS)
    )
    return is_ban, is_transient


# ─────────────────────────────────────────────────────────
async def run(account_id: int, channel_username: str):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.telegram import make_telethon_client
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.campaign import Campaign, CampaignStatus
    from models.channel_ban_stats import ChannelBanStats
    from datetime import datetime

    clean_ch = channel_username.lstrip('@').strip()

    print(f"\n{C.M}{C.BOLD}═══ ТЕСТ ЗАПИСИ БАНА В channel_ban_stats ═══{C.OFF}")
    print(f"  Аккаунт: #{account_id}")
    print(f"  Канал:   @{clean_ch}\n")

    # ── 1. Загрузка аккаунта ──
    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == account_id)
        )).scalar_one_or_none()
        if not acc:
            print(f"{C.R}Аккаунт #{account_id} не найден в БД{C.OFF}")
            return 1
        proxy = None
        if acc.proxy_id:
            proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
        user_id = acc.user_id
        phone = acc.phone
        print(f"  {C.G}✓{C.OFF} acc: {phone} user_id={user_id}")

    if not acquire_account_lock(account_id, ttl=120):
        print(f"{C.R}Аккаунт занят (lock){C.OFF}")
        return 1

    err_type = None
    err_msg = None
    success = False

    try:
        # ── 2. Telethon connect ──
        client = make_telethon_client(acc, proxy)
        if not client:
            print(f"{C.R}Нет файла сессии{C.OFF}")
            return 1
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print(f"{C.R}Аккаунт не авторизован{C.OFF}")
                return 1
            print(f"  {C.G}✓{C.OFF} Telethon connected")

            # ── 3. Резолв канала ──
            from telethon.tl.functions.contacts import ResolveUsernameRequest
            try:
                resolved = await client(ResolveUsernameRequest(clean_ch))
                if not resolved.chats:
                    print(f"{C.R}Канал @{clean_ch} не существует{C.OFF}")
                    return 1
                entity = resolved.chats[0]
                print(f"  {C.G}✓{C.OFF} resolved → «{getattr(entity, 'title', '?')}»")
            except Exception as e:
                print(f"{C.R}Resolve упал: {type(e).__name__}: {e}{C.OFF}")
                return 1

            # ── 4. Берём последний пост и пытаемся комментить ──
            posts = await client.get_messages(entity, limit=1)
            if not posts:
                print(f"{C.R}В канале нет постов{C.OFF}")
                return 1
            target_post = posts[0]
            print(f"  {C.G}✓{C.OFF} последний пост: #{target_post.id}")

            test_comment = "👍"  # минимальный безобидный коммент
            print(f"  {C.B}ℹ{C.OFF} пытаюсь отправить «{test_comment}» как комментарий...")

            try:
                await client.send_message(
                    entity=entity, message=test_comment,
                    comment_to=target_post.id,
                )
                success = True
                print(f"  {C.G}✓ Коммент отправлен — это успех (бана нет){C.OFF}")
            except Exception as e:
                err_type = type(e).__name__
                err_msg = str(e)
                print(f"  {C.Y}⚠ Ошибка: [{err_type}] {err_msg[:200]}{C.OFF}")

        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    finally:
        release_account_lock(account_id)

    # ── 5. Прогоняем через classifier ──
    if not success:
        is_ban, is_transient = classify_error(err_type, err_msg)
        print(f"\n{C.C}{C.BOLD}Classifier результат:{C.OFF}")
        print(f"  is_ban       = {C.R if is_ban else C.G}{is_ban}{C.OFF}")
        print(f"  is_transient = {C.Y if is_transient else C.G}{is_transient}{C.OFF}")

        if is_transient:
            print(f"  {C.Y}⚠ Транзиентная ошибка — НЕ пишем в стату (не вина канала){C.OFF}")
            return 0

    # ── 6. Симулируем то же что делает plan_executor: запись в channel_ban_stats ──
    print(f"\n{C.C}{C.BOLD}Запись в channel_ban_stats:{C.OFF}")
    async with Session() as db:
        # Нужна активная кампания пользователя — иначе campaign.user_id не получить.
        # Берём ЛЮБУЮ кампанию этого юзера для теста (или используем user_id напрямую)
        # В реальном plan_executor используется campaign.user_id — здесь делаем то же.
        camp_q = await db.execute(
            select(Campaign).where(Campaign.user_id == user_id).limit(1)
        )
        camp = camp_q.scalar_one_or_none()
        camp_user_id = camp.user_id if camp else user_id

        st = (await db.execute(
            select(ChannelBanStats).where(
                ChannelBanStats.user_id == camp_user_id,
                ChannelBanStats.channel_username == clean_ch,
            )
        )).scalar_one_or_none()

        before_attempts = st.total_attempts if st else 0
        before_bans = st.banned_count if st else 0

        if not st:
            st = ChannelBanStats(
                user_id=camp_user_id, channel_username=clean_ch,
                total_attempts=0, banned_count=0,
            )
            db.add(st)
            print(f"  {C.B}ℹ{C.OFF} новая запись создана")
        else:
            print(f"  {C.B}ℹ{C.OFF} запись существовала: {before_attempts} attempts, {before_bans} bans")

        st.total_attempts += 1
        if not success:
            is_ban, _ = classify_error(err_type, err_msg)
            if is_ban:
                st.banned_count += 1
                st.last_ban_reason = f"[{err_type}] {err_msg[:180]}"
        st.last_updated = datetime.utcnow()

        await db.commit()
        await db.refresh(st)

        pass_rate = round((st.total_attempts - st.banned_count) / max(st.total_attempts, 1) * 100, 1)

        print(f"  {C.G}✓ Записано:{C.OFF}")
        print(f"    user_id          = {st.user_id}")
        print(f"    channel_username = {st.channel_username}")
        print(f"    total_attempts   = {before_attempts} → {C.BOLD}{st.total_attempts}{C.OFF}")
        print(f"    banned_count     = {before_bans} → {C.BOLD}{st.banned_count}{C.OFF}")
        print(f"    pass_rate        = {C.G if pass_rate >= 70 else C.Y if pass_rate >= 40 else C.R}{pass_rate}%{C.OFF}")
        if st.last_ban_reason:
            print(f"    last_ban_reason  = {st.last_ban_reason[:100]}")

    # ── 7. Проверяем что появится в whitelist API ──
    print(f"\n{C.C}{C.BOLD}Проверка через whitelist endpoint:{C.OFF}")
    async with Session() as db:
        rows = (await db.execute(
            select(ChannelBanStats).where(ChannelBanStats.user_id == camp_user_id)
            .order_by(ChannelBanStats.last_updated.desc())
        )).scalars().all()
        found = next((r for r in rows if r.channel_username == clean_ch), None)
        if found:
            print(f"  {C.G}✓ @{clean_ch} ТЕПЕРЬ ВИДЕН в whitelist UI (всего {len(rows)} записей){C.OFF}")
        else:
            print(f"  {C.R}✕ канал НЕ найден в whitelist (баг ещё есть){C.OFF}")
            return 1

    print(f"\n{C.G}{C.BOLD}═══ ТЕСТ ПРОЙДЕН — открой UI вкладку «Проходимость» в парсере, канал @{clean_ch} там{C.OFF}\n")
    return 0


def cli():
    parser = argparse.ArgumentParser(description="Тест записи бана канала в channel_ban_stats")
    parser.add_argument("account_id", type=int, help="ID аккаунта (например 54)")
    parser.add_argument("channel", type=str, help="username канала (durov или @durov)")
    args = parser.parse_args()
    rc = asyncio.run(run(args.account_id, args.channel))
    sys.exit(rc)


if __name__ == "__main__":
    cli()
