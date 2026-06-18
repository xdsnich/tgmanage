"""
GramGPT API — tasks/subscribe_tasks.py
Предподготовка: подписка аккаунтов на каналы.

Оптимизация:
  - ОДНО подключение на аккаунт (не на каждую подписку)
  - Не проверяем подписан ли — просто JoinChannel
  - Если уже подписан — Telegram просто проигнорирует, делей всё равно
  - Всё через прокси
  - Рандомные паузы между подписками и между аккаунтами
"""

import asyncio
import random
import sys
import os
import logging
from datetime import datetime

from celery_app import celery_app

logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _normalize_channel(link: str) -> str:
    link = link.strip()
    if link.startswith("@"):
        return link[1:]
    if "t.me/" in link:
        return link.split("t.me/")[-1].split("/")[0].replace("@", "")
    return link


async def _run_subscribe_task(task_data: dict):
    """
    Подписка аккаунтов на каналы.
    
    Логика:
      1. Перемешиваем аккаунты
      2. Для каждого аккаунта:
         - Одно подключение через прокси
         - Перемешиваем каналы
         - Подписываемся на каждый с рандомной паузой
         - Отключаемся
      3. Большая пауза между аккаунтами
    
    Общее время ≈ total_minutes
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.subscribe_task import SubscribeTask
    from utils.telegram import make_telethon_client
    from utils.db_pool import async_session as Session

    account_ids = task_data["account_ids"]
    channels = [_normalize_channel(c) for c in task_data["channels"]]
    total_minutes = task_data.get("total_minutes", 240)
    subscribe_task_id = task_data.get("task_id")

    total_subs = len(account_ids) * len(channels)
    total_seconds = total_minutes * 60

    # Перемешиваем аккаунты
    random.shuffle(account_ids)

    # Считаем время на каждый аккаунт
    # Время внутри аккаунта + время между аккаунтами
    num_accounts = len(account_ids)
    if num_accounts == 0 or len(channels) == 0:
        return {"subscribed": 0, "failed": 0, "skipped": 0, "total": 0}

    # Время между аккаунтами (большие паузы)
    time_per_account = total_seconds / num_accounts

    logger.info(f"[subscribe] ═══ Старт: {num_accounts} акков × {len(channels)} каналов = {total_subs} подписок за ~{total_minutes} мин")

    subscribed = 0
    failed = 0
    skipped = 0
    results = []

    async with Session() as db:
        try:
            # Обновляем статус
            if subscribe_task_id:
                st_r = await db.execute(select(SubscribeTask).where(SubscribeTask.id == subscribe_task_id))
                task_row = st_r.scalar_one_or_none()
                if task_row:
                    task_row.status = "running"
                    task_row.started_at = datetime.utcnow()
                    await db.commit()

            for acc_i, acc_id in enumerate(account_ids):
                # ── Загружаем аккаунт ────────────────────
                acc_r = await db.execute(
                    select(TelegramAccount)
                    .options(joinedload(TelegramAccount.api_app))
                    .where(TelegramAccount.id == acc_id)
                )
                acc = acc_r.scalar_one_or_none()
                if not acc:
                    logger.warning(f"[subscribe] Аккаунт #{acc_id} не найден")
                    for ch in channels:
                        results.append({"account_id": acc_id, "channel": ch, "phone": "?", "ok": False, "detail": "Не найден"})
                        failed += 1
                    continue

                # ── Загружаем прокси ─────────────────────
                proxy = None
                if acc.proxy_id:
                    proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
                    proxy = proxy_r.scalar_one_or_none()

                if not proxy:
                    logger.warning(f"[subscribe] ⚠️ {acc.phone} без прокси — пропускаю")
                    for ch in channels:
                        results.append({"account_id": acc_id, "channel": ch, "phone": acc.phone, "ok": False, "detail": "Нет прокси"})
                        failed += 1
                    continue

                # ── Per-IP cooldown ──────────────────────
                # Не подключаемся если этот же IP только что использовался
                # warmup'ом/комментингом/AI-диалогом — ждём пока IP отдохнёт.
                # Subscribe не критичен по времени, можно подождать.
                from utils.ip_throttle import acquire_ip_lock, get_ip_cooldown_remaining
                if not acquire_ip_lock(proxy):
                    remaining = get_ip_cooldown_remaining(proxy)
                    logger.info(
                        f"[subscribe] {acc.phone} IP {proxy.host}:{proxy.port} "
                        f"в cooldown ({remaining}с) — пропуск"
                    )
                    for ch in channels:
                        results.append({"account_id": acc_id, "channel": ch, "phone": acc.phone,
                                        "ok": False, "detail": f"IP в cooldown ({remaining}с)"})
                        skipped += 1
                    continue

                # ── ОДНО подключение на аккаунт ──────────
                client = make_telethon_client(acc, proxy)
                if not client:
                    for ch in channels:
                        results.append({"account_id": acc_id, "channel": ch, "phone": acc.phone, "ok": False, "detail": "Нет session"})
                        failed += 1
                    continue

                logger.info(f"[subscribe] ── Аккаунт {acc.phone} ({acc_i + 1}/{num_accounts}) ──")

                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        logger.warning(f"[subscribe] {acc.phone} не авторизован")
                        for ch in channels:
                            results.append({"account_id": acc_id, "channel": ch, "phone": acc.phone, "ok": False, "detail": "Не авторизован"})
                            failed += 1
                        continue

                    # Перемешиваем каналы для этого аккаунта
                    shuffled_channels = list(channels)
                    random.shuffle(shuffled_channels)

                    from telethon.tl.functions.channels import JoinChannelRequest

                    for ch_i, channel in enumerate(shuffled_channels):
                        try:
                            entity = await client.get_entity(channel)
                            await client(JoinChannelRequest(entity))

                            results.append({"account_id": acc_id, "channel": channel, "phone": acc.phone, "ok": True, "detail": "Подписан"})
                            subscribed += 1
                            logger.info(f"[subscribe]   ✅ {acc.phone} → @{channel} ({ch_i + 1}/{len(channels)})")

                        except Exception as e:
                            err = str(e)[:150]
                            if "already" in err.lower() or "USER_ALREADY_PARTICIPANT" in err:
                                results.append({"account_id": acc_id, "channel": channel, "phone": acc.phone, "ok": True, "detail": "Уже подписан"})
                                skipped += 1
                                logger.info(f"[subscribe]   ✓ {acc.phone} уже в @{channel}")
                            elif "CHANNELS_TOO_MUCH" in err:
                                results.append({"account_id": acc_id, "channel": channel, "phone": acc.phone, "ok": False, "detail": "Лимит каналов"})
                                failed += 1
                                logger.warning(f"[subscribe]   ⛔ {acc.phone} лимит каналов — прерываю")
                                break  # Дальше бессмысленно для этого аккаунта
                            elif "FLOOD_WAIT" in err:
                                # Ждём сколько Telegram скажет
                                import re
                                wait = re.search(r"(\d+)", err)
                                wait_sec = int(wait.group(1)) if wait else 60
                                
                                # ← НОВЕ: Перериваємо акаунт, якщо бан занадто довгий (> 10 хвилин)
                                if wait_sec > 600:
                                    logger.error(f"[subscribe] 🚨 КРИТИЧНИЙ БАН на {wait_sec} секунд для {acc.phone}. Згортаємо підписку для цього акаунта.")
                                    results.append({"account_id": acc_id, "channel": channel, "phone": acc.phone, "ok": False, "detail": f"КРИТИЧНИЙ FLOOD_WAIT {wait_sec}с"})
                                    failed += 1
                                    break # Виходимо з циклу каналів для ЦЬОГО акаунта
                                    
                                logger.warning(f"[subscribe]   ⏳ {acc.phone} FLOOD_WAIT {wait_sec}с")
                                results.append({"account_id": acc_id, "channel": channel, "phone": acc.phone, "ok": False, "detail": f"FLOOD_WAIT {wait_sec}с"})
                                failed += 1
                                await asyncio.sleep(wait_sec + random.randint(5, 15)) 
                            else:
                                results.append({"account_id": acc_id, "channel": channel, "phone": acc.phone, "ok": False, "detail": err})
                                failed += 1
                                logger.warning(f"[subscribe]   ❌ {acc.phone} → @{channel}: {err}")

                        # ── Рандомная пауза между каналами ───
                        if ch_i < len(shuffled_channels) - 1:
                            # Рассчитываем паузу из оставшегося времени на аккаунт
                            remaining_channels = len(shuffled_channels) - ch_i - 1
                            remaining_time = time_per_account * 0.7  # 70% времени на подписки, 30% на паузу между акками

                            if remaining_channels > 0:
                                avg_delay = remaining_time / (len(shuffled_channels))
                            else:
                                avg_delay = 30

                            # Рандом вокруг среднего (±50%)
                            delay = int(avg_delay * random.uniform(0.3, 2.0))
                            delay = max(15, min(delay, 600))  # Минимум 15с, максимум 10мин

                            logger.info(f"[subscribe]   ⏳ Пауза {delay}с...")
                            await asyncio.sleep(delay)

                except Exception as e:
                    logger.error(f"[subscribe] Ошибка аккаунта {acc.phone}: {e}")
                    for ch in channels:
                        if not any(r.get("account_id") == acc_id and r.get("channel") == ch for r in results):
                            results.append({"account_id": acc_id, "channel": ch, "phone": acc.phone, "ok": False, "detail": str(e)[:100]})
                            failed += 1
                finally:
                    try:
                        await client.disconnect()
                        logger.info(f"[subscribe]   Отключён {acc.phone}")
                    except:
                        pass

                # ── Обновляем прогресс в БД ──────────────
                done = subscribed + failed + skipped
                progress = round(done / total_subs * 100) if total_subs > 0 else 100

                if subscribe_task_id:
                    st_r = await db.execute(select(SubscribeTask).where(SubscribeTask.id == subscribe_task_id))
                    task_row = st_r.scalar_one_or_none()
                    if task_row:
                        task_row.subscribed = subscribed
                        task_row.failed = failed
                        task_row.skipped = skipped
                        task_row.progress = progress
                        task_row.results = results[-30:]
                        await db.commit()

                # ── Большая пауза между аккаунтами ───────
                if acc_i < num_accounts - 1:
                    # 30% оставшегося времени на паузы между аккаунтами
                    between_delay = int(time_per_account * 0.3 * random.uniform(0.5, 1.5))
                    between_delay = max(30, min(between_delay, 1800))  # 30с–30мин

                    logger.info(f"[subscribe] ── Пауза между аккаунтами: {between_delay}с (~{between_delay // 60} мин) ──")
                    await asyncio.sleep(between_delay)

            # ── Финал ────────────────────────────────────
            if subscribe_task_id:
                st_r = await db.execute(select(SubscribeTask).where(SubscribeTask.id == subscribe_task_id))
                task_row = st_r.scalar_one_or_none()
                if task_row:
                    task_row.status = "done"
                    task_row.finished_at = datetime.utcnow()
                    task_row.subscribed = subscribed
                    task_row.failed = failed
                    task_row.skipped = skipped
                    task_row.progress = 100
                    task_row.results = results
                    await db.commit()

        except Exception as e:
            logger.error(f"[subscribe] Критическая ошибка: {e}")
            if subscribe_task_id:
                st_r = await db.execute(select(SubscribeTask).where(SubscribeTask.id == subscribe_task_id))
                task_row = st_r.scalar_one_or_none()
                if task_row:
                    task_row.status = "error"
                    task_row.error = str(e)[:300]
                    await db.commit()

    logger.info(f"[subscribe] ═══ Готово: ✅ {subscribed} подписано, ❌ {failed} ошибок, ✓ {skipped} уже были")

    return {
        "subscribed": subscribed,
        "failed": failed,
        "skipped": skipped,
        "total": total_subs,
    }


@celery_app.task(bind=True, name="tasks.subscribe_tasks.run_subscribe")
def run_subscribe(self, task_data: dict):
    """Celery таск — подписка в фоне."""
    self.update_state(state="PROGRESS", meta={"message": "Подписка на каналы..."})
    return run_async(_run_subscribe_task(task_data))