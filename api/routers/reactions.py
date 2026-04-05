"""
GramGPT API — routers/reactions.py
Реакции на посты И комментарии в каналах.
"""

import random
import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy
from models.reaction import ReactionTask

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reactions", tags=["reactions"])

AVAILABLE_REACTIONS = ["👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "💯", "🤣", "💔", "🏆", "😭", "😴", "😈", "🤓", "👻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍️", "🤗", "🫡", "🎅", "🎄", "☃️", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂️", "🤷", "🤷‍♀️", "😡"]


# ── Schemas ──────────────────────────────────────────────────

class ReactionCreate(BaseModel):
    channel_link: str
    post_id: Optional[int] = None
    account_ids: list[int]
    reactions: list[str] = ["👍", "🔥", "❤️"]
    mode: str = "random"
    target: str = "post"           # post | comments | both
    comments_limit: int = 5        # сколько комментов реактить
    count: int = 0
    delay_min: int = 3
    delay_max: int = 15


# ── Helpers ──────────────────────────────────────────────────

def _task_to_dict(t: ReactionTask) -> dict:
    return {
        "id": t.id,
        "channel_link": t.channel_link,
        "post_id": t.post_id,
        "account_ids": t.account_ids or [],
        "reactions": t.reactions or [],
        "mode": t.mode,
        "target": t.target or "post",
        "comments_limit": t.comments_limit or 5,
        "count": t.count,
        "delay_min": t.delay_min,
        "delay_max": t.delay_max,
        "status": t.status,
        "reactions_sent": t.reactions_sent,
        "reactions_failed": t.reactions_failed,
        "error": t.error,
        "results": t.results or [],
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        "created_at": t.created_at.isoformat(),
    }


def _normalize_channel(link: str) -> str:
    link = link.strip()
    if link.startswith("@"):
        return link[1:]
    if "t.me/" in link:
        return link.split("t.me/")[-1].split("/")[0].replace("@", "")
    return link


def _pick_emoji(reactions: list, mode: str, index: int):
    if mode == "sequential":
        return reactions[index % len(reactions)]
    elif mode == "all":
        return reactions
    else:
        return random.choice(reactions)


async def _send_reaction(client, entity, msg_id: int, emoji) -> bool:
    """Отправляет реакцию на сообщение. Возвращает True если успешно."""
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    if isinstance(emoji, list):
        reaction_list = [ReactionEmoji(emoticon=e) for e in emoji]
    else:
        reaction_list = [ReactionEmoji(emoticon=emoji)]

    await client(SendReactionRequest(
        peer=entity,
        msg_id=msg_id,
        reaction=reaction_list,
    ))
    return True


async def _get_comments(client, entity, post_id: int, limit: int) -> list:
    """Получает комментарии под постом."""
    comments = []
    try:
        async for msg in client.iter_messages(entity, reply_to=post_id, limit=limit):
            if msg.text:
                comments.append({"id": msg.id, "text": msg.text[:100], "from": msg.sender_id})
    except Exception as e:
        logger.warning(f"Не удалось получить комментарии: {e}")
    return comments


# ── Endpoints ────────────────────────────────────────────────

@router.get("/emojis")
async def list_available_reactions():
    return {"reactions": AVAILABLE_REACTIONS}


@router.get("/tasks")
async def list_reaction_tasks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ReactionTask)
        .where(ReactionTask.user_id == current_user.id)
        .order_by(ReactionTask.created_at.desc())
    )
    return [_task_to_dict(t) for t in result.scalars().all()]


