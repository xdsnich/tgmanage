"""utils/subscriptions.py — регистрация и чтение подписок акка.

Публичные функции:
    record_channel_join(db, acc_id, channel, via, channel_id=None)
    record_discussion_join(db, acc_id, channel, via)
    get_account_subscribed_channels(db, acc_id) → set[str]
    get_account_subscribed_map(db, acc_ids) → dict[int, set[str]]

Правило "готов комментить в канале" (юзер настоял 2026-07-16):
    is_channel_joined OR is_discussion_joined == True
Такие каналы НЕ требуют join_target_channel action в новых кампаниях.
"""
from __future__ import annotations
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as _pg_insert

from models.account_subscription import AccountSubscription


def _norm(channel: str) -> str:
    """Normalize channel username: lowercase, no @, no whitespace."""
    return (channel or "").lstrip("@").strip().lower()


async def _upsert(db, acc_id: int, channel: str, *, is_channel_joined: Optional[bool] = None,
                  is_discussion_joined: Optional[bool] = None, via: str = "",
                  channel_id: Optional[int] = None) -> None:
    """UPSERT для (acc_id, channel_username). Флаги True «липнут» — раз ставим,
    не снимаем автоматически (только через ручной left/banned/re-check)."""
    ch = _norm(channel)
    if not ch:
        return
    now = datetime.utcnow()
    # Values для новой строки
    values = {
        "account_id": acc_id,
        "channel_username": ch,
        "channel_id": channel_id,
        "is_channel_joined": bool(is_channel_joined) if is_channel_joined else False,
        "is_discussion_joined": bool(is_discussion_joined) if is_discussion_joined else False,
        "discovered_via": via or "",
        "status": "active",
        "joined_at": now,
        "last_verified_at": now,
    }
    stmt = _pg_insert(AccountSubscription).values(**values)
    # ON CONFLICT: обновляем только истинные True (не сбрасываем существующие True → False)
    update_set = {"last_verified_at": now}
    if is_channel_joined:
        update_set["is_channel_joined"] = True
    if is_discussion_joined:
        update_set["is_discussion_joined"] = True
    if channel_id is not None:
        update_set["channel_id"] = channel_id
    if via:
        # discovered_via оставляем самый ранний, но сохраняем если не был установлен
        update_set["discovered_via"] = AccountSubscription.discovered_via
    stmt = stmt.on_conflict_do_update(
        index_elements=["account_id", "channel_username"],
        set_=update_set,
    )
    try:
        await db.execute(stmt)
    except Exception:
        # fail-safe: не роняем сессию из-за анти-ban учёта
        pass


async def record_channel_join(db, acc_id: int, channel: str, via: str = "",
                              channel_id: Optional[int] = None) -> None:
    """Регистрируем что акк подписан на канал (broadcast)."""
    await _upsert(db, acc_id, channel, is_channel_joined=True, via=via, channel_id=channel_id)


async def record_discussion_join(db, acc_id: int, channel: str, via: str = "") -> None:
    """Регистрируем что акк в discussion group канала.
    По правилу юзера: считается «готов комментить в канале»."""
    await _upsert(db, acc_id, channel, is_discussion_joined=True, via=via)


async def get_account_subscribed_channels(db, acc_id: int, ready_only: bool = True) -> set[str]:
    """Возвращает набор каналов на которые акк ЛИБО подписан ЛИБО в discussion.

    ready_only=True (default) — только "готов комментить" (channel OR discussion).
    """
    q = select(AccountSubscription.channel_username).where(
        AccountSubscription.account_id == acc_id,
        AccountSubscription.status == "active",
    )
    if ready_only:
        q = q.where(
            (AccountSubscription.is_channel_joined == True) |    # noqa: E712
            (AccountSubscription.is_discussion_joined == True)   # noqa: E712
        )
    r = await db.execute(q)
    return {row[0] for row in r.all()}


async def get_account_subscribed_map(db, acc_ids: Iterable[int], ready_only: bool = True) -> dict[int, set[str]]:
    """Bulk-версия — один запрос вместо N. Возвращает {acc_id: set(channels)}."""
    ids = list(acc_ids)
    if not ids:
        return {}
    q = select(AccountSubscription.account_id, AccountSubscription.channel_username).where(
        AccountSubscription.account_id.in_(ids),
        AccountSubscription.status == "active",
    )
    if ready_only:
        q = q.where(
            (AccountSubscription.is_channel_joined == True) |    # noqa: E712
            (AccountSubscription.is_discussion_joined == True)   # noqa: E712
        )
    r = await db.execute(q)
    out: dict[int, set[str]] = {aid: set() for aid in ids}
    for aid, ch in r.all():
        out.setdefault(aid, set()).add(ch)
    return out
