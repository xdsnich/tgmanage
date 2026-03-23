"""
GramGPT API — tasks/proxy_tasks.py
Celery задачи для работы с прокси
Очередь: high_priority
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


@celery_app.task(bind=True, name="tasks.proxy_tasks.check_proxy")
def check_proxy(self, proxy_dict: dict) -> dict:
    """Проверяет один прокси"""
    proxy_id = proxy_dict.get("id", "?")

    self.update_state(
        state="PROGRESS",
        meta={"proxy": proxy_id, "message": f"Проверяю {proxy_id}..."}
    )

    try:
        import proxy_manager as pm
        result = run_async(pm.check_proxy(proxy_dict))
        return {
            "success": True,
            "proxy_id": proxy_id,
            "is_valid": result.get("is_valid"),
            "error": result.get("error"),
        }
    except Exception as e:
        return {"success": False, "proxy_id": proxy_id, "error": str(e)}


@celery_app.task(bind=True, name="tasks.proxy_tasks.check_proxies_bulk")
def check_proxies_bulk(self, proxies: list[dict]) -> dict:
    """Мультипоточная проверка всех прокси"""
    total = len(proxies)

    self.update_state(
        state="PROGRESS",
        meta={"current": 0, "total": total, "message": "Начинаю проверку прокси..."}
    )

    try:
        import proxy_manager as pm
        results = run_async(pm.check_all(proxies))

        valid   = sum(1 for p in results if p.get("is_valid") is True)
        invalid = sum(1 for p in results if p.get("is_valid") is False)

        return {
            "total":   total,
            "valid":   valid,
            "invalid": invalid,
            "results": results,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
