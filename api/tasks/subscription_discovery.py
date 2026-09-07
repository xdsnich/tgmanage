"""tasks/subscription_discovery.py — сканирует существующие подписки акка.

Идёт через `iter_dialogs()`, находит каналы + discussion groups куда акк
УЖЕ подписан, и пишет в AccountSubscription. Это позволяет caller'у кампаний
не подписывать повторно на уже подписанные каналы.

Для discussion group: обычно её username пустой (это supergroup под каналом).
Мы связываем discussion с каналом через `linked_chat_id` в GetFullChannelRequest
если он есть в кэше клиента.

Целевой сценарий: перед запуском кампании #53 (2006 каналов × 151 акк) —
запустить discovery, чтобы система знала кто уже подписан на какие каналы.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime

# API-scope-friendly path setup
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

import asyncio as _asyncio
from celery_app import celery_app

def run_async(coro):
    """Синхронный wrapper для async-функции (тот же паттерн что в других tasks)."""
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            _new_loop = _asyncio.new_event_loop()
            try:
                return _new_loop.run_until_complete(coro)
            finally:
                _new_loop.close()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return _asyncio.run(coro)

logger = logging.getLogger(__name__)


async def _discover_account_subscriptions(account_id: int, campaign_id: int = None,
                                          only_campaign_channels: bool = True) -> dict:
    """Сканирует подписки одного акка. Пишет в AccountSubscription.

    only_campaign_channels: если True и campaign_id задан — записываем ТОЛЬКО те
    подписки которые пересекаются с каналами этой кампании (важно для юзерского
    сценария: интересуют только 2006 каналов из camp #53, а не все ~1000 subs акка).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.subscriptions import record_channel_join, record_discussion_join
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from telethon.tl.types import Channel, Chat

    result = {
        "account_id": account_id,
        "campaign_id": campaign_id,
        "channels_found": 0,
        "channels_recorded": 0,
        "discussions_recorded": 0,
        "errors": [],
    }

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount)
            .options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == account_id)
        )).scalar_one_or_none()
        if not acc or acc.status not in ("active", "unknown"):
            result["errors"].append(f"acc {account_id}: inactive")
            return result

        proxy = None
        if acc.proxy_id:
            proxy = (await db.execute(
                select(Proxy).where(Proxy.id == acc.proxy_id)
            )).scalar_one_or_none()

        # Список каналов кампании (если ограничиваем)
        campaign_channels_norm: set[str] = set()
        campaign_channels_by_norm: dict[str, str] = {}
        if campaign_id and only_campaign_channels:
            from models.campaign import TargetChannel
            _tc = await db.execute(
                select(TargetChannel.username).where(
                    TargetChannel.campaign_id == campaign_id,
                    TargetChannel.is_active == True,
                )
            )
            for u, in _tc.all():
                if not u:
                    continue
                nrm = u.lstrip("@").strip().lower()
                campaign_channels_norm.add(nrm)
                campaign_channels_by_norm[nrm] = u.lstrip("@").strip()

        client = make_telethon_client(acc, proxy)
        if not client:
            result["errors"].append(f"acc {account_id}: no client")
            return result

        try:
            await client.connect()
            if not await client.is_user_authorized():
                result["errors"].append(f"acc {account_id}: not authorized")
                return result

            # ── Итерируем dialogs, отбираем broadcast-каналы и supergroups
            # (supergroup может быть либо ЧАТОМ, либо discussion group под каналом).
            _all_channels: dict[int, dict] = {}     # entity_id → {username, kind}
            async for dlg in client.iter_dialogs(limit=None):
                ent = dlg.entity
                if not isinstance(ent, Channel):
                    continue
                uname = (ent.username or "").lstrip("@").strip()
                _all_channels[ent.id] = {
                    "id": ent.id,
                    "username": uname,
                    "broadcast": bool(getattr(ent, "broadcast", False)),
                    "megagroup": bool(getattr(ent, "megagroup", False)),
                    "title": ent.title or "",
                }
            result["channels_found"] = len(_all_channels)

            # ── Regular каналы: записываем прямо через username ──
            for ch in _all_channels.values():
                if not ch["broadcast"]:
                    continue
                un_norm = ch["username"].lower() if ch["username"] else ""
                if only_campaign_channels and un_norm not in campaign_channels_norm:
                    continue
                if not ch["username"]:
                    continue
                target_uname = campaign_channels_by_norm.get(un_norm, ch["username"])
                await record_channel_join(db, acc.id, target_uname,
                                          via="import", channel_id=ch["id"])
                result["channels_recorded"] += 1

            # ── Discussion groups: их username пустой, ищем через linked_chat_id
            # у каждого broadcast-канала (если акк подписан на канал И его discussion).
            from telethon.tl.functions.channels import GetFullChannelRequest
            for ch in _all_channels.values():
                if not ch["broadcast"] or not ch["username"]:
                    continue
                un_norm = ch["username"].lower()
                if only_campaign_channels and un_norm not in campaign_channels_norm:
                    continue
                try:
                    ent_ch = await client.get_entity(int(ch["id"]))
                    full = await client(GetFullChannelRequest(ent_ch))
                    linked_id = getattr(full.full_chat, "linked_chat_id", 0) or 0
                    if linked_id and linked_id in _all_channels:
                        # Акк В discussion этого канала (пришёл через iter_dialogs)
                        target_uname = campaign_channels_by_norm.get(un_norm, ch["username"])
                        await record_discussion_join(db, acc.id, target_uname, via="import")
                        result["discussions_recorded"] += 1
                except Exception as e:
                    logger.debug(f"[disc discovery] acc={acc.id} @{ch['username']}: {e}")

            await db.commit()
        except Exception as e:
            result["errors"].append(f"scan err: {type(e).__name__}: {str(e)[:120]}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    logger.info(f"[discovery] acc#{account_id} camp={campaign_id}: {result}")
    return result


@celery_app.task(bind=True, name="tasks.subscription_discovery.discover_account",
                 acks_late=False, reject_on_worker_lost=False)
def discover_account(self, account_id: int, campaign_id: int = None,
                     only_campaign_channels: bool = True):
    """Одна celery-таска на одного акка. Для массового запуска — dispatcher ниже."""
    return run_async(_discover_account_subscriptions(account_id, campaign_id, only_campaign_channels))


@celery_app.task(bind=True, name="tasks.subscription_discovery.discover_campaign",
                 acks_late=False, reject_on_worker_lost=False)
def discover_campaign(self, campaign_id: int, only_campaign_channels: bool = True):
    """Запускает discovery для всех акков кампании (по одной celery-таске на акк)."""
    async def _run():
        from sqlalchemy import select
        from utils.db_pool import async_session as Session
        from models.campaign import Campaign

        async with Session() as db:
            camp = (await db.execute(select(Campaign).where(Campaign.id == campaign_id))).scalar_one_or_none()
            if not camp:
                return {"error": "campaign not found"}
            acc_ids = camp.account_ids or []

        # Рассылаем через celery — с jitter'ом чтобы не биться друг об друга по IP
        import random as _r
        for i, a_id in enumerate(acc_ids):
            _delay = _r.randint(0, len(acc_ids) * 20)  # размазать по 20с/акк
            celery_app.send_task(
                "tasks.subscription_discovery.discover_account",
                args=[a_id, campaign_id, only_campaign_channels],
                queue="bulk_actions",
                countdown=_delay,
            )
        return {"campaign_id": campaign_id, "n_accs": len(acc_ids), "dispatched": True}

    return run_async(_run())
