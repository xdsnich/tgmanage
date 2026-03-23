"""
GramGPT API — routers/accounts.py
Эндпоинты: управление Telegram аккаунтами
"""

from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from schemas.account import AccountCreate, AccountUpdate, AccountOut, AccountCheckResult
from services import accounts as acc_svc
from routers.deps import get_current_user
from models.user import User

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/", response_model=list[AccountOut])
async def list_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Список всех аккаунтов пользователя"""
    return await acc_svc.get_accounts(db, current_user.id)


@router.get("/stats")
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Статистика по аккаунтам для дашборда"""
    return await acc_svc.get_stats(db, current_user.id)


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Один аккаунт по ID"""
    return await acc_svc.get_account(db, account_id, current_user.id)


@router.post("/", response_model=AccountOut, status_code=201)
async def create_account(
    data: AccountCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Добавить аккаунт (создаёт запись, авторизация через CLI)"""
    return await acc_svc.create_account(db, current_user, data.phone)


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int,
    data: AccountUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Обновить данные аккаунта (теги, роль, заметки, прокси)"""
    account = await acc_svc.get_account(db, account_id, current_user.id)
    return await acc_svc.update_account(db, account, data)


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Удалить аккаунт"""
    account = await acc_svc.get_account(db, account_id, current_user.id)
    await acc_svc.delete_account(db, account)


@router.post("/import-json")
async def import_from_json(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Импортирует аккаунты из существующего data/accounts.json (CLI-формат).
    Используется для миграции данных из CLI в API.
    """
    import json
    from pathlib import Path

    json_path = Path(__file__).parent.parent.parent / "data" / "accounts.json"
    if not json_path.exists():
        return {"detail": "accounts.json не найден", "imported": 0}

    with open(json_path, "r", encoding="utf-8") as f:
        accounts_data = json.load(f)

    imported = 0
    errors = []
    for acc_dict in accounts_data:
        try:
            await acc_svc.sync_from_dict(db, current_user, acc_dict)
            imported += 1
        except Exception as e:
            errors.append({"phone": acc_dict.get("phone"), "error": str(e)})

    return {
        "imported": imported,
        "errors": errors,
        "total": len(accounts_data)
    }
