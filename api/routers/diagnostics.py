"""
GramGPT API — routers/diagnostics.py
Диагностика аккаунтов: тест подписки, проверка соединения, статус.

Использует ту же логику что и plan_executor.join_target_channel,
чтобы понять реально ли работает подписка для конкретного аккаунта.
"""

import os
import sys
import asyncio
import random
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class TestJoinRequest(BaseModel):
    account_id: int
    channel_username: str
    leave_after: bool = False  # выйти из канала после теста (если только что вступили)


class TestJoinStep(BaseModel):
    step: str
    label: str
    ok: bool
    detail: str = ""
    error: Optional[str] = None
    error_type: Optional[str] = None


@router.post("/test-join")
async def test_join(
    body: TestJoinRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Полный тест процесса подписки на канал.
    Прогоняет тот же flow что и plan_executor, но детально показывает каждый шаг.
    """
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import make_telethon_client

    # ── Загружаем аккаунт ─────────────────────────────────
    acc = (await db.execute(
        select(TelegramAccount)
        .options(joinedload(TelegramAccount.api_app))
        .where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )).scalar_one_or_none()

    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    clean_ch = body.channel_username.lstrip('@').strip()
    if not clean_ch:
        raise HTTPException(status_code=400, detail="Укажи username канала")

    steps = []
    final_member = False
    joined_now = False
    error = None
    error_type = None

    # Прокси
    proxy = None
    if acc.proxy_id:
        proxy = (await db.execute(
            select(Proxy).where(Proxy.id == acc.proxy_id)
        )).scalar_one_or_none()

    client = make_telethon_client(acc, proxy)
    if not client:
        steps.append({
            "step": "client", "label": "Создание клиента",
            "ok": False, "detail": "",
            "error": "Файл сессии не найден",
            "error_type": "NoSession",
        })
        return {
            "success": False, "is_member": False, "joined_now": False,
            "steps": steps, "error": "Файл сессии не найден",
            "error_type": "NoSession",
        }

    started = datetime.utcnow()

    try:
        # ── ШАГ 1: Подключение ───────────────────────────
        try:
            await client.connect()
            if not await client.is_user_authorized():
                steps.append({
                    "step": "connect", "label": "Подключение",
                    "ok": False, "detail": "Сессия неавторизована",
                    "error": "Not authorized",
                    "error_type": "NotAuthorized",
                })
                return {
                    "success": False, "is_member": False, "joined_now": False,
                    "steps": steps, "error": "Аккаунт неавторизован",
                    "error_type": "NotAuthorized",
                }
            steps.append({
                "step": "connect", "label": "Подключение",
                "ok": True, "detail": f"Подключен (прокси: {proxy.host + ':' + str(proxy.port) if proxy else 'нет'})",
            })
        except Exception as e:
            steps.append({
                "step": "connect", "label": "Подключение",
                "ok": False, "detail": "",
                "error": str(e)[:200], "error_type": type(e).__name__,
            })
            return {
                "success": False, "is_member": False, "joined_now": False,
                "steps": steps, "error": str(e)[:200],
                "error_type": type(e).__name__,
            }

        me = await client.get_me()

        # ── ШАГ 2: Резолв канала ─────────────────────────
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        from telethon.errors import (
            UsernameNotOccupiedError, UsernameInvalidError,
            FloodWaitError,
        )

        try:
            resolved = await client(ResolveUsernameRequest(clean_ch))
            if not resolved.chats:
                steps.append({
                    "step": "resolve", "label": "Поиск канала",
                    "ok": False, "detail": f"@{clean_ch}",
                    "error": "Канал не найден на сервере",
                    "error_type": "NotFound",
                })
                return {
                    "success": False, "is_member": False, "joined_now": False,
                    "steps": steps, "error": "Канал не существует",
                    "error_type": "NotFound",
                }
            entity = resolved.chats[0]
            ch_title = getattr(entity, 'title', '?')
            ch_id = getattr(entity, 'id', None)
            steps.append({
                "step": "resolve", "label": "Поиск канала",
                "ok": True, "detail": f"@{clean_ch} → «{ch_title}» (id: {ch_id})",
            })
        except (UsernameNotOccupiedError, UsernameInvalidError) as e:
            steps.append({
                "step": "resolve", "label": "Поиск канала",
                "ok": False, "detail": "",
                "error": f"Невалидный username: {str(e)[:150]}",
                "error_type": type(e).__name__,
            })
            return {
                "success": False, "is_member": False, "joined_now": False,
                "steps": steps, "error": f"@{clean_ch}: невалидный username",
                "error_type": type(e).__name__,
            }
        except FloodWaitError as e:
            steps.append({
                "step": "resolve", "label": "Поиск канала",
                "ok": False, "detail": "",
                "error": f"FloodWait: ждать {e.seconds}с",
                "error_type": "FloodWaitError",
            })
            return {
                "success": False, "is_member": False, "joined_now": False,
                "steps": steps, "error": f"FloodWait {e.seconds}с — аккаунт в флудвейте",
                "error_type": "FloodWaitError",
            }
        except Exception as e:
            steps.append({
                "step": "resolve", "label": "Поиск канала",
                "ok": False, "detail": "",
                "error": str(e)[:200], "error_type": type(e).__name__,
            })
            return {
                "success": False, "is_member": False, "joined_now": False,
                "steps": steps, "error": str(e)[:200],
                "error_type": type(e).__name__,
            }

        # ── ШАГ 3: Pre-check (уже подписан?) ─────────────
        from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
        from telethon.errors import (
            UserAlreadyParticipantError, InviteRequestSentError,
            ChannelsTooMuchError, ChannelPrivateError,
            UserBannedInChannelError,
        )

        already_in = False
        try:
            await client(GetParticipantRequest(channel=entity, participant=me))
            already_in = True
            steps.append({
                "step": "pre_check", "label": "Проверка перед join",
                "ok": True, "detail": "Аккаунт УЖЕ подписан на канал — JoinRequest не нужен",
            })
            final_member = True
        except Exception as e:
            steps.append({
                "step": "pre_check", "label": "Проверка перед join",
                "ok": True, "detail": f"Не подписан → попробуем вступить ({type(e).__name__})",
            })

        # ── ШАГ 4: JoinChannelRequest ───────────────────
        if not already_in:
            try:
                await client(JoinChannelRequest(entity))
                joined_now = True
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": True, "detail": "Запрос отправлен, Telegram принял (без ошибок)",
                })
            except UserAlreadyParticipantError:
                already_in = True
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": True, "detail": "Telegram сообщил: уже подписан",
                })
            except InviteRequestSentError:
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": False, "detail": "Канал требует одобрения админа",
                    "error": "Отправлена заявка, ждёт подтверждения",
                    "error_type": "InviteRequestSentError",
                })
                error = "Канал требует одобрения — заявка отправлена"
                error_type = "InviteRequestSentError"
            except ChannelsTooMuchError:
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": False, "detail": "Лимит каналов на аккаунте достигнут (500)",
                    "error": "ChannelsTooMuchError",
                    "error_type": "ChannelsTooMuchError",
                })
                error = "Лимит каналов на аккаунте (500)"
                error_type = "ChannelsTooMuchError"
            except ChannelPrivateError:
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": False, "detail": "Приватный канал — нужен invite link",
                    "error": "ChannelPrivateError",
                    "error_type": "ChannelPrivateError",
                })
                error = "Канал приватный"
                error_type = "ChannelPrivateError"
            except UserBannedInChannelError:
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": False, "detail": "Аккаунт забанен в этом канале",
                    "error": "UserBannedInChannelError",
                    "error_type": "UserBannedInChannelError",
                })
                error = "Аккаунт забанен в канале"
                error_type = "UserBannedInChannelError"
            except FloodWaitError as e:
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": False, "detail": f"FloodWait: ждать {e.seconds}с",
                    "error": f"FloodWait {e.seconds}с",
                    "error_type": "FloodWaitError",
                })
                error = f"FloodWait {e.seconds}с — аккаунт в флудвейте"
                error_type = "FloodWaitError"
            except Exception as e:
                steps.append({
                    "step": "join", "label": "JoinChannelRequest",
                    "ok": False, "detail": "",
                    "error": str(e)[:200], "error_type": type(e).__name__,
                })
                error = str(e)[:200]
                error_type = type(e).__name__

            # ── ШАГ 5: Верификация (только если join прошёл без явных ошибок) ──
            if joined_now or already_in:
                wait_sec = round(random.uniform(2, 4), 1)
                await asyncio.sleep(wait_sec)
                try:
                    await client(GetParticipantRequest(channel=entity, participant=me))
                    final_member = True
                    steps.append({
                        "step": "verify", "label": f"Верификация ({wait_sec}с пауза)",
                        "ok": True, "detail": "Подписка ПОДТВЕРЖДЕНА — аккаунт в участниках канала",
                    })
                except Exception as ve:
                    steps.append({
                        "step": "verify", "label": f"Верификация ({wait_sec}с пауза)",
                        "ok": False, "detail": "JoinRequest вернулся без ошибок, но аккаунт НЕ в участниках",
                        "error": f"Возможен теневой бан / заморозка / hidden restriction ({type(ve).__name__}: {str(ve)[:120]})",
                        "error_type": type(ve).__name__,
                    })
                    error = f"Подписка не прошла верификацию: {type(ve).__name__}"
                    error_type = type(ve).__name__

        # ── ШАГ 6: Выход (если просили и только что вступили) ──
        if body.leave_after and joined_now and final_member:
            try:
                from telethon.tl.functions.channels import LeaveChannelRequest
                await client(LeaveChannelRequest(entity))
                steps.append({
                    "step": "cleanup", "label": "Выход из канала (тест)",
                    "ok": True, "detail": "LeaveChannelRequest — аккаунт вышел из канала",
                })
            except Exception as e:
                steps.append({
                    "step": "cleanup", "label": "Выход из канала (тест)",
                    "ok": False, "detail": "",
                    "error": str(e)[:200], "error_type": type(e).__name__,
                })

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    elapsed = (datetime.utcnow() - started).total_seconds()

    return {
        "success": final_member,
        "is_member": final_member,
        "joined_now": joined_now and not already_in,
        "already_in": already_in,
        "channel": clean_ch,
        "account_phone": acc.phone,
        "elapsed_seconds": round(elapsed, 2),
        "steps": steps,
        "error": error,
        "error_type": error_type,
    }


@router.get("/account-channels/{account_id}")
async def list_account_subscriptions(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список каналов на которые подписан аккаунт (live из Telegram)."""
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import make_telethon_client

    acc = (await db.execute(
        select(TelegramAccount)
        .options(joinedload(TelegramAccount.api_app))
        .where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )).scalar_one_or_none()

    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    proxy = None
    if acc.proxy_id:
        proxy = (await db.execute(
            select(Proxy).where(Proxy.id == acc.proxy_id)
        )).scalar_one_or_none()

    client = make_telethon_client(acc, proxy)
    if not client:
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    channels = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Аккаунт неавторизован")

        from telethon.tl.types import Channel
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, Channel):
                channels.append({
                    "id": entity.id,
                    "title": entity.title,
                    "username": entity.username or "",
                    "is_broadcast": getattr(entity, "broadcast", False),
                    "is_megagroup": getattr(entity, "megagroup", False),
                    "members": getattr(entity, "participants_count", None),
                })
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")
    finally:
        try: await client.disconnect()
        except: pass

    return {"account_id": account_id, "phone": acc.phone, "total": len(channels), "channels": channels}
