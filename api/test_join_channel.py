"""
GramGPT — test_join_channel.py
Тестирует подписку аккаунта на канал, показывая каждый шаг.

Запуск:
  cd api
  python test_join_channel.py <account_id> <channel_username> [--leave]

Пример:
  python test_join_channel.py 5 DC_Draino
  python test_join_channel.py 5 @durov --leave
"""

import sys
import os
import asyncio
import argparse
import random
from datetime import datetime

# sys.path: api/ — первым, parent (tg_manager1/ с легаси config.py) — убрать
_API_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_API_DIR)
sys.path = [p for p in sys.path
            if os.path.normcase(os.path.abspath(p) if p else "") != os.path.normcase(_PARENT_DIR)]
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from config import DATABASE_URL
from models.account import TelegramAccount
from models.proxy import Proxy
from utils.telegram import make_telethon_client


# ── ANSI цвета ──────────────────────────────────────────
class C:
    R = "\033[31m"  # red
    G = "\033[32m"  # green
    Y = "\033[33m"  # yellow
    B = "\033[34m"  # blue
    C = "\033[36m"  # cyan
    M = "\033[35m"  # magenta
    W = "\033[37m"  # white
    BOLD = "\033[1m"
    DIM = "\033[2m"
    OFF = "\033[0m"


def ok(label, detail=""):
    print(f"  {C.G}✓{C.OFF} {C.BOLD}{label}{C.OFF}{C.DIM} — {detail}{C.OFF}" if detail else f"  {C.G}✓{C.OFF} {C.BOLD}{label}{C.OFF}")


def fail(label, err="", error_type=""):
    type_str = f"{C.Y}[{error_type}]{C.OFF} " if error_type else ""
    print(f"  {C.R}✕{C.OFF} {C.BOLD}{label}{C.OFF} → {type_str}{C.R}{err}{C.OFF}")


def info(label, detail=""):
    print(f"  {C.B}ℹ{C.OFF} {label}{C.DIM} — {detail}{C.OFF}" if detail else f"  {C.B}ℹ{C.OFF} {label}")


def header(text):
    print(f"\n{C.M}{C.BOLD}═══ {text} ═══{C.OFF}")


