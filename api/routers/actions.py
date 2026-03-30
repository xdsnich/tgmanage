"""
GramGPT API — routers/actions.py
Быстрые действия над аккаунтами через веб.
По ТЗ раздел 2.5: автовыход из чатов, отписка, удаление переписок, прочитать всё, кэш, папки.
"""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount

router = APIRouter(prefix="/actions", tags=["actions"])


# ── Schemas ──────────────────────────────────────────────────

class BulkActionRequest(BaseModel):
    account_ids: list[int]


# ── Helper ───────────────────────────────────────────────────

async def _get_accounts(db, account_ids: list[int], user_id: int) -> list[TelegramAccount]:
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id.in_(account_ids),
            TelegramAccount.user_id == user_id,
        )
    )
    accounts = result.scalars().all()
    if not accounts:
        raise HTTPException(status_code=404, detail="Аккаунты не найдены")
    return accounts


def _to_dict(a: TelegramAccount) -> dict:
    return {
        "phone": a.phone,
        "session_file": a.session_file,
        "status": a.status.value,
        "first_name": a.first_name or "",
    }


async def _run_action(action_func, accounts, action_name: str):
    """Запускает действие над списком аккаунтов, собирает результаты"""
    results = []
    for acc in accounts:
        acc_dict = _to_dict(acc)
        try:
            await action_func(acc_dict)
            results.append({
                "phone": acc.phone,
                "status": "success",
                "message": f"{action_name} выполнено",
            })
        except Exception as e:
            results.append({
                "phone": acc.phone,
                "status": "error",
                "message": str(e),
            })
    return results


# ── Endpoints ────────────────────────────────────────────────

@router.post("/leave-chats")
async def leave_chats(
    body: BulkActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Автовыход из всех групповых чатов.
    По ТЗ: автовыход из всех чатов для выбранного пула аккаунтов.
    """
    accounts = await _get_accounts(db, body.account_ids, current_user.id)

    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        import actions as act
        results = await _run_action(act.leave_all_chats, accounts, "Выход из чатов")
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль actions не найден"} for a in accounts]

    return {"action": "leave_chats", "results": results}


@router.post("/leave-channels")
async def leave_channels(
    body: BulkActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Отписка от всех каналов.
    По ТЗ: отписка от всех каналов.
    """
    accounts = await _get_accounts(db, body.account_ids, current_user.id)

    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        import actions as act
        results = await _run_action(act.leave_all_channels, accounts, "Отписка от каналов")
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль actions не найден"} for a in accounts]

    return {"action": "leave_channels", "results": results}


@router.post("/delete-dialogs")
async def delete_dialogs(
    body: BulkActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Удаление личных переписок.
    По ТЗ: удаление личных переписок.
    """
    accounts = await _get_accounts(db, body.account_ids, current_user.id)

    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        import actions as act
        results = await _run_action(act.delete_all_dialogs, accounts, "Удаление переписок")
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль actions не найден"} for a in accounts]

    return {"action": "delete_dialogs", "results": results}


@router.post("/read-all")
async def read_all(
    body: BulkActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Отметка всех сообщений как прочитанных.
    По ТЗ: отметка всех сообщений как прочитанных.
    """
    accounts = await _get_accounts(db, body.account_ids, current_user.id)

    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        import actions as act
        results = await _run_action(act.read_all_messages, accounts, "Прочитать всё")
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль actions не найден"} for a in accounts]

    return {"action": "read_all", "results": results}


@router.post("/clear-cache")
async def clear_cache(
    body: BulkActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Удаление кэша Telegram.
    По ТЗ: удаление кэша Telegram.
    """
    accounts = await _get_accounts(db, body.account_ids, current_user.id)

    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        import actions as act
        results = await _run_action(act.clear_cache, accounts, "Очистка кэша")
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль actions не найден"} for a in accounts]

    return {"action": "clear_cache", "results": results}


@router.post("/unpin-folders")
async def unpin_folders(
    body: BulkActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Открепление папок (высвобождение лимитов).
    По ТЗ: открепление папок для высвобождения лимитов.
    """
    accounts = await _get_accounts(db, body.account_ids, current_user.id)

    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        import actions as act
        results = await _run_action(act.unpin_folders, accounts, "Открепление папок")
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль actions не найден"} for a in accounts]

    return {"action": "unpin_folders", "results": results}