"""
GramGPT API — routers/tasks.py
Эндпоинты для управления Celery задачами
WebSocket для стриминга прогресса в реальном времени
По ТЗ: /dashboard/tasks — очередь задач, прогресс, логи
"""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from celery.result import AsyncResult

from database import get_db
from routers.deps import get_current_user
from models.user import User
from celery_app import celery_app
import tasks.account_tasks as acc_tasks
import tasks.proxy_tasks as proxy_tasks
import tasks.bulk_tasks as bulk_tasks

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ============================================================
# ЗАПУСК ЗАДАЧ
# ============================================================

@router.post("/check-accounts")
async def start_check_accounts(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Запускает мультипоточную проверку аккаунтов в фоне.
    Возвращает task_id для отслеживания прогресса.
    """
    from services.accounts import get_accounts
    accounts = await get_accounts(db, current_user.id)

    if not accounts:
        raise HTTPException(status_code=400, detail="Нет аккаунтов для проверки")

    # Конвертируем в dict для передачи в Celery
    accounts_data = [
        {
            "phone": a.phone,
            "session_file": a.session_file,
            "status": a.status.value,
            "trust_score": a.trust_score,
            "tags": a.tags or [],
            "role": a.role.value,
        }
        for a in accounts
    ]

    check_spam = body.get("check_spam", False)
    task = acc_tasks.check_accounts_bulk.apply_async(
        args=[accounts_data, check_spam],
        queue="high_priority"
    )

    return {
        "task_id": task.id,
        "status": "started",
        "total": len(accounts_data),
        "message": f"Проверка {len(accounts_data)} аккаунтов запущена"
    }


@router.post("/check-proxies")
async def start_check_proxies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает проверку всех прокси в фоне"""
    from sqlalchemy import select
    from models.proxy import Proxy

    result = await db.execute(
        select(Proxy).where(Proxy.user_id == current_user.id)
    )
    proxies = result.scalars().all()

    if not proxies:
        raise HTTPException(status_code=400, detail="Нет прокси для проверки")

    proxies_data = [
        {
            "id": f"{p.host}:{p.port}",
            "host": p.host,
            "port": p.port,
            "login": p.login,
            "password": p.password,
            "protocol": p.protocol.value,
        }
        for p in proxies
    ]

    task = proxy_tasks.check_proxies_bulk.apply_async(
        args=[proxies_data],
        queue="high_priority"
    )

    return {
        "task_id": task.id,
        "status": "started",
        "total": len(proxies_data),
    }


@router.post("/bulk-update-profiles")
async def start_bulk_update_profiles(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает пакетное обновление профилей"""
    from services.accounts import get_accounts

    account_ids = body.get("account_ids")  # None = все
    accounts = await get_accounts(db, current_user.id)

    if account_ids:
        accounts = [a for a in accounts if a.id in account_ids]

    accounts_data = [
        {"phone": a.phone, "session_file": a.session_file}
        for a in accounts
    ]

    task = bulk_tasks.update_profiles_bulk.apply_async(
        args=[accounts_data],
        kwargs={
            "first_name": body.get("first_name"),
            "last_name":  body.get("last_name"),
            "bio":        body.get("bio"),
        },
        queue="bulk_actions"
    )

    return {"task_id": task.id, "status": "started", "total": len(accounts_data)}


# ============================================================
# СТАТУС ЗАДАЧИ
# ============================================================

@router.get("/{task_id}")
async def get_task_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Получить текущий статус задачи по ID"""
    result = AsyncResult(task_id, app=celery_app)

    response = {
        "task_id": task_id,
        "status":  result.status,   # PENDING | PROGRESS | SUCCESS | FAILURE
    }

    if result.status == "PROGRESS":
        response["progress"] = result.info  # dict с current/total/message

    elif result.status == "SUCCESS":
        response["result"] = result.result

    elif result.status == "FAILURE":
        response["error"] = str(result.result)

    return response


@router.delete("/{task_id}")
async def cancel_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Отменить задачу"""
    celery_app.control.revoke(task_id, terminate=True)
    return {"task_id": task_id, "status": "cancelled"}


# ============================================================
# WEBSOCKET — стриминг прогресса в реальном времени
# По ТЗ: прогресс-бар в реальном времени для долгих операций
# ============================================================

@router.websocket("/ws/{task_id}")
async def task_progress_ws(websocket: WebSocket, task_id: str):
    """
    WebSocket для отслеживания прогресса задачи.
    Подключение: ws://localhost:8000/api/v1/tasks/ws/{task_id}
    Шлёт обновления каждые 500мс пока задача не завершится.
    """
    await websocket.accept()

    try:
        while True:
            result = AsyncResult(task_id, app=celery_app)

            message = {
                "task_id": task_id,
                "status":  result.status,
            }

            if result.status == "PROGRESS":
                message["progress"] = result.info

            elif result.status == "SUCCESS":
                message["result"] = result.result
                await websocket.send_text(json.dumps(message))
                break  # Задача завершена

            elif result.status == "FAILURE":
                message["error"] = str(result.result)
                await websocket.send_text(json.dumps(message))
                break

            await websocket.send_text(json.dumps(message))
            await asyncio.sleep(0.5)  # Пул каждые 500мс

    except WebSocketDisconnect:
        pass  # Клиент отключился — ничего страшного
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"error": str(e)}))
        except Exception:
            pass