# ── Тест ────────────────────────────────────────────────
async def run_test(account_id: int, channel_username: str, leave_after: bool):
    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    clean_ch = channel_username.lstrip('@').strip()

    header(f"ТЕСТ ПОДПИСКИ: account_id={account_id} → @{clean_ch}")

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == account_id)
        )).scalar_one_or_none()

        if not acc:
            print(f"{C.R}{C.BOLD}ОШИБКА:{C.OFF} аккаунт #{account_id} не найден в БД")
            await engine.dispose()
            return

        print(f"{C.C}Аккаунт:{C.OFF} {acc.phone} ({acc.first_name or '?'} {acc.last_name or ''}) status={acc.status}")

        proxy = None
        if acc.proxy_id:
            proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
        print(f"{C.C}Прокси:{C.OFF} {f'{proxy.host}:{proxy.port}' if proxy else 'нет'}")

        client = make_telethon_client(acc, proxy)
        if not client:
            fail("Создание клиента", "Файл сессии не найден", "NoSession")
            await engine.dispose()
            return

    started = datetime.utcnow()

    try:
        header("ШАГИ ПОДПИСКИ")

        # 1) Подключение
        try:
            await client.connect()
            if not await client.is_user_authorized():
                fail("Подключение", "Сессия неавторизована", "NotAuthorized")
                return
            ok("Подключение", f"прокси: {f'{proxy.host}:{proxy.port}' if proxy else 'нет'}")
        except Exception as e:
            fail("Подключение", str(e)[:150], type(e).__name__)
            return

        me = await client.get_me()

        # 2) Резолв
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        from telethon.errors import (
            UsernameNotOccupiedError, UsernameInvalidError,
            FloodWaitError, UserAlreadyParticipantError,
            InviteRequestSentError, ChannelsTooMuchError,
            ChannelPrivateError, UserBannedInChannelError,
        )

        try:
            resolved = await client(ResolveUsernameRequest(clean_ch))
            if not resolved.chats:
                fail("Поиск канала", "канал не найден", "NotFound")
                return
            entity = resolved.chats[0]
            ok("Поиск канала", f"@{clean_ch} → «{getattr(entity, 'title', '?')}» (id: {entity.id})")
        except FloodWaitError as e:
            fail("Поиск канала", f"FloodWait {e.seconds}с", "FloodWaitError")
            return
        except Exception as e:
            fail("Поиск канала", str(e)[:150], type(e).__name__)
            return

        # 3) Pre-check
        from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest, LeaveChannelRequest
        already_in = False
        try:
            await client(GetParticipantRequest(channel=entity, participant=me))
            already_in = True
            ok("Pre-check", "аккаунт УЖЕ подписан, JoinRequest не нужен")
        except Exception as e:
            info("Pre-check", f"не подписан → пробуем вступить ({type(e).__name__})")

        # 4) Join
        joined_now = False
        final_member = already_in
        if not already_in:
            try:
                await client(JoinChannelRequest(entity))
                joined_now = True
                ok("JoinChannelRequest", "Telegram принял запрос без ошибок")
            except UserAlreadyParticipantError:
                already_in = True
                ok("JoinChannelRequest", "Telegram сообщил: уже подписан")
            except InviteRequestSentError:
                fail("JoinChannelRequest", "канал требует одобрения админа", "InviteRequestSentError")
                return
            except ChannelsTooMuchError:
                fail("JoinChannelRequest", "лимит каналов 500 достигнут", "ChannelsTooMuchError")
                return
            except ChannelPrivateError:
                fail("JoinChannelRequest", "приватный канал", "ChannelPrivateError")
                return
            except UserBannedInChannelError:
                fail("JoinChannelRequest", "аккаунт забанен в этом канале", "UserBannedInChannelError")
                return
            except FloodWaitError as e:
                fail("JoinChannelRequest", f"FloodWait {e.seconds}с", "FloodWaitError")
                return
            except Exception as e:
                fail("JoinChannelRequest", str(e)[:200], type(e).__name__)
                return

            # 5) Верификация (1 раз)
            wait_sec = round(random.uniform(2, 4), 1)
            info(f"Пауза {wait_sec}с перед верификацией")
            await asyncio.sleep(wait_sec)
            try:
                await client(GetParticipantRequest(channel=entity, participant=me))
                final_member = True
                ok("Верификация", "подписка ПОДТВЕРЖДЕНА — аккаунт в участниках")
            except Exception as ve:
                fail(
                    "Верификация",
                    f"JoinRequest прошёл без ошибок, но аккаунт НЕ участник → теневой бан / заморозка / hidden restriction ({str(ve)[:120]})",
                    type(ve).__name__,
                )
                final_member = False

        # 6) Leave (опционально)
        if leave_after and joined_now and final_member:
            try:
                await client(LeaveChannelRequest(entity))
                ok("LeaveChannelRequest", "вышли из канала (тест)")
            except Exception as e:
                fail("LeaveChannelRequest", str(e)[:150], type(e).__name__)

        # ── Итог ──
        elapsed = (datetime.utcnow() - started).total_seconds()
        header("ИТОГ")
        if final_member:
            if already_in and not joined_now:
                print(f"  {C.G}{C.BOLD}РЕЗУЛЬТАТ: уже был подписан{C.OFF}")
            else:
                print(f"  {C.G}{C.BOLD}РЕЗУЛЬТАТ: подписка работает{C.OFF}")
        else:
            print(f"  {C.R}{C.BOLD}РЕЗУЛЬТАТ: подписка НЕ прошла{C.OFF}")
        print(f"  {C.DIM}время: {elapsed:.2f}с{C.OFF}\n")

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Тест подписки аккаунта на канал")
    parser.add_argument("account_id", type=int, help="ID аккаунта в БД")
    parser.add_argument("channel", type=str, help="username канала (@durov или durov)")
    parser.add_argument("--leave", action="store_true", help="выйти из канала после теста")
    args = parser.parse_args()

    asyncio.run(run_test(args.account_id, args.channel, args.leave))


if __name__ == "__main__":
    main()
