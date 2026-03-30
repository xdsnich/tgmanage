"""
GramGPT API — routers/actions.py
Быстрые действия над аккаунтами через веб.
По ТЗ раздел 2.5.
"""

import sys
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount

router = APIRouter(prefix="/actions", tags=["actions"])


# ── Safe CLI import ──────────────────────────────────────────

def _import_cli_actions():
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    api_config_cache = sys.modules.pop('config', None)
    for mod_name in ['ui', 'trust', 'actions', 'tg_client']:
        sys.modules.pop(mod_name, None)

    try:
        import actions as act
        return act
    finally:
        if api_config_cache:
            sys.modules['config'] = api_config_cache


# ── Schemas ──────────────────────────────────────────────────

class BulkActionRequest(BaseModel):
    account_ids: list[int]


# ── Helpers ──────────────────────────────────────────────────

async def _get_accounts(db, account_ids, user_id):
    result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id.in_(account_ids), TelegramAccount.user_id == user_id)
    )
    accounts = result.scalars().all()
    if not accounts:
        raise HTTPException(status_code=404, detail="Аккаунты не найдены")
    return accounts


def _to_dict(a):
    return {"phone": a.phone, "session_file": a.session_file, "status": a.status.value, "first_name": a.first_name or ""}


async def _run_action(action_name, action_attr, accounts):
    results = []
    try:
        act = _import_cli_actions()
        fn = getattr(act, action_attr, None)
        if not fn:
            return [{"phone": a.phone, "status": "error", "message": f"{action_attr} не найден"} for a in accounts]
        for acc in accounts:
            try:
                await fn(_to_dict(acc))
                results.append({"phone": acc.phone, "status": "success", "message": f"{action_name} выполнено"})
            except Exception as e:
                results.append({"phone": acc.phone, "status": "error", "message": str(e)})
    except ImportError:
        results = [{"phone": a.phone, "status": "skipped", "message": "CLI-модуль не найден"} for a in accounts]
    return results


# ── Endpoints ────────────────────────────────────────────────

@router.post("/leave-chats")
async def leave_chats(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "leave_chats", "results": await _run_action("Выход из чатов", "leave_all_chats", accounts)}

@router.post("/leave-channels")
async def leave_channels(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "leave_channels", "results": await _run_action("Отписка от каналов", "leave_all_channels", accounts)}

@router.post("/delete-dialogs")
async def delete_dialogs(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "delete_dialogs", "results": await _run_action("Удаление переписок", "delete_all_dialogs", accounts)}

@router.post("/read-all")
async def read_all(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "read_all", "results": await _run_action("Прочитать всё", "read_all_messages", accounts)}

@router.post("/clear-cache")
async def clear_cache(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "clear_cache", "results": await _run_action("Очистка кэша", "clear_cache", accounts)}

@router.post("/unpin-folders")
async def unpin_folders(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "unpin_folders", "results": await _run_action("Открепление папок", "unpin_folders", accounts)}