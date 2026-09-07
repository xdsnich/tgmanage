"""Backfill task: догоняем discussion-подписки для уже joined каналов
одной кампании. Прошёлся я по всем `CampaignChannelAssignment.status='joined'`,
резолвит linked_chat_id (с Redis-кешем), вступает если ещё не в discussion.

Мотивация: legacy accs подписались на канал ДО фикса pre-emptive discussion
join. Каждый smart_comment на такой канал первый раз ловит ChatGuestSendForbidden,
делает auto-join disc + retry. Это 900+ бесполезных первых-неудачных отправок.
Лучше один раз запустить backfill и все будут готовы.

Прогресс публикуется в Redis: `backfill:discussions:{camp_id}:progress`.
"""
from __future__ import annotations
import asyncio
import json
import os
import random
import sys
import time
import logging
from datetime import datetime

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _progress_key(camp_id: int) -> str:
    return f"backfill:discussions:{camp_id}:progress"


def _stop_key(camp_id: int) -> str:
    return f"backfill:discussions:{camp_id}:stop"


async def _do_backfill(camp_id: int):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.campaign import Campaign
    from models.campaign_channel_assignment import CampaignChannelAssignment
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.db_pool import async_session as Session
    from utils.redis_pool import get_redis
    from telethon.tl.functions.channels import (
        GetFullChannelRequest, JoinChannelRequest,
    )
    from telethon.errors import (
        FloodWaitError, UserAlreadyParticipantError,
    )

    r = get_redis()
    prog_k = _progress_key(camp_id)
    stop_k = _stop_key(camp_id)
    r.delete(stop_k)

    def _pub(status: str, **kw):
        try:
            snapshot = {
                "status": status,
                "campaign_id": camp_id,
                "ts": int(time.time()),
                **kw,
            }
            r.setex(prog_k, 24 * 3600, json.dumps(snapshot, ensure_ascii=False))
        except Exception:
            pass

    async with Session() as db:
        # 1. Все joined ассайнменты
        result = await db.execute(
            select(CampaignChannelAssignment).where(
                CampaignChannelAssignment.campaign_id == camp_id,
                CampaignChannelAssignment.status == "joined",
            )
        )
        assignments = result.scalars().all()
        total = len(assignments)
        if not assignments:
            _pub("done", total=0, done=0, joined=0, already=0, failed=0)
            return {"ok": True, "total": 0}

        # 2. Группируем по акку
        by_acc: dict[int, list[str]] = {}
        for ca in assignments:
            by_acc.setdefault(ca.account_id, []).append(ca.channel_username)

        # 3. Подгружаем акки + прокси
        acc_ids = list(by_acc.keys())
        accs_res = await db.execute(
            select(TelegramAccount)
            .options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id.in_(acc_ids))
        )
        accs = {a.id: a for a in accs_res.scalars().all()}
        proxy_ids = list({a.proxy_id for a in accs.values() if a.proxy_id})
        proxies = {}
        if proxy_ids:
            pres = await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
            for p in pres.scalars().all():
                proxies[p.id] = p

    logger.info(f"[backfill_disc c={camp_id}] {total} assignments across {len(by_acc)} accs")

    stats = {"total": total, "done": 0, "joined": 0, "already": 0,
             "no_disc": 0, "failed": 0, "flooded_accs": 0}
    _pub("running", **stats)

    async def _one_acc(acc_id: int, channels: list[str]):
        acc = accs.get(acc_id)
        if not acc:
            stats["failed"] += len(channels)
            return
        proxy = proxies.get(acc.proxy_id) if acc.proxy_id else None
        if not acquire_account_lock(acc_id, ttl=600):
            logger.info(f"[backfill_disc] acc={acc_id} busy — skip")
            stats["failed"] += len(channels)
            return
        try:
            client = make_telethon_client(acc, proxy)
            if not client:
                stats["failed"] += len(channels)
                return
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    stats["failed"] += len(channels)
                    return
                for ch in channels:
                    if r.get(stop_k):
                        return
                    clean = ch.lstrip("@").strip()
                    try:
                        # Redis-кеш linked_chat_id — избегаем повторных
                        # GetFullChannelRequest для одного и того же канала.
                        cache_k = f"gramgpt:linked_chat:{clean}"
                        link_id = None
                        try:
                            _cached = r.get(cache_k)
                            if _cached:
                                link_id = int(_cached.decode() if isinstance(_cached, bytes) else _cached)
                        except Exception:
                            pass
                        if link_id is None:
                            entity = await client.get_entity(clean)
                            full = await client(GetFullChannelRequest(entity))
                            link_id = getattr(full.full_chat, "linked_chat_id", 0) or 0
                            try:
                                r.setex(cache_k, 24 * 3600, str(link_id))
                            except Exception:
                                pass
                        if not link_id:
                            stats["no_disc"] += 1
                            stats["done"] += 1
                            continue
                        disc = await client.get_entity(link_id)
                        try:
                            await client(JoinChannelRequest(disc))
                            stats["joined"] += 1
                            logger.debug(f"[backfill_disc] acc={acc_id} @{clean} disc joined")
                        except UserAlreadyParticipantError:
                            stats["already"] += 1
                        stats["done"] += 1
                        # пауза 2-5 сек между каналами
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                    except FloodWaitError as e:
                        logger.warning(f"[backfill_disc] acc={acc_id} FloodWait {e.seconds}с — стоп акка")
                        stats["flooded_accs"] += 1
                        stats["failed"] += 1
                        return
                    except Exception as e:
                        stats["failed"] += 1
                        stats["done"] += 1
                        logger.debug(f"[backfill_disc] acc={acc_id} @{clean}: {type(e).__name__}")
                    _pub("running", **stats)
            finally:
                try: await client.disconnect()
                except Exception: pass
        finally:
            release_account_lock(acc_id)

    # Параллельно 4 акка (по одному на прокси-группу; используем semaphore)
    sem = asyncio.Semaphore(4)

    async def _capped(a, chs):
        # Слегка размазанный старт, 0-15 сек — не бомбим все прокси одновременно
        await asyncio.sleep(random.uniform(0, 15))
        async with sem:
            await _one_acc(a, chs)

    tasks = [_capped(a, chs) for a, chs in by_acc.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

    _pub("done", **stats)
    logger.info(f"[backfill_disc c={camp_id}] final: {stats}")
    return {"ok": True, **stats}


@celery_app.task(
    bind=True,
    name="tasks.backfill_discussions.backfill_discussion_joins",
    acks_late=False,
)
def backfill_discussion_joins(self, campaign_id: int):
    """Celery entrypoint: догнать discussion-подписки для всех joined
    каналов указанной кампании."""
    return _run_async(_do_backfill(campaign_id))