@router.post("/tasks")
async def create_reaction_task(
    body: ReactionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not body.account_ids:
        raise HTTPException(status_code=400, detail="Выбери хотя бы один аккаунт")
    if not body.reactions:
        raise HTTPException(status_code=400, detail="Выбери хотя бы одну реакцию")
    if not body.channel_link:
        raise HTTPException(status_code=400, detail="Укажи канал")
    if body.target not in ("post", "comments", "both"):
        raise HTTPException(status_code=400, detail="target: post, comments или both")

    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id.in_(body.account_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )
    valid_ids = [a.id for a in acc_r.scalars().all()]
    if not valid_ids:
        raise HTTPException(status_code=400, detail="Аккаунты не найдены")

    task = ReactionTask(
        user_id=current_user.id,
        channel_link=body.channel_link.strip(),
        post_id=body.post_id,
        account_ids=valid_ids,
        reactions=body.reactions,
        mode=body.mode,
        target=body.target,
        comments_limit=max(body.comments_limit, 1),
        count=body.count if body.count > 0 else len(valid_ids),
        delay_min=max(body.delay_min, 1),
        delay_max=max(body.delay_max, body.delay_min + 1),
    )
    db.add(task)
    await db.flush()
    return _task_to_dict(task)


@router.post("/tasks/{task_id}/run")
async def run_reaction_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запустить задачу — отправить реакции на пост и/или комментарии."""
    import sys, os
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import make_telethon_client

    result = await db.execute(
        select(ReactionTask).where(ReactionTask.id == task_id, ReactionTask.user_id == current_user.id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status == "running":
        raise HTTPException(status_code=400, detail="Задача уже запущена")

    task.status = "running"
    task.started_at = datetime.utcnow()
    task.reactions_sent = 0
    task.reactions_failed = 0
    task.results = []
    task.error = None
    await db.flush()
    await db.commit()

    # Загружаем аккаунты
    acc_r = await db.execute(
        select(TelegramAccount)
        .options(joinedload(TelegramAccount.api_app))
        .where(
            TelegramAccount.id.in_(task.account_ids),
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.status == "active",
        )
    )
    accounts = acc_r.scalars().all()

    if not accounts:
        task.status = "error"
        task.error = "Нет активных аккаунтов"
        await db.flush()
        return _task_to_dict(task)

    if task.count and task.count < len(accounts):
        accounts = random.sample(accounts, task.count)

    channel = _normalize_channel(task.channel_link)
    target = task.target or "post"
    results = []
    sent = 0
    failed = 0

    for i, acc in enumerate(accounts):
        emoji = _pick_emoji(task.reactions, task.mode, i)
        emoji_str = emoji if isinstance(emoji, str) else ", ".join(emoji)

        # Загружаем прокси
        proxy = None
        if acc.proxy_id:
            proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
            proxy = proxy_r.scalar_one_or_none()

        client = make_telethon_client(acc, proxy)
        if not client:
            results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "—", "ok": False, "error": "Нет session файла"})
            failed += 1
            continue

        try:
            await client.connect()
            if not await client.is_user_authorized():
                results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "—", "ok": False, "error": "Не авторизован"})
                failed += 1
                continue

            # Получаем канал
            try:
                entity = await client.get_entity(channel)
            except Exception as e:
                results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "—", "ok": False, "error": f"Канал не найден: {str(e)[:80]}"})
                failed += 1
                continue

            # Определяем пост
            msg_id = task.post_id
            if not msg_id:
                async for msg in client.iter_messages(entity, limit=1):
                    msg_id = msg.id
                    break
            if not msg_id:
                results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "—", "ok": False, "error": "Нет постов"})
                failed += 1
                continue

            # ── Реакция на ПОСТ ──────────────────────────
            if target in ("post", "both"):
                try:
                    await _send_reaction(client, entity, msg_id, emoji)
                    results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "пост", "ok": True, "error": None})
                    sent += 1
                except Exception as e:
                    results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "пост", "ok": False, "error": str(e)[:150]})
                    failed += 1

                if target == "both":
                    await asyncio.sleep(random.randint(1, 3))

            # ── Реакции на КОММЕНТАРИИ ───────────────────
            if target in ("comments", "both"):
                try:
                    # Получаем linked discussion group
                    from telethon.tl.functions.channels import GetFullChannelRequest
                    full = await client(GetFullChannelRequest(entity))
                    linked_chat_id = getattr(full.full_chat, 'linked_chat_id', None)

                    if linked_chat_id:
                        discussion = await client.get_entity(linked_chat_id)
                    else:
                        discussion = entity

                    comments = await _get_comments(client, discussion, msg_id, task.comments_limit or 5)

                    if not comments:
                        results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "комменты", "ok": False, "error": "Нет комментариев"})
                        failed += 1
                    else:
                        comment_sent = 0
                        for ci, comment in enumerate(comments):
                            comment_emoji = _pick_emoji(task.reactions, task.mode, i + ci)
                            comment_emoji_str = comment_emoji if isinstance(comment_emoji, str) else ", ".join(comment_emoji)
                            try:
                                await _send_reaction(client, discussion, comment["id"], comment_emoji)
                                results.append({
                                    "account_id": acc.id, "phone": acc.phone,
                                    "emoji": comment_emoji_str,
                                    "target": f"коммент #{comment['id']}",
                                    "ok": True, "error": None,
                                })
                                sent += 1
                                comment_sent += 1
                                if ci < len(comments) - 1:
                                    await asyncio.sleep(random.randint(1, 4))
                            except Exception as e:
                                results.append({
                                    "account_id": acc.id, "phone": acc.phone,
                                    "emoji": comment_emoji_str,
                                    "target": f"коммент #{comment['id']}",
                                    "ok": False, "error": str(e)[:100],
                                })
                                failed += 1

                except Exception as e:
                    results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "комменты", "ok": False, "error": str(e)[:150]})
                    failed += 1

        except Exception as e:
            results.append({"account_id": acc.id, "phone": acc.phone, "emoji": emoji_str, "target": "—", "ok": False, "error": str(e)[:150]})
            failed += 1
        finally:
            try:
                await client.disconnect()
            except:
                pass

        if i < len(accounts) - 1:
            await asyncio.sleep(random.randint(task.delay_min, task.delay_max))

    task.status = "done"
    task.reactions_sent = sent
    task.reactions_failed = failed
    task.results = results
    task.finished_at = datetime.utcnow()
    await db.flush()

    return _task_to_dict(task)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_reaction_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ReactionTask).where(ReactionTask.id == task_id, ReactionTask.user_id == current_user.id)
    )
    task = result.scalar_one_or_none()
    if task:
        await db.delete(task)
        await db.flush()


class QuickReactRequest(BaseModel):
    channel_link: str
    post_id: Optional[int] = None
    account_ids: list[int]
    emoji: str = "👍"
    target: str = "post"
    comments_limit: int = 5


@router.post("/quick")
async def quick_react(
    body: QuickReactRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = ReactionTask(
        user_id=current_user.id,
        channel_link=body.channel_link.strip(),
        post_id=body.post_id,
        account_ids=body.account_ids,
        reactions=[body.emoji],
        mode="random",
        target=body.target,
        comments_limit=body.comments_limit,
        count=len(body.account_ids),
        delay_min=2,
        delay_max=8,
    )
    db.add(task)
    await db.flush()
    await db.commit()
    return await run_reaction_task(task.id, current_user, db)