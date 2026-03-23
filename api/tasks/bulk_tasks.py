"""
GramGPT API — tasks/bulk_tasks.py
Celery задачи для пакетных операций
Очередь: bulk_actions
"""

import asyncio
import sys
import os

from celery_app import celery_app

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, name="tasks.bulk_tasks.update_profiles_bulk")
def update_profiles_bulk(self, accounts: list[dict],
                         first_name: str = None,
                         last_name: str = None,
                         bio: str = None) -> dict:
    """Пакетное обновление профилей"""
    total = len(accounts)
    results = []

    for i, account in enumerate(accounts):
        phone = account.get("phone", "?")
        self.update_state(
            state="PROGRESS",
            meta={
                "current": i + 1,
                "total": total,
                "percent": int((i + 1) / total * 100),
                "message": f"[{i+1}/{total}] Обновляю профиль {phone}...",
            }
        )
        try:
            import profile_manager as pm
            ok = run_async(pm.update_profile(
                account,
                first_name=first_name,
                last_name=last_name,
                bio=bio
            ))
            results.append({"phone": phone, "success": ok})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)})

    success = sum(1 for r in results if r.get("success"))
    return {"total": total, "success": success, "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.leave_chats_bulk")
def leave_chats_bulk(self, accounts: list[dict]) -> dict:
    """Пакетный выход из всех чатов"""
    total = len(accounts)
    results = []

    for i, account in enumerate(accounts):
        phone = account.get("phone", "?")
        self.update_state(
            state="PROGRESS",
            meta={
                "current": i + 1,
                "total": total,
                "percent": int((i + 1) / total * 100),
                "message": f"[{i+1}/{total}] Выхожу из чатов {phone}...",
            }
        )
        try:
            import actions
            run_async(actions.leave_all_chats(account))
            results.append({"phone": phone, "success": True})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)})

    return {"total": total, "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.set_avatars_bulk")
def set_avatars_bulk(self, accounts: list[dict], image_path: str) -> dict:
    """Пакетная установка аватарок"""
    total = len(accounts)
    results = []

    for i, account in enumerate(accounts):
        phone = account.get("phone", "?")
        self.update_state(
            state="PROGRESS",
            meta={
                "current": i + 1,
                "total": total,
                "percent": int((i + 1) / total * 100),
                "message": f"[{i+1}/{total}] Ставлю аватарку {phone}...",
            }
        )
        try:
            import profile_manager as pm
            ok = run_async(pm.set_avatar(account, image_path))
            results.append({"phone": phone, "success": ok})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)})

    success = sum(1 for r in results if r.get("success"))
    return {"total": total, "success": success, "results": results}
