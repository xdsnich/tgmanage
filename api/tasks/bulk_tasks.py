"""
GramGPT API — tasks/bulk_tasks.py
Пакетные Celery задачи. Все через make_telethon_client (с прокси).
Очередь: bulk_actions
"""

import asyncio
import json
import random
import sys
import os
import logging
import time

from celery_app import celery_app

logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _get_phone_by_acc_id(acc_id: int) -> str | None:
    """Fallback: если phone нет в Redis-плане (истёк TTL), достаём из БД.
    UI не должен видеть 'acc:63' — юзер не поймёт что это за акк."""
    from sqlalchemy import select as _sel
    from models.account import TelegramAccount
    from utils.db_pool import async_session as _Session
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    try:
        async with _Session() as db:
            r = await db.execute(_sel(TelegramAccount.phone).where(TelegramAccount.id == acc_id))
            row = r.first()
            return row[0] if row else None
    except Exception:
        return None


async def _get_client_for_phone(phone: str):
    from sqlalchemy.orm import joinedload
    """Загружает аккаунт + прокси из БД, возвращает TelegramClient."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.db_pool import async_session as Session

    async with Session() as db:
        acc_r = await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.phone == phone))
        account = acc_r.scalar_one_or_none()
        if not account:
            return None, None

        proxy = None
        if account.proxy_id:
            proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
            proxy = proxy_r.scalar_one_or_none()

        client = make_telethon_client(account, proxy)
        return client, proxy


# ── Универсальная обёртка ────────────────────────────────────

async def _run_bulk(accounts, action_fn, action_name):
    """Выполняет action_fn(client, phone) для каждого аккаунта с прокси.

    Координация с warmup/commenting:
      - acquire_account_lock — чтобы bulk не конфликтовал с execute_plan_session
        на этом же акке (два параллельных connect = revoke)
      - acquire_ip_lock — чтобы не делать N connect'ов на одном IP за секунды
        (anti-burst, как plan_executor)
      - asyncio.sleep между акками — мягкая пауза, чтобы один прокси не
        получал серию connect'ов даже если IP-lock'и истекли

    Если account_lock занят (warmup идёт) — ждём до 10 сек освобождения.
    Если IP занят — пропускаем акк, юзер увидит "IP в cooldown" в детали.
    """
    import asyncio
    import random
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock, get_ip_cooldown_remaining

    results = []
    for idx, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        acc_id = acc.get("id") or acc.get("account_id")

        # Загружаем client и proxy (нужен proxy для IP-lock)
        client, proxy = await _get_client_for_phone(phone)
        if not client:
            results.append({"phone": phone, "success": False, "error": "Аккаунт/сессия не найдены"})
            continue

        # ── Per-account lock (короткое TTL — bulk быстрый) ──
        # Если занят warmup'ом — ждём до 10 сек и пропускаем.
        acquired_acc = False
        if acc_id:
            for _ in range(10):
                if acquire_account_lock(acc_id, ttl=120):
                    acquired_acc = True
                    break
                await asyncio.sleep(1)
            if not acquired_acc:
                try: await client.disconnect()
                except: pass
                results.append({"phone": phone, "success": False,
                                "error": "Аккаунт занят (warmup/commenting) — пропуск"})
                continue

        # ── Per-IP lock — bulk не должен запускать burst на IP ──
        if proxy and not acquire_ip_lock(proxy):
            remaining = get_ip_cooldown_remaining(proxy)
            try: await client.disconnect()
            except: pass
            if acc_id and acquired_acc:
                release_account_lock(acc_id)
            results.append({"phone": phone, "success": False,
                            "error": f"IP {proxy.host}:{proxy.port} в cooldown ({remaining}с)"})
            continue

        # Мягкая пауза между акками — чтобы Telegram не видел серию
        # connect'ов на одном IP вплотную друг за другом, даже если
        # юзер кликнул bulk на пачку акков одного прокси.
        if idx > 0:
            await asyncio.sleep(random.uniform(3.0, 7.0))

        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                results.append({"phone": phone, "success": False, "error": "Сессия не активна"})
                continue

            msg = await action_fn(client, phone)
            await client.disconnect()
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            try: await client.disconnect()
            except: pass
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
        finally:
            if acc_id and acquired_acc:
                release_account_lock(acc_id)

    return results


# ── Action функции ───────────────────────────────────────────

async def _update_profile(client, phone, first_name=None, last_name=None, bio=None):
    from telethon.tl.functions.account import UpdateProfileRequest
    kwargs = {}
    if first_name is not None: kwargs['first_name'] = first_name
    if last_name is not None: kwargs['last_name'] = last_name
    if bio is not None: kwargs['about'] = bio
    if kwargs:
        await client(UpdateProfileRequest(**kwargs))
    return f"Профиль обновлён"


async def _set_avatar(client, phone, image_path):
    from telethon.tl.functions.photos import UploadProfilePhotoRequest
    if not os.path.exists(image_path):
        return "Файл аватарки не найден"
    file = await client.upload_file(image_path)
    await client(UploadProfilePhotoRequest(file=file))
    return "Аватарка установлена"


async def _leave_chats(client, phone):
    from telethon.tl.types import Chat, Channel
    from telethon.tl.functions.channels import LeaveChannelRequest
    dialogs = await client.get_dialogs()
    groups = [d for d in dialogs if isinstance(d.entity, (Chat, Channel))
              and not (isinstance(d.entity, Channel) and d.entity.broadcast)]
    left = 0
    for d in groups:
        try:
            await client(LeaveChannelRequest(d.entity))
            left += 1
            await asyncio.sleep(1)
        except: pass
    return f"Вышел из {left} чатов"


async def _leave_channels(client, phone):
    from telethon.tl.types import Channel
    from telethon.tl.functions.channels import LeaveChannelRequest
    dialogs = await client.get_dialogs()
    channels = [d for d in dialogs if isinstance(d.entity, Channel) and d.entity.broadcast]
    left = 0
    for d in channels:
        try:
            await client(LeaveChannelRequest(d.entity))
            left += 1
            await asyncio.sleep(1)
        except: pass
    return f"Отписался от {left} каналов"


async def _read_all(client, phone):
    dialogs = await client.get_dialogs()
    unread = [d for d in dialogs if d.unread_count > 0]
    read = 0
    for d in unread:
        try:
            await client.send_read_acknowledge(d.entity)
            read += 1
            await asyncio.sleep(0.3)
        except: pass
    return f"Прочитано {read} диалогов"


async def _delete_chats(client, phone):
    from telethon.tl.types import User as TgUser
    from telethon.tl.functions.messages import DeleteHistoryRequest
    dialogs = await client.get_dialogs()
    private = [d for d in dialogs if isinstance(d.entity, TgUser) and not d.entity.bot and not d.entity.is_self]
    deleted = 0
    for d in private:
        try:
            await client(DeleteHistoryRequest(peer=d.entity, max_id=0, revoke=False))
            deleted += 1
            await asyncio.sleep(0.5)
        except: pass
    return f"Удалено {deleted} переписок"


async def _unpin_folders(client, phone):
    from telethon.tl.functions.messages import UpdateDialogFilterRequest
    removed = 0
    for fid in range(2, 11):
        try:
            await client(UpdateDialogFilterRequest(id=fid))
            removed += 1
        except: pass
    return f"Удалено {removed} папок"


async def _terminate_sessions(client, phone):
    from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
    result = await client(GetAuthorizationsRequest())
    terminated = 0
    for auth in result.authorizations:
        if auth.current:
            continue
        try:
            await client(ResetAuthorizationRequest(hash=auth.hash))
            terminated += 1
            await asyncio.sleep(0.5)
        except: pass
    return f"Завершено {terminated} сессий"


async def _set_2fa(client, phone, password, hint):
    from telethon.tl.functions.account import UpdatePasswordSettingsRequest, GetPasswordRequest
    from telethon.tl.types import InputCheckPasswordEmpty
    from telethon.password import compute_check

    pwd = await client(GetPasswordRequest())
    if pwd.has_password:
        return "2FA уже установлена"

    await client(UpdatePasswordSettingsRequest(
        password=InputCheckPasswordEmpty(),
        new_settings={'new_algo': pwd.new_algo,
                      'new_password_hash': compute_check(pwd, password),
                      'hint': hint or ''}
    ))
    return "2FA установлена"


async def _create_channel(client, phone, title, description):
    from telethon.tl.functions.channels import CreateChannelRequest
    result = await client(CreateChannelRequest(title=title, about=description, broadcast=True, megagroup=False))
    ch = result.chats[0]
    link = f"https://t.me/{ch.username}" if ch.username else f"id{ch.id}"
    return {"id": ch.id, "title": title, "link": link}


# ── Celery Tasks ─────────────────────────────────────────────

@celery_app.task(bind=True, name="tasks.bulk_tasks.update_profiles_bulk")
def update_profiles_bulk(self, accounts: list[dict], first_name=None, last_name=None, bio=None):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] Профиль {phone}..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return "Не найден"
                await client.connect()
                msg = await _update_profile(client, phone, first_name, last_name, bio)
                await client.disconnect()
                return msg
            msg = run_async(do())
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.set_avatars_bulk")
def set_avatars_bulk(self, accounts: list[dict], image_path: str):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] Аватарка {phone}..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return "Не найден"
                await client.connect()
                msg = await _set_avatar(client, phone, image_path)
                await client.disconnect()
                return msg
            msg = run_async(do())
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.leave_chats_bulk")
def leave_chats_bulk(self, accounts: list[dict]):
    total = len(accounts)
    self.update_state(state="PROGRESS", meta={"current": 0, "total": total, "message": "Начинаю..."})
    results = run_async(_run_bulk(accounts, _leave_chats, "Выход из чатов"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.leave_channels_bulk")
def leave_channels_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _leave_channels, "Отписка от каналов"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.read_all_bulk")
def read_all_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _read_all, "Прочитать всё"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.delete_chats_bulk")
def delete_chats_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _delete_chats, "Удаление переписок"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.unpin_folders_bulk")
def unpin_folders_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _unpin_folders, "Открепление папок"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.set_2fa_bulk")
def set_2fa_bulk(self, accounts: list[dict], password: str, hint: str = ""):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] 2FA {phone}..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return "Не найден"
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return "Сессия не активна"
                msg = await _set_2fa(client, phone, password, hint)
                await client.disconnect()
                return msg
            msg = run_async(do())
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.terminate_sessions_bulk")
def terminate_sessions_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _terminate_sessions, "Завершение сессий"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


# ── Prepare accounts: аватарка + имя, разнесённо во времени ────────
#
# Что делает:
#   1. Опционально ставит новую аватарку каждому акку (файлы распределяются
#      РАНДОМНО, повторы разрешены если файлов меньше чем аккаунтов)
#   2. Опционально меняет first_name/last_name (пары берутся РАНДОМНО из
#      списка, порядок аккаунтов тоже перетасован)
#   3. Между акками ждёт uniform(min_delay, max_delay) секунд — юзер задаёт
#   4. На одном прокси гарантированно не бывает burst: через per-IP lock
#      (TTL = min_delay), любой акк с занятым IP пропускает свою очередь
#      и попадает в конец списка
#
# Почему без flood_wait: связка (per-IP throttle + per-account lock +
# uniform delay) гарантирует что Telegram видит редкие одиночные заходы
# на разные UIDs, а не пачку связанных операций.


_VIDEO_EXTS = {".mov", ".mp4", ".webm", ".mkv", ".avi", ".m4v", ".m4p", ".m4b"}


def _is_video_file(path: str) -> bool:
    if not path: return False
    ext = os.path.splitext(path.lower())[1]
    return ext in _VIDEO_EXTS


def _extract_video_metadata(path: str) -> dict:
    """Возвращает {'duration': int, 'width': int, 'height': int} из
    hachoir-parser'а. Если хоть что-то не удалось — вернёт заглушки."""
    result = {"duration": 0, "width": 640, "height": 480}
    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata
        parser = createParser(path)
        if not parser:
            return result
        with parser:
            meta = extractMetadata(parser)
            if not meta:
                return result
            if meta.has("duration"):
                try:
                    result["duration"] = int(meta.get("duration").total_seconds())
                except Exception:
                    pass
            if meta.has("width"):
                try:
                    result["width"] = int(meta.get("width"))
                except Exception:
                    pass
            if meta.has("height"):
                try:
                    result["height"] = int(meta.get("height"))
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"[_extract_video_metadata] {path}: {e}")
    return result


def _make_video_thumbnail(path: str, timeout_sec: int = 8) -> str | None:
    """Через ffmpeg вытаскивает первый кадр в JPEG. Возвращает путь к thumb
    или None если ffmpeg недоступен/упал."""
    try:
        import subprocess, tempfile, shutil, uuid
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        # UUID чтобы параллельные воркеры не переписали и не удалили
        # thumb друг друга (гонка → FileNotFoundError в send_file).
        thumb_path = os.path.join(
            tempfile.gettempdir(),
            f"tgprep_thumb_{os.getpid()}_{uuid.uuid4().hex}.jpg",
        )
        # -ss 0.5 = 500ms в файл (первые кадры часто чёрные), scale 320px по большей стороне
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-ss", "0.5", "-i", path,
             "-vframes", "1",
             "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
             "-q:v", "5", thumb_path],
            timeout=timeout_sec, check=True,
        )
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.debug(f"[_make_video_thumbnail] {path}: {e}")
    return None


async def _send_media_smart(client, entity, path: str, caption: str = "") -> None:
    """send_file с корректными video-атрибутами и thumbnail для видео.

    Причина: Telethon для .mov без hachoir отправляет файл как документ,
    без duration/width/height/thumb → Telegram рендерит крохотный серый
    квадрат. С hachoir + явными attributes + thumb — как нормальное видео.
    """
    from telethon.tl.types import DocumentAttributeVideo
    if not _is_video_file(path):
        # Фото / гиф / другое — send_file сам разберётся.
        await client.send_file(entity=entity, file=path, caption=caption)
        return

    meta = _extract_video_metadata(path)
    thumb = _make_video_thumbnail(path)
    attributes = [DocumentAttributeVideo(
        duration=meta["duration"],
        w=meta["width"],
        h=meta["height"],
        supports_streaming=True,
    )]
    try:
        await client.send_file(
            entity=entity, file=path,
            caption=caption,
            supports_streaming=True,
            attributes=attributes,
            thumb=thumb,           # None = Telethon сам попробует
        )
    finally:
        # Чистим временный thumbnail
        if thumb and os.path.exists(thumb):
            try: os.unlink(thumb)
            except Exception: pass


async def _create_channel_step_by_step(
    client, phone: str,
    title: str,
    description: str = "",
    avatar_path: str | None = None,
    first_post: str = "",
    first_post_photo: str | None = None,
    pin_to_profile: bool = True,
    reconnect_pause_sec: int = 2,
    public_usernames: list[str] | None = None,
) -> dict:
    """
    Создаёт канал ПОЭТАПНО (порт логики /channels/create-full).

    Этапы (внутри одного захода на клиент):
      1. CreateChannelRequest — title + description
      2. sleep 1-2с — микропауза перед следующим request'ом
      3. EditPhoto (если avatar_path)
      4. sleep 1-2с
      5. ToggleDialogPin в профиль (если pin_to_profile)
      6. disconnect → sleep(reconnect_pause_sec=2) → reconnect
         — Telegram видит это как «юзер закрыл и открыл клиент»
         — иначе первый пост на свежем канале режется флуд-фильтром
      7. send_message (первый пост, если задан)

    Возвращает dict с флагами какой этап удался.
    """
    import random as _r
    from telethon.tl.functions.channels import (
        CreateChannelRequest as TgCreateChannel,
        EditPhotoRequest,
    )
    from telethon.tl.types import InputChatUploadedPhoto

    result = {
        "created": False, "avatar_set": False, "pinned": False,
        "first_post": False, "channel_id": None, "channel_title": title,
    }

    # 1. Создание
    resp = await client(TgCreateChannel(
        title=title, about=description, broadcast=True, megagroup=False,
    ))
    channel = resp.chats[0]
    result["created"] = True
    result["channel_id"] = channel.id
    result["public_username"] = None
    await asyncio.sleep(_r.uniform(1.0, 2.5))

    # 1.5. Публичный username (если задан пул).
    # Пробуем базовые кандидаты, при UsernameOccupied — с числовым суффиксом.
    if public_usernames:
        from telethon.tl.functions.channels import UpdateUsernameRequest
        from telethon import errors as _terrs
        expanded = []
        for base in public_usernames:
            base = (base or "").lstrip("@").strip()
            if not base:
                continue
            expanded.append(base)
            for _ in range(3):
                expanded.append(f"{base}{_r.randint(10, 9999)}")
        set_username = None
        for uname in expanded:
            if not (5 <= len(uname) <= 32) or not uname[0].isalpha():
                continue
            try:
                await client(UpdateUsernameRequest(channel=channel, username=uname))
                set_username = uname
                break
            except (_terrs.UsernameOccupiedError, _terrs.UsernameInvalidError,
                    _terrs.UsernamePurchaseAvailableError):
                continue
            except Exception as e:
                logger.warning(f"[create_channel] {phone} username '{uname}': {e}")
                continue
        if set_username:
            result["public_username"] = set_username
            await asyncio.sleep(_r.uniform(1.0, 2.0))

    # 2. Аватар канала
    if avatar_path and os.path.exists(avatar_path):
        try:
            file_handle = await client.upload_file(avatar_path)
            await client(EditPhotoRequest(
                channel=channel,
                photo=InputChatUploadedPhoto(file=file_handle),
            ))
            result["avatar_set"] = True
            await asyncio.sleep(_r.uniform(1.0, 2.5))
        except Exception as e:
            logger.warning(f"[create_channel] {phone} avatar: {e}")

    # 3. Пин в профиль
    if pin_to_profile:
        try:
            from telethon.tl.functions.messages import ToggleDialogPinRequest
            from telethon.tl.types import InputDialogPeer, InputPeerChannel
            peer = InputPeerChannel(channel_id=channel.id, access_hash=channel.access_hash)
            await client(ToggleDialogPinRequest(
                peer=InputDialogPeer(peer=peer), pinned=True,
            ))
            result["pinned"] = True
        except Exception as e:
            logger.warning(f"[create_channel] {phone} pin: {e}")

    # 4. Первый пост — с disconnect/reconnect
    has_text = bool(first_post and first_post.strip())
    has_photo = bool(first_post_photo and os.path.exists(first_post_photo))
    if has_text or has_photo:
        pause = max(0, int(reconnect_pause_sec))
        try:
            await client.disconnect()
        except Exception:
            pass
        if pause:
            await asyncio.sleep(pause + _r.uniform(0.0, 1.5))
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except Exception as e:
            logger.warning(f"[create_channel] {phone} reconnect: {e}")

        try:
            if has_photo:
                # _send_media_smart — с hachoir metadata и ffmpeg thumbnail
                # для видео, иначе .mov приходит серым квадратом
                await _send_media_smart(
                    client, channel, first_post_photo,
                    caption=first_post if has_text else "",
                )
            else:
                await client.send_message(entity=channel, message=first_post)
            result["first_post"] = True
        except Exception as e:
            logger.warning(f"[create_channel] {phone} first_post: {e}")

    return result


async def _prepare_one(
    client, phone,
    avatar_path=None, first_name=None, last_name=None,
    channel_cfg: dict | None = None,
    delete_previous_profile_avatar: bool = False,
):
    """Опциональные операции: имя → аватарка → канал (поэтапно). Между
    каждым шагом микропауза 1.5-4 сек. Всё на одном коннекте.

    delete_previous_profile_avatar: перед uploadом новой аватарки удалить
        все существующие фото профиля. Пауза 2-5 сек между удалением и upload.
    """
    import random as _r
    changes = []
    if first_name is not None or last_name is not None:
        kwargs = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
        if last_name is not None:
            kwargs["last_name"] = last_name
        if kwargs:
            from telethon.tl.functions.account import UpdateProfileRequest
            await client(UpdateProfileRequest(**kwargs))
            changes.append(f"name={first_name or ''} {last_name or ''}".strip())
            # Небольшая пауза внутри одного захода — чтобы не два request'а
            # подряд миллисекунда в миллисекунду. Всё равно уже подключились.
            await asyncio.sleep(_r.uniform(1.5, 4.0))
    if avatar_path:
        if not os.path.exists(avatar_path):
            changes.append(f"avatar=missing:{avatar_path}")
        else:
            from telethon.tl.functions.photos import UploadProfilePhotoRequest
            deleted_prev = 0
            if delete_previous_profile_avatar:
                try:
                    from telethon.tl.functions.photos import (
                        GetUserPhotosRequest, DeletePhotosRequest,
                    )
                    from telethon.tl.types import InputPhoto
                    resp = await client(GetUserPhotosRequest(
                        user_id="me", offset=0, max_id=0, limit=100,
                    ))
                    inputs = []
                    for p in (resp.photos or []):
                        if hasattr(p, "id") and hasattr(p, "access_hash") and hasattr(p, "file_reference"):
                            inputs.append(InputPhoto(
                                id=p.id, access_hash=p.access_hash,
                                file_reference=p.file_reference,
                            ))
                    if inputs:
                        await client(DeletePhotosRequest(id=inputs))
                        deleted_prev = len(inputs)
                    await asyncio.sleep(_r.uniform(2.0, 5.0))
                except Exception as e:
                    logger.warning(f"[delete_prev_profile_photo] {phone}: {e}")
            file = await client.upload_file(avatar_path)
            await client(UploadProfilePhotoRequest(file=file))
            changes.append(f"avatar=set{f' (del {deleted_prev})' if deleted_prev else ''}")
            # Пауза перед созданием канала — не два тяжёлых upload подряд
            await asyncio.sleep(_r.uniform(2.0, 5.0))
    if channel_cfg and channel_cfg.get("enabled"):
        try:
            ch_result = await _create_channel_step_by_step(
                client, phone,
                title=channel_cfg.get("title") or "Channel",
                description=channel_cfg.get("description") or "",
                avatar_path=channel_cfg.get("avatar_path"),
                first_post=channel_cfg.get("first_post") or "",
                first_post_photo=channel_cfg.get("first_post_photo"),
                pin_to_profile=bool(channel_cfg.get("pin_to_profile", True)),
                reconnect_pause_sec=int(channel_cfg.get("reconnect_pause_sec", 2)),
                public_usernames=channel_cfg.get("public_usernames") or None,
            )
            parts = []
            if ch_result.get("created"):
                parts.append(f"channel='{ch_result.get('channel_title')}'")
            if ch_result.get("public_username"): parts.append(f"@{ch_result['public_username']}")
            if ch_result.get("avatar_set"): parts.append("ch_avatar")
            if ch_result.get("pinned"): parts.append("pinned")
            if ch_result.get("first_post"): parts.append("post")
            if parts:
                changes.append("|".join(parts))
        except Exception as e:
            changes.append(f"channel_err={type(e).__name__}: {str(e)[:80]}")
    return "; ".join(changes) if changes else "no-op"


def _publish_prepare_progress(redis, key: str, payload: dict, ttl: int = 86400):
    import json as _json
    try:
        redis.setex(key, ttl, _json.dumps(payload, ensure_ascii=False))
    except Exception as _e:
        logger.debug(f"[prepare_bulk] redis setex fail: {_e}")


@celery_app.task(bind=True, name="tasks.bulk_tasks.prepare_accounts_bulk")
def prepare_accounts_bulk(
    self,
    accounts: list[dict],
    job_id: str,
    avatar_paths: list[str] | None = None,
    names: list[dict] | None = None,     # [{"first_name": "...", "last_name": "..."}]
    min_delay_sec: int = 60,
    max_delay_sec: int = 180,
    channel_cfg: dict | None = None,     # {enabled, titles[], description, first_post,
                                         #  avatar_paths[], pin, reconnect_pause_sec}
    delete_previous_profile_avatar: bool = False,
):
    """Подготовка пачки аккаунтов: аватарки + имена + создание канала,
    разнесённо во времени.

    Args:
        accounts: [{"phone": "...", "id": ...}, ...]
        job_id: UUID для отслеживания прогресса в Redis
        avatar_paths: список путей к загруженным фото профиля. Пусто → пропуск.
        names: список dict {first_name, last_name}. Пусто → пропуск.
        min_delay_sec / max_delay_sec: пауза между акками (uniform)
        channel_cfg: конфиг создания канала:
            {
              "enabled": True,
              "titles": ["Название 1", "Название 2", ...],  # рандом на акк
              "description": "общее описание",
              "first_post": "текст первого поста",
              "avatar_paths": ["/tmp/ch_ava_0.jpg", ...],   # рандом на акк
              "pin": True,
              "reconnect_pause_sec": 2,
            }
            Если channel_cfg["enabled"] == False или отсутствует — не создаём.

    Все аккаунты и распределения РАНДОМИЗИРУЮТСЯ. Файл может быть присвоен
    нескольким аккаунтам если total_accounts > total_files (и наоборот —
    некоторые файлы могут не пригодиться если total_files > total_accounts).
    """
    import random as _r
    import json as _json

    total = len(accounts)
    avatar_paths = list(avatar_paths or [])
    names = list(names or [])
    channel_cfg = channel_cfg or {}
    change_avatars = bool(avatar_paths)
    change_names = bool(names)
    create_channels = bool(channel_cfg.get("enabled"))

    if not change_avatars and not change_names and not create_channels:
        return {"total": total, "success": 0, "results": [],
                "error": "Ни аватарок, ни имён, ни каналов — нечего делать"}

    # ── Redis для прогресса и IP-lock ─────────────────────────
    from utils.redis_pool import get_redis
    from utils.ip_throttle import acquire_ip_lock, get_ip_cooldown_remaining
    from utils.account_lock import acquire_account_lock, release_account_lock
    redis = get_redis()
    prog_key = f"bulk:prepare:{job_id}:progress"

    # ── Рандомизация: перемешиваем акки И распределяем ресурсы ─
    accounts_shuffled = list(accounts)
    _r.shuffle(accounts_shuffled)

    # Аватарки: каждому акку — случайный из списка (с повторами если нужно)
    def pick_avatar():
        return _r.choice(avatar_paths) if change_avatars else None

    # Имена: каждому акку — случайное из списка
    def pick_name():
        return _r.choice(names) if change_names else {}

    # Конфиг канала для конкретного акка. Все ресурсы рандомизируются:
    #   title, description, avatar, first-post photo — по независимому пулу
    ch_titles = list(channel_cfg.get("titles") or [])
    # descriptions[] новый формат, fallback на legacy single-line description
    ch_descriptions = list(channel_cfg.get("descriptions") or [])
    if not ch_descriptions and channel_cfg.get("description"):
        ch_descriptions = [channel_cfg["description"]]
    ch_avatars = list(channel_cfg.get("avatar_paths") or [])
    ch_post_photos = list(channel_cfg.get("post_photo_paths") or [])
    ch_public_usernames = list(channel_cfg.get("public_usernames") or [])
    ch_public = bool(channel_cfg.get("public")) and bool(ch_public_usernames)
    # {acc_id_str: {"text": "...", "photo_path": "..." | None}}
    custom_posts_by_acc = dict(channel_cfg.get("custom_posts_by_acc") or {})
    def pick_channel_cfg(acc_id: int | None = None):
        if not create_channels:
            return None
        # Приоритет: если для этого акка юзер задал явный пост — используем его.
        # Иначе общий first_post + рандом из общего пула фото.
        custom = custom_posts_by_acc.get(str(acc_id)) if acc_id is not None else None
        if custom:
            first_post_text = custom.get("text") or ""
            first_post_photo = custom.get("photo_path")
        else:
            first_post_text = channel_cfg.get("first_post", "")
            first_post_photo = _r.choice(ch_post_photos) if ch_post_photos else None
        # Для публичного канала — акку даём один случайный username
        # (в step_by_step он попробует его + суффиксные fallback'и).
        # Даём кандидат "по остаточному принципу" — рандом из пула.
        acc_public = None
        if ch_public and ch_public_usernames:
            acc_public = [_r.choice(ch_public_usernames)]
        return {
            "enabled": True,
            "title": _r.choice(ch_titles) if ch_titles else "Мой канал",
            "description": _r.choice(ch_descriptions) if ch_descriptions else "",
            "first_post": first_post_text,
            "avatar_path": _r.choice(ch_avatars) if ch_avatars else None,
            "first_post_photo": first_post_photo,
            "public_usernames": acc_public,
            "pin_to_profile": bool(channel_cfg.get("pin", True)),
            "reconnect_pause_sec": int(channel_cfg.get("reconnect_pause_sec", 2)),
        }

    # ── Initial progress ─────────────────────────────────────
    _publish_prepare_progress(redis, prog_key, {
        "status": "running", "job_id": job_id,
        "total": total, "done": 0, "ok": 0, "failed": 0,
        "current_phone": "", "results": [],
        "ts": int(time.time()),
    })

    async def run():
        results = []
        for idx, acc in enumerate(accounts_shuffled):
            phone = acc.get("phone", "?")
            acc_id = acc.get("id") or acc.get("account_id")

            # ── Пауза перед следующим акком (кроме первого) ──
            if idx > 0:
                pause = _r.uniform(float(min_delay_sec), float(max_delay_sec))
                # публикуем что «отдыхаем» — UI видит счётчик
                _publish_prepare_progress(redis, prog_key, {
                    "status": "running", "job_id": job_id,
                    "total": total, "done": idx,
                    "ok": sum(1 for r in results if r.get("success")),
                    "failed": sum(1 for r in results if not r.get("success")),
                    "current_phone": phone,
                    "sleeping_sec": int(pause),
                    "results": results[-20:],
                    "ts": int(time.time()),
                })
                logger.info(
                    f"[prepare_bulk] {job_id} pause {pause:.0f}с перед {phone} "
                    f"({idx}/{total})"
                )
                await asyncio.sleep(pause)

            # ── Загрузка клиента ─────────────────────────────
            client, proxy = await _get_client_for_phone(phone)
            if not client:
                results.append({"phone": phone, "success": False,
                                "error": "Аккаунт/сессия не найдены"})
                continue

            # ── Account lock (warmup не должен пересекаться) ──
            acquired_acc = False
            if acc_id:
                for _ in range(10):
                    if acquire_account_lock(acc_id, ttl=300):
                        acquired_acc = True
                        break
                    await asyncio.sleep(1)
                if not acquired_acc:
                    try: await client.disconnect()
                    except: pass
                    results.append({"phone": phone, "success": False,
                                    "error": "Занят warmup'ом — пропуск"})
                    continue

            # ── IP throttle (TTL = min_delay чтобы не жарить один прокси) ──
            if proxy and not acquire_ip_lock(proxy, ttl=int(min_delay_sec)):
                remaining = get_ip_cooldown_remaining(proxy)
                try: await client.disconnect()
                except: pass
                if acc_id and acquired_acc:
                    release_account_lock(acc_id)
                # Прокси занят — эту итерацию пропускаем, добавим акк в конец
                accounts_shuffled.append(acc)  # даём второй шанс
                results.append({"phone": phone, "success": False,
                                "error": f"IP {proxy.host} в cooldown {remaining}с — отложено"})
                continue

            avatar = pick_avatar()
            name = pick_name()
            first = name.get("first_name") if isinstance(name, dict) else None
            last = name.get("last_name") if isinstance(name, dict) else None
            ch_cfg_for_acc = pick_channel_cfg(acc_id=acc_id)

            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    results.append({"phone": phone, "success": False,
                                    "error": "Сессия не активна"})
                    continue
                msg = await _prepare_one(
                    client, phone, avatar, first, last,
                    channel_cfg=ch_cfg_for_acc,
                    delete_previous_profile_avatar=bool(delete_previous_profile_avatar),
                )
                await client.disconnect()
                results.append({
                    "phone": phone, "success": True, "message": msg,
                    "first_name": first, "last_name": last,
                    "avatar": os.path.basename(avatar) if avatar else None,
                    "channel_title": ch_cfg_for_acc.get("title") if ch_cfg_for_acc else None,
                })
            except Exception as e:
                try: await client.disconnect()
                except: pass
                results.append({"phone": phone, "success": False,
                                "error": str(e)[:200]})
            finally:
                if acc_id and acquired_acc:
                    release_account_lock(acc_id)

            # публикуем прогресс после каждого акка
            _publish_prepare_progress(redis, prog_key, {
                "status": "running", "job_id": job_id,
                "total": total, "done": idx + 1,
                "ok": sum(1 for r in results if r.get("success")),
                "failed": sum(1 for r in results if not r.get("success")),
                "current_phone": phone,
                "results": results[-20:],
                "ts": int(time.time()),
            })

        return results

    results = run_async(run())

    final = {
        "status": "done", "job_id": job_id,
        "total": total,
        "done": len(results),
        "ok": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
        "results": results,
        "ts": int(time.time()),
    }
    _publish_prepare_progress(redis, prog_key, final, ttl=7 * 86400)
    return final


# ══════════════════════════════════════════════════════════════
# PREP PLAN — независимые планы по акку с сессиями во времени
# и параллелизмом через Celery.
# ══════════════════════════════════════════════════════════════
#
# Архитектура:
#   1. Роутер вызывает prep_plan_start_bulk — генерит планы для всех
#      акков и сохраняет в Redis. Для каждой сессии каждого плана
#      делает send_task(execute_prep_session, countdown=connect_ts-now).
#   2. Executor'ы срабатывают в свои моменты, параллельно на разных
#      акках через воркеры Celery. Внутри — ip_lock/account_lock как
#      обычно. Один прокси не получает burst.
#   3. Progress собирается в Redis по мере выполнения сессий, UI поллит.


async def _execute_action(client, phone: str, action: dict, ctx: dict) -> dict:
    """Один action в рамках сессии. ctx — общий for the whole plan
    (например, channel_id/access_hash — чтоб пост попал в тот канал
    который создали в прошлой сессии)."""
    import random as _r
    typ = action.get("type")
    if typ == "change_profile_name":
        from telethon.tl.functions.account import UpdateProfileRequest
        kwargs = {}
        if action.get("first_name") is not None:
            kwargs["first_name"] = action["first_name"]
        if action.get("last_name") is not None:
            kwargs["last_name"] = action["last_name"]
        if kwargs:
            await client(UpdateProfileRequest(**kwargs))
        return {"ok": True, "detail": f"name={kwargs.get('first_name','')} {kwargs.get('last_name','')}".strip()}

    if typ == "set_profile_avatar":
        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        path = action.get("avatar_path")
        if not path or not os.path.exists(path):
            return {"ok": False, "detail": f"avatar file missing: {path}"}
        # Опционально: удалить все предыдущие фотки профиля.
        # ВНУТРИ одной сессии (без reconnect) с рандомной паузой 2-5 сек.
        deleted_prev = 0
        if action.get("delete_previous"):
            try:
                from telethon.tl.functions.photos import (
                    GetUserPhotosRequest, DeletePhotosRequest,
                )
                from telethon.tl.types import InputPhoto
                resp = await client(GetUserPhotosRequest(
                    user_id="me", offset=0, max_id=0, limit=100,
                ))
                inputs = []
                for p in (resp.photos or []):
                    if hasattr(p, "id") and hasattr(p, "access_hash") and hasattr(p, "file_reference"):
                        inputs.append(InputPhoto(
                            id=p.id, access_hash=p.access_hash,
                            file_reference=p.file_reference,
                        ))
                if inputs:
                    await client(DeletePhotosRequest(id=inputs))
                    deleted_prev = len(inputs)
                # Пауза между удалением и загрузкой новой
                await asyncio.sleep(_r.uniform(2.0, 5.0))
            except Exception as e:
                logger.warning(f"[delete_prev_profile_photo] {phone}: {e}")
        file = await client.upload_file(path)
        await client(UploadProfilePhotoRequest(file=file))
        detail = f"profile avatar → {os.path.basename(path)}"
        if deleted_prev:
            detail += f" (удалено старых: {deleted_prev})"
        return {"ok": True, "detail": detail}

    if typ == "create_channel":
        from telethon.tl.functions.channels import CreateChannelRequest as TgCreateChannel
        resp = await client(TgCreateChannel(
            title=action.get("title", "Channel"),
            about=action.get("description", ""),
            broadcast=True, megagroup=False,
        ))
        ch = resp.chats[0]
        ctx["channel_id"] = ch.id
        ctx["channel_access_hash"] = ch.access_hash
        ctx["channel_title"] = action.get("title")
        return {"ok": True, "detail": f"channel '{action.get('title')}' id={ch.id}"}

    if typ == "set_channel_username":
        # Делает канал ПУБЛИЧНЫМ через UpdateUsernameRequest.
        # Requires ctx.channel_id/access_hash (create_channel в этой же сессии).
        # username_candidates — список кандидатов; пробуем по порядку,
        # при UsernameOccupied — генерим 3 варианта с случайным суффиксом,
        # чтобы избежать конфликтов если один пользователь пытается забрать
        # то же самое имя для нескольких акков.
        from telethon.tl.functions.channels import UpdateUsernameRequest
        from telethon.tl.types import InputChannel
        from telethon import errors as _terrs
        ch_id = ctx.get("channel_id")
        ch_hash = ctx.get("channel_access_hash")
        if not ch_id:
            return {"ok": False, "detail": "no channel_id in ctx"}
        # Формат: либо одна строка (кандидат), либо список
        raw = action.get("username_candidates") or action.get("username")
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, list):
            candidates = list(raw)
        else:
            candidates = []
        if not candidates:
            return {"ok": False, "detail": "нет кандидатов на username"}
        ch_input = InputChannel(channel_id=ch_id, access_hash=ch_hash)
        # Расширяем каждый кандидат fallback'ами с суффиксом (число 10-9999)
        expanded = []
        for base in candidates:
            base = (base or "").lstrip("@").strip()
            if not base:
                continue
            expanded.append(base)
            for _ in range(3):
                expanded.append(f"{base}{_r.randint(10, 9999)}")
        set_username = None
        last_err = None
        for uname in expanded:
            if not (5 <= len(uname) <= 32):
                last_err = f"'{uname}': длина должна быть 5-32"
                continue
            if not uname[0].isalpha():
                last_err = f"'{uname}': должно начинаться с буквы"
                continue
            try:
                await client(UpdateUsernameRequest(channel=ch_input, username=uname))
                set_username = uname
                break
            except _terrs.UsernameOccupiedError:
                last_err = f"'{uname}': занят"
                continue
            except _terrs.UsernameInvalidError:
                last_err = f"'{uname}': не валиден для Telegram"
                continue
            except _terrs.UsernamePurchaseAvailableError:
                last_err = f"'{uname}': продаётся, купить сначала"
                continue
            except Exception as e:
                last_err = f"'{uname}': {type(e).__name__}: {str(e)[:80]}"
                continue
        if set_username:
            # Сохраняем в ctx чтобы потом (пост) видеть публичный статус
            ctx["channel_username"] = set_username
            return {"ok": True, "detail": f"public: @{set_username}"}
        return {"ok": False, "detail": f"все кандидаты не подошли (last: {last_err})"}

    if typ == "set_channel_avatar":
        # Требует что был create_channel в этой же сессии ИЛИ в предыдущей
        # (мы сохранили channel_id в ctx).
        from telethon.tl.functions.channels import EditPhotoRequest
        from telethon.tl.types import (
            InputChatUploadedPhoto, InputChannel, InputChatPhotoEmpty,
        )
        ch_id = ctx.get("channel_id")
        ch_hash = ctx.get("channel_access_hash")
        if not ch_id:
            return {"ok": False, "detail": "no channel_id in ctx"}
        path = action.get("avatar_path")
        if not path or not os.path.exists(path):
            return {"ok": False, "detail": f"channel avatar missing: {path}"}
        ch_input = InputChannel(channel_id=ch_id, access_hash=ch_hash)
        # Опционально: удалить существующую аватарку канала.
        # InputChatPhotoEmpty = «нет фото». Пауза перед загрузкой новой.
        cleared_prev = False
        if action.get("delete_previous"):
            try:
                await client(EditPhotoRequest(
                    channel=ch_input, photo=InputChatPhotoEmpty(),
                ))
                cleared_prev = True
                await asyncio.sleep(_r.uniform(2.0, 5.0))
            except Exception as e:
                logger.warning(f"[delete_prev_channel_photo]: {e}")
        file_handle = await client.upload_file(path)
        await client(EditPhotoRequest(
            channel=ch_input,
            photo=InputChatUploadedPhoto(file=file_handle),
        ))
        detail = f"channel avatar → {os.path.basename(path)}"
        if cleared_prev:
            detail += " (старая удалена)"
        return {"ok": True, "detail": detail}

    if typ == "pin_channel_to_profile":
        from telethon.tl.functions.messages import ToggleDialogPinRequest
        from telethon.tl.types import InputDialogPeer, InputPeerChannel
        ch_id = ctx.get("channel_id")
        ch_hash = ctx.get("channel_access_hash")
        if not ch_id:
            return {"ok": False, "detail": "no channel_id in ctx"}
        peer = InputPeerChannel(channel_id=ch_id, access_hash=ch_hash)
        await client(ToggleDialogPinRequest(
            peer=InputDialogPeer(peer=peer), pinned=True,
        ))
        return {"ok": True, "detail": "pinned to profile"}

    if typ == "post_first_message":
        from telethon.tl.types import InputPeerChannel
        ch_id = ctx.get("channel_id")
        ch_hash = ctx.get("channel_access_hash")
        if not ch_id:
            return {"ok": False, "detail": "no channel_id in ctx (create_channel не выполнен)"}
        # Ключ: этот action ДОЛЖЕН быть в отдельной сессии от create_channel.
        # Значит фактический disconnect уже был между create и этим action'ом.
        entity = InputPeerChannel(channel_id=ch_id, access_hash=ch_hash)
        text = action.get("text", "")
        photo = action.get("photo_path")
        if photo and os.path.exists(photo):
            # _send_media_smart корректно обрабатывает видео (metadata + thumb)
            await _send_media_smart(client, entity, photo, caption=text or "")
            kind = "video" if _is_video_file(photo) else "photo"
            return {"ok": True, "detail": f"first post + {kind} → {os.path.basename(photo)}"}
        if not text.strip():
            return {"ok": False, "detail": "empty post (no text and no photo)"}
        await client.send_message(entity=entity, message=text)
        return {"ok": True, "detail": "first post published"}

    return {"ok": False, "detail": f"unknown action type: {typ}"}


def _prep_plan_key(job_id: str, acc_id: int) -> str:
    return f"prep_plan:{job_id}:acc:{acc_id}"


def _prep_plan_meta_key(job_id: str) -> str:
    return f"prep_plan:{job_id}:meta"


def _prep_plan_ctx_key(job_id: str, acc_id: int) -> str:
    """Между-сессионный контекст акка (channel_id и т.д.)."""
    return f"prep_plan:{job_id}:ctx:{acc_id}"


def _load_plan(redis, job_id: str, acc_id: int) -> dict | None:
    raw = redis.get(_prep_plan_key(job_id, acc_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_plan(redis, job_id: str, acc_id: int, plan: dict):
    redis.setex(_prep_plan_key(job_id, acc_id), 7 * 86400,
                json.dumps(plan, ensure_ascii=False))


def _load_ctx(redis, job_id: str, acc_id: int) -> dict:
    raw = redis.get(_prep_plan_ctx_key(job_id, acc_id))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_ctx(redis, job_id: str, acc_id: int, ctx: dict):
    redis.setex(_prep_plan_ctx_key(job_id, acc_id), 7 * 86400,
                json.dumps(ctx, ensure_ascii=False))


def _prep_plan_cancel_key(job_id: str) -> str:
    return f"prep_plan:{job_id}:cancel"


def _prep_plan_task_ids_key(job_id: str) -> str:
    """Redis key где хранится JSON-массив task_id'ов всех запланированных
    сессий этого job'а. Нужен для force-cancel — revoke всех разом."""
    return f"prep_plan:{job_id}:task_ids"


@celery_app.task(bind=True, name="tasks.bulk_tasks.execute_prep_session",
                 acks_late=True, max_retries=0)
def execute_prep_session(self, job_id: str, acc_id: int, session_idx: int):
    """Один сеанс подготовки. Стартует по расписанию через countdown.

    Ключевой принцип: НИКАКИХ RETRY. Каждый повторный connect к акку —
    это ещё одна «попытка авторизации» с точки зрения Telegram, и в
    сочетании с ip_lock'ами (когда прокси занят и мы retry'им через
    те же 90 сек) это буквально путь к FloodWait / SessionRevoked.

    Что делает:
      1. Проверяет cancel-флаг → если стоит, помечает сессию 'cancelled'
      2. Читает plan из Redis, выцепляет sessions[session_idx]
      3. acquire account_lock (ждём до 20с)
      4. acquire ip_lock (не ждём — если занят, помечаем 'skipped' и уходим)
      5. connect → выполняет actions по порядку с микропаузами → disconnect
      6. Обновляет status сессии в plan + сохраняет ctx (channel_id и т.д.)

    Терминальные статусы (не повторяем):
      done       — всё ок
      failed     — упало во время выполнения (сеть/Telethon)
      cancelled  — юзер нажал стоп
      skipped    — прокси/аккаунт был занят (даём другим сессиям пройти)
      not_authorized — сессия акка мертва
    """
    import random as _r
    import traceback as _tb
    from utils.redis_pool import get_redis
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock, get_ip_cooldown_remaining
    redis = get_redis()

    log_prefix = f"[prep_session job={job_id[:8]} acc={acc_id} sess={session_idx}]"

    plan = _load_plan(redis, job_id, acc_id)
    if not plan:
        logger.warning(f"{log_prefix} plan not found — job истёк?")
        return {"ok": False, "reason": "plan not found"}
    sessions = plan.get("sessions", [])
    if session_idx >= len(sessions):
        logger.warning(f"{log_prefix} session_idx out of range")
        return {"ok": False, "reason": "session_idx out of range"}
    session = sessions[session_idx]
    if session.get("status") in ("done", "failed", "failed_permanent", "cancelled", "skipped"):
        logger.info(f"{log_prefix} already terminal ({session.get('status')}), пропускаем")
        return {"ok": True, "reason": "already terminal"}
    phone = plan.get("phone", "?")

    # ── Проверка cancel-флага ──
    if redis.get(_prep_plan_cancel_key(job_id)):
        session["status"] = "cancelled"
        session["error"] = "Отменено пользователем"
        session["finished_at"] = int(time.time())
        _save_plan(redis, job_id, acc_id, plan)
        logger.info(f"{log_prefix} cancelled by user flag")
        return {"ok": False, "reason": "cancelled"}

    session["status"] = "running"
    session["started_at"] = int(time.time())
    session["attempts"] = 1  # без retry-механик — всегда 1
    session["error"] = None
    _save_plan(redis, job_id, acc_id, plan)

    actions_summary = [a.get("type") for a in session.get("actions", [])]
    logger.info(f"{log_prefix} start · phone={phone} · actions={actions_summary}")

    def _terminate(status: str, reason: str, log_level: str = "info"):
        """Проставляет терминальный статус + finished_at + пишет в лог."""
        session["status"] = status
        session["error"] = reason
        session["finished_at"] = int(time.time())
        _save_plan(redis, job_id, acc_id, plan)
        getattr(logger, log_level)(f"{log_prefix} {status.upper()}: {reason}")

    async def _run():
        client, proxy = await _get_client_for_phone(phone)
        if not client:
            return {"status": "failed", "reason": "клиент не создался (нет сессии или прокси)"}

        # account_lock — ждём до 20 сек. Если занят — reschedule (не skip навсегда).
        acquired_acc = False
        for _ in range(20):
            if acquire_account_lock(acc_id, ttl=600):
                acquired_acc = True
                break
            await asyncio.sleep(1)
        if not acquired_acc:
            try: await client.disconnect()
            except: pass
            return {
                "status": "reschedule",
                "reason": "акк занят (warmup/commenting) — 20с не дождались",
                "wait_sec": random.randint(180, 360),
            }

        # ip_lock — не ждём. Если занят — RESCHEDULE на позже (после освобождения IP).
        # Ставим connect_ts = now + remaining + jitter, dispatcher подхватит.
        if proxy and not acquire_ip_lock(proxy, ttl=90):
            remaining = get_ip_cooldown_remaining(proxy)
            release_account_lock(acc_id)
            try: await client.disconnect()
            except: pass
            return {
                "status": "reschedule",
                "reason": f"IP {proxy.host} занят другой сессией (свободен через ~{remaining}с)",
                "wait_sec": max(60, remaining) + random.randint(30, 90),
            }

        try:
            try:
                await client.connect()
            except Exception as e:
                return {"status": "failed", "reason": f"connect: {type(e).__name__}: {str(e)[:200]}"}

            try:
                is_auth = await client.is_user_authorized()
            except Exception as e:
                return {"status": "failed", "reason": f"is_authorized: {type(e).__name__}: {str(e)[:150]}"}

            if not is_auth:
                try: await client.disconnect()
                except: pass
                return {"status": "not_authorized", "reason": "сессия акка мертва"}

            ctx = _load_ctx(redis, job_id, acc_id)
            results = []
            for i, action in enumerate(session.get("actions", [])):
                atype = action.get("type")
                try:
                    r = await _execute_action(client, phone, action, ctx)
                    if r.get("ok"):
                        logger.info(f"{log_prefix} action[{i}] {atype}: OK · {r.get('detail','')}")
                    else:
                        logger.warning(f"{log_prefix} action[{i}] {atype}: FAIL · {r.get('detail','')}")
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:200]}"
                    logger.exception(f"{log_prefix} action[{i}] {atype}: EXC · {err}")
                    r = {"ok": False, "detail": err}
                results.append({"type": atype, **r})
                # Микропауза между action'ами внутри сессии
                if i < len(session["actions"]) - 1:
                    await asyncio.sleep(_r.uniform(2.0, 6.0))
            _save_ctx(redis, job_id, acc_id, ctx)
            try: await client.disconnect()
            except: pass
            # Успех = хотя бы один action прошёл. Если все упали — сессия failed.
            any_ok = any(x.get("ok") for x in results)
            all_ok = all(x.get("ok") for x in results)
            if all_ok:
                return {"status": "done", "results": results}
            elif any_ok:
                # Часть прошла, часть нет — считаем done с warning'ом
                fails = [x for x in results if not x.get("ok")]
                return {"status": "done", "results": results,
                        "reason": f"частично: {len(fails)} action(ов) провалились"}
            else:
                # Все action'ы упали
                return {"status": "failed", "results": results,
                        "reason": f"все {len(results)} action(ов) провалились"}
        finally:
            if acquired_acc:
                release_account_lock(acc_id)

    try:
        result = run_async(_run())
    except Exception as e:
        # Неожиданная ошибка на уровне loop/setup. Пишем полный traceback в лог.
        tb = _tb.format_exc()
        logger.error(f"{log_prefix} UNEXPECTED EXCEPTION\n{tb}")
        _terminate("failed", f"{type(e).__name__}: {str(e)[:200]}", "error")
        return {"ok": False, "reason": str(e)[:200]}

    status = result.get("status", "failed")
    reason = result.get("reason")
    session["results"] = result.get("results", [])
    if status == "done":
        session["error"] = reason  # None или warning
        session["status"] = "done"
        session["finished_at"] = int(time.time())
        _save_plan(redis, job_id, acc_id, plan)
        logger.info(f"{log_prefix} DONE · {len(session['results'])} action(s)")
        return {"ok": True, "results": session["results"]}
    elif status == "reschedule":
        # Прокси/акк был занят. НЕ терминал — dispatcher вернётся через
        # connect_ts + jitter. До MAX_RESCHED попыток, потом честно skipped.
        MAX_RESCHED = 5
        rc = session.get("reschedule_count", 0) + 1
        session["reschedule_count"] = rc
        if rc > MAX_RESCHED:
            _terminate(
                "skipped",
                f"после {MAX_RESCHED} попыток IP/акк всё ещё занят · последняя: {reason}",
                "warning",
            )
            return {"ok": False, "reason": "reschedule limit reached"}
        wait_sec = int(result.get("wait_sec", 180))
        session["status"] = "scheduled"
        session["connect_ts"] = int(time.time()) + wait_sec
        session["error"] = f"⏰ resched #{rc}/{MAX_RESCHED}: {reason}"
        session.pop("started_at", None)  # чистим — сессия ещё не началась реально
        _save_plan(redis, job_id, acc_id, plan)
        logger.info(
            f"{log_prefix} ⏰ RESCHEDULED #{rc}/{MAX_RESCHED} через {wait_sec}с · {reason}"
        )
        return {"ok": True, "rescheduled": True, "reason": reason}
    else:
        _terminate(status, reason or status,
                   "warning" if status in ("skipped", "not_authorized") else "error")
        return {"ok": False, "reason": reason or status}


@celery_app.task(bind=True, name="tasks.bulk_tasks.prep_plan_start_bulk")
def prep_plan_start_bulk(
    self,
    accounts: list[dict],
    job_id: str,
    config: dict,
):
    """Генерация планов + постановка всех сессий в очередь Celery.

    config = {
        "target_duration_sec": 7200,
        "sessions_per_acc": 3,
        "change_names": bool, "names": [...],
        "change_avatars": bool, "profile_avatar_paths": [...],
        "create_channels": bool, "channel_titles": [...],
        "channel_description": "", "channel_first_post": "",
        "channel_avatar_paths": [...], "channel_pin": bool,
    }
    """
    from utils.redis_pool import get_redis
    from tasks.prep_plan import build_actions_for_account, generate_plan
    redis = get_redis()

    target_duration_sec = int(config.get("target_duration_sec", 3600))
    sessions_per_acc = max(1, min(int(config.get("sessions_per_acc", 3)), 6))
    start_ts = time.time()

    total_accounts = len(accounts)
    total_sessions_scheduled = 0
    total_actions_scheduled = 0

    meta = {
        "job_id": job_id,
        "start_ts": int(start_ts),
        "target_duration_sec": target_duration_sec,
        "sessions_per_acc": sessions_per_acc,
        "total_accounts": total_accounts,
        "config": {
            "change_names": config.get("change_names", False),
            "change_avatars": config.get("change_avatars", False),
            "create_channels": config.get("create_channels", False),
            "channel_pin": config.get("channel_pin", True),
        },
        "account_ids": [a["id"] for a in accounts],
    }

    # Копим task_id всех запланированных сессий — для force-revoke на cancel
    all_task_ids: list[str] = []

    for acc in accounts:
        acc_id = acc["id"]
        phone = acc.get("phone", "?")

        actions = build_actions_for_account(
            account_id=acc_id,
            change_names=bool(config.get("change_names")),
            names_pool=config.get("names") or [],
            change_avatars=bool(config.get("change_avatars")),
            profile_avatar_paths=config.get("profile_avatar_paths") or [],
            create_channels=bool(config.get("create_channels")),
            channel_titles=config.get("channel_titles") or [],
            channel_descriptions=config.get("channel_descriptions") or [],
            channel_first_post=config.get("channel_first_post") or "",
            channel_avatar_paths=config.get("channel_avatar_paths") or [],
            channel_post_photo_paths=config.get("channel_post_photo_paths") or [],
            channel_pin=bool(config.get("channel_pin", True)),
            delete_previous_profile_avatar=bool(config.get("delete_previous_profile_avatar")),
            delete_previous_channel_avatar=bool(config.get("delete_previous_channel_avatar")),
            custom_posts_by_acc=config.get("custom_posts_by_acc") or {},
            channel_public=bool(config.get("channel_public")),
            channel_public_usernames=config.get("channel_public_usernames") or None,
        )
        if not actions:
            continue

        plan = generate_plan(
            account_id=acc_id, phone=phone,
            actions=actions,
            target_start_ts=start_ts,
            target_duration_sec=target_duration_sec,
            n_sessions=sessions_per_acc,
        )
        _save_plan(redis, job_id, acc_id, plan)
        _save_ctx(redis, job_id, acc_id, {})

        # Планируем каждую сессию. Сохраняем task_id — на cancel вызовем
        # revoke() каждого. Тогда pending countdown'ы удалятся из очереди
        # Celery и не сработают вообще (не только пропустят action'ы).
        for sess in plan["sessions"]:
            countdown = max(0, sess["connect_ts"] - int(time.time()))
            async_result = celery_app.send_task(
                "tasks.bulk_tasks.execute_prep_session",
                args=[job_id, acc_id, sess["idx"]],
                queue="bulk_actions",
                countdown=countdown,
            )
            sess["task_id"] = async_result.id
            all_task_ids.append(async_result.id)
            total_sessions_scheduled += 1
            total_actions_scheduled += len(sess.get("actions", []))
            sess["status"] = "scheduled"
        _save_plan(redis, job_id, acc_id, plan)

    # Сохраняем плоский список task_id для force-revoke
    if all_task_ids:
        redis.setex(
            _prep_plan_task_ids_key(job_id), 7 * 86400,
            json.dumps(all_task_ids),
        )

    meta["total_sessions_scheduled"] = total_sessions_scheduled
    meta["total_actions_scheduled"] = total_actions_scheduled
    redis.setex(_prep_plan_meta_key(job_id), 7 * 86400,
                json.dumps(meta, ensure_ascii=False))
    return meta


@celery_app.task(bind=True, name="tasks.bulk_tasks.dispatch_prep_plan_sessions",
                 ignore_result=True)
def dispatch_prep_plan_sessions(self):
    """Периодический диспетчер (beat, каждые 60 сек):
    находит `scheduled` сессии с прошедшим connect_ts и пере-запускает
    их через send_task с countdown=0.

    Нужно потому что Celery `send_task(countdown=N)` держит задачу
    в памяти worker'а (ETA reserved), НЕ в broker'е. При рестарте
    worker'а эти отложенные задачи ТЕРЯЮТСЯ. Redis-plan остаётся,
    но никто его не выполнит.

    Beat запускается независимо от worker'ов и переставляет задачи
    в живую очередь. Race condition исключается тем что executor
    моментально ставит status='running' при взятии сессии в работу —
    dispatcher сюда уже не залезет.
    """
    from utils.redis_pool import get_redis
    import time as _t
    r = get_redis()
    now_ts = int(_t.time())

    total_dispatched = 0
    total_jobs = 0

    # Сканируем meta-ключи всех активных prep-plan job'ов
    for meta_key in r.scan_iter(match="prep_plan:*:meta"):
        try:
            meta_raw = r.get(meta_key)
            if not meta_raw:
                continue
            meta = json.loads(meta_raw)
        except Exception:
            continue
        # Пропускаем cancelled/done job'ы
        job_id = meta.get("job_id")
        if not job_id:
            continue
        if r.get(f"prep_plan:{job_id}:cancel"):
            continue

        job_dispatched = 0
        for acc_id in meta.get("account_ids") or []:
            plan_key = f"prep_plan:{job_id}:acc:{acc_id}"
            plan_raw = r.get(plan_key)
            if not plan_raw:
                continue
            try:
                plan = json.loads(plan_raw)
            except Exception:
                continue
            plan_changed = False
            for sess in plan.get("sessions", []):
                # Отправляем только те что: scheduled + время пришло
                if sess.get("status") != "scheduled":
                    continue
                connect_ts = sess.get("connect_ts", 0)
                if connect_ts > now_ts:
                    continue
                # Пере-отправляем с countdown=0 (немедленно) + маленький
                # jitter 0-15с чтобы не было burst'а
                try:
                    ar = celery_app.send_task(
                        "tasks.bulk_tasks.execute_prep_session",
                        args=[job_id, acc_id, sess["idx"]],
                        queue="bulk_actions",
                        countdown=random.randint(0, 15),
                    )
                    sess["task_id"] = ar.id
                    plan_changed = True
                    job_dispatched += 1
                    total_dispatched += 1
                except Exception as e:
                    logger.warning(
                        f"[dispatch_prep] {job_id[:8]}/{acc_id}/{sess.get('idx')}: "
                        f"send_task fail: {e}"
                    )
            if plan_changed:
                _save_plan(r, job_id, acc_id, plan)
        if job_dispatched:
            total_jobs += 1
            logger.info(
                f"[dispatch_prep] {job_id[:8]}: пере-запущено {job_dispatched} "
                f"просроченных сессий"
            )

    if total_dispatched:
        logger.info(
            f"[dispatch_prep] Итого: {total_dispatched} сессий из {total_jobs} job'ов"
        )
    return {"dispatched": total_dispatched, "jobs": total_jobs}


# ── Ретраебельные ошибки (те что не финальны, стоит попробовать снова) ──
# ip busy         — прокси занят другой сессией, IP_lock 90с, скоро освободится
# account busy    — акк в warmup/commenting локе, скоро освободится
# no client       — тайминг с БД / redis, редкая транзиентная ошибка
# not authorized  — ТЕРМИНАЛ (сессия акка мертва, не ретраим)
# no ctx / no channel — ТЕРМИНАЛ (в этом job'е канал не создавался)
_RETRYABLE_REASON_SUBSTRINGS = ("ip busy", "account busy", "no client")


def _is_retryable_reason(reason: str) -> bool:
    if not reason: return False
    r = reason.lower()
    return any(sub in r for sub in _RETRYABLE_REASON_SUBSTRINGS)


def _extract_flood_wait_seconds(result: dict) -> int:
    """Если в reason закодирован FloodWait — возвращает секунды. Иначе 0."""
    if not result: return 0
    reason = (result.get("reason") or "").lower()
    # Telethon: "FloodWaitError: A wait of N seconds is required"
    # Или наш явный field flood_wait_sec
    if isinstance(result.get("flood_wait_sec"), int):
        return int(result["flood_wait_sec"])
    import re as _re
    m = _re.search(r"wait of (\d+) seconds", reason)
    if m: return int(m.group(1))
    m = _re.search(r"floodwait.*?(\d+)", reason)
    if m: return int(m.group(1))
    return 0


async def _process_by_proxy_parallel(
    account_ids: list[int],
    process_one_fn,           # async fn(acc_id) -> {ok, phone, reason, ...}
    state_container: dict,    # {"results": [...]} — вливаем сюда для publisher'а
    publisher_fn,             # fn(status, current_phone=..., sleeping_sec=...)
    delay_min_sec: int,
    delay_max_sec: int,
    max_attempts: int = 5,
    max_flood_wait_sec: int = 1800,
    max_parallel_proxies: int = 4,
    logger_prefix: str = "[refix]",
    cancel_check_fn=None,     # sync fn() -> bool; True = отменить и выйти
) -> list[dict]:
    """Умный параллельный цикл: группирует акки по прокси, каждый прокси
    получает своего worker'а, до 4 worker'ов бегут параллельно.

    Внутри worker'а прокси — акки идут последовательно (соблюдаем ip_lock).
    Ждём умно:
      - ip busy — парсим "~118с" из reason и спим ровно столько + jitter
      - account busy — короткий sleep 15-45с
      - FloodWait Ns — спим N + jitter
      - После sleep — повторяем этот же акк (не переходим к следующему)
    """
    import random as _r
    import re as _re
    from collections import defaultdict
    from sqlalchemy import select as _sel
    from models.account import TelegramAccount
    from utils.db_pool import async_session as _Session

    # 1. Тянем proxy_id для каждого акка (единый запрос в БД)
    acc_proxy: dict[int, int] = {}
    try:
        async with _Session() as db:
            rows = (await db.execute(
                _sel(TelegramAccount.id, TelegramAccount.proxy_id)
                .where(TelegramAccount.id.in_(account_ids))
            )).all()
            for row in rows:
                acc_proxy[row[0]] = row[1] or 0
    except Exception as e:
        logger.warning(f"{logger_prefix} проксий-lookup fail: {e}")
        for a in account_ids: acc_proxy[a] = 0

    # 2. Группируем по proxy_id (0 = без прокси, идут все в одну группу
    #    но там не должно быть коллизий ip_lock т.к. прокси нет)
    proxy_groups: dict[int, list[int]] = defaultdict(list)
    for acc_id in account_ids:
        proxy_groups[acc_proxy.get(acc_id, 0)].append(acc_id)
    logger.info(
        f"{logger_prefix} sharded: {len(account_ids)} акк на "
        f"{len(proxy_groups)} прокси, макс {max_parallel_proxies} воркеров параллельно"
    )

    # 3. Общее состояние результатов
    results_by_acc: dict[int, dict] = {}   # финальные + текущие retry-in-flight
    attempts: dict[int, int] = {a: 0 for a in account_ids}

    def _sync_state():
        state_container["results"] = list(results_by_acc.values())

    def _pub(current_phone: str = "", sleeping_sec: int = 0):
        _sync_state()
        publisher_fn("running", current_phone=current_phone, sleeping_sec=sleeping_sec)

    def _parse_ip_wait(reason: str) -> int:
        # "IP 138.16.51.4 занят другой сессией (свободен через ~118с)"
        m = _re.search(r"~(\d+)с", reason)
        if m: return int(m.group(1))
        return 60   # default

    def _is_cancelled() -> bool:
        if cancel_check_fn is None:
            return False
        try:
            return bool(cancel_check_fn())
        except Exception:
            return False

    async def _cancellable_sleep(total: float, chunk: float = 2.0) -> bool:
        """Спит total секунд, но каждые chunk сек проверяет cancel.
        Возвращает True если отменено (спим прервали)."""
        remaining = float(total)
        while remaining > 0:
            if _is_cancelled():
                return True
            step = min(chunk, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return False

    # 4. Worker для одной прокси-группы
    async def proxy_worker(proxy_id: int, accs: list[int]):
        if _is_cancelled():
            return
        # STAGGER: чтобы 4 паралельных воркера не стартанули в одну секунду
        # и не сделали синхронный burst — каждый ждёт 0-30 сек перед первым
        # аккаунтом, размазывая старт.
        stagger = _r.uniform(0.0, 30.0)
        _pub(current_phone=f"proxy={proxy_id} стартовый разброс", sleeping_sec=int(stagger))
        if await _cancellable_sleep(stagger): return

        for idx, acc_id in enumerate(accs):
            if _is_cancelled(): return
            # Между акками одной прокси — стандартная пауза (30-90с)
            if idx > 0:
                pause = _r.uniform(float(delay_min_sec), float(delay_max_sec))
                _pub(current_phone=f"proxy={proxy_id} перед acc={acc_id}", sleeping_sec=int(pause))
                if await _cancellable_sleep(pause): return

            # До max_attempts попыток
            while attempts[acc_id] < max_attempts:
                attempts[acc_id] += 1
                att = attempts[acc_id]
                r = await process_one_fn(acc_id)
                r["attempts"] = att
                phone = r.get("phone", f"acc:{acc_id}")

                if r.get("ok"):
                    results_by_acc[acc_id] = r
                    if r.get("skipped"):
                        logger.info(f"{logger_prefix} {phone}: SKIP ({r.get('reason','уже ок')}) proxy={proxy_id}")
                        _pub(current_phone=f"{phone} — ⏭ {r.get('reason','уже ок')}")
                    else:
                        logger.info(f"{logger_prefix} {phone}: OK (att={att} proxy={proxy_id})")
                        _pub(current_phone=phone)
                    break

                reason = r.get("reason", "")

                # FloodWait
                fw = _extract_flood_wait_seconds(r)
                if fw > 0:
                    if fw > max_flood_wait_sec:
                        results_by_acc[acc_id] = r
                        logger.warning(f"{logger_prefix} {phone}: FloodWait {fw}с > лимит — final")
                        _pub(current_phone=phone)
                        break
                    logger.info(f"{logger_prefix} {phone}: FloodWait {fw}с — жду и повторю")
                    r_temp = dict(r); r_temp["reason"] = f"⏰ FloodWait {fw}s (жду)"
                    results_by_acc[acc_id] = r_temp
                    _pub(current_phone=phone, sleeping_sec=fw)
                    if await _cancellable_sleep(fw + _r.uniform(2, 10)): return
                    continue

                # ip busy / account busy — умное ожидание по фактической длине cooldown
                if _is_retryable_reason(reason):
                    rlow = reason.lower()
                    if "ip busy" in rlow:
                        wait_s = _parse_ip_wait(reason) + _r.randint(5, 20)
                        note = f"IP занят, жду {wait_s}с"
                    elif "account busy" in rlow:
                        wait_s = _r.randint(15, 45)
                        note = f"acc занят, жду {wait_s}с"
                    else:
                        wait_s = _r.randint(20, 60)
                        note = f"retry через {wait_s}с"
                    logger.info(f"{logger_prefix} {phone}: {reason} — {note} (att={att} proxy={proxy_id})")
                    r_temp = dict(r); r_temp["reason"] = f"🔁 {reason} (retry {att}/{max_attempts}, жду {wait_s}с)"
                    results_by_acc[acc_id] = r_temp
                    _pub(current_phone=phone, sleeping_sec=wait_s)
                    if await _cancellable_sleep(wait_s): return
                    continue

                # Терминальная ошибка (not_authorized, no_ctx, etc)
                results_by_acc[acc_id] = r
                logger.warning(f"{logger_prefix} {phone}: {reason} — final (att={att})")
                _pub(current_phone=phone)
                break

            # Исчерпаны attempts — если ещё не записан, пометим как final
            if acc_id not in results_by_acc:
                results_by_acc[acc_id] = {
                    "acc_id": acc_id, "ok": False, "attempts": attempts[acc_id],
                    "reason": f"исчерпан лимит {max_attempts} попыток",
                }
                _pub()

    # 5. Запускаем воркеры параллельно с capacity semaphore
    sem = asyncio.Semaphore(max_parallel_proxies)
    async def _capped(proxy_id, accs):
        async with sem:
            await proxy_worker(proxy_id, accs)

    tasks = [_capped(p, a) for p, a in proxy_groups.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

    _sync_state()
    return list(results_by_acc.values())


async def _process_with_retry_loop(
    account_ids: list[int],
    process_one_fn,           # async fn(acc_id) -> {ok, phone, reason, ...}
    publisher_fn,             # fn(status, current_phone=..., sleeping_sec=..., extra=...)
    delay_min_sec: int,
    delay_max_sec: int,
    max_attempts: int = 5,
    max_flood_wait_sec: int = 1800,  # 30 мин
    logger_prefix: str = "[refix]",
) -> list[dict]:
    """Общий цикл: пробегается по всем акка, ретраит retryable ошибки,
    ждёт FloodWait (до 30 мин), пропускает only after max_attempts.

    Возвращает список финальных результатов.
    """
    import random as _r
    # queue.append + queue.pop(0) — FIFO. attempts[acc_id] считаем.
    queue = list(account_ids)
    attempts = {a: 0 for a in account_ids}
    results = []          # финальные (ok или окончательно failed)
    # результаты в UI сортируем по acc_id → в publisher_fn'е

    def _record(r: dict, final: bool = True):
        # Заменяем прошлый результат для этого acc_id если есть
        acc = r.get("acc_id")
        for i, existing in enumerate(results):
            if existing.get("acc_id") == acc:
                results[i] = r
                return
        results.append(r)

    while queue:
        acc_id = queue.pop(0)
        attempts[acc_id] += 1
        att = attempts[acc_id]

        # Пауза перед каждым акком (кроме самого первого запуска)
        total_processed = sum(1 for r in results if r.get("acc_id") == acc_id) + len([q for q in queue if q == acc_id])
        if len(results) > 0 or att > 1:
            pause = _r.uniform(float(delay_min_sec), float(delay_max_sec))
            publisher_fn("running", current_phone=f"акк {acc_id} #{att}", sleeping_sec=int(pause))
            await asyncio.sleep(pause)

        r = await process_one_fn(acc_id)
        r["attempts"] = att
        phone = r.get("phone", f"acc:{acc_id}")

        if r.get("ok"):
            logger.info(f"{logger_prefix} {phone}: OK (attempt {att})")
            _record(r, final=True)
            publisher_fn("running", current_phone=phone)
            continue

        # FloodWait — ждём и возвращаем в очередь.
        # Если превышен потолок, помечаем окончательным fail.
        fw = _extract_flood_wait_seconds(r)
        if fw > 0:
            if fw > max_flood_wait_sec:
                r["reason"] = f"FloodWait {fw}s превышает лимит {max_flood_wait_sec}s"
                logger.warning(f"{logger_prefix} {phone}: FloodWait too long: {fw}с — окончательный fail")
                _record(r, final=True)
                publisher_fn("running", current_phone=phone)
                continue
            logger.warning(f"{logger_prefix} {phone}: FloodWait {fw}с — ждём и повторим")
            # публикуем состояние ожидания
            r_temp = dict(r); r_temp["reason"] = f"FloodWait {fw}s (жду и повторю)"
            _record(r_temp, final=False)
            publisher_fn("running", current_phone=phone, sleeping_sec=fw)
            await asyncio.sleep(fw + _r.uniform(2, 10))
            queue.append(acc_id)   # снова в конец очереди
            continue

        reason = r.get("reason", "")
        if _is_retryable_reason(reason) and att < max_attempts:
            logger.info(f"{logger_prefix} {phone}: {reason} — попробуем ещё (attempt {att}/{max_attempts})")
            # Не записываем как финальный — заглушкой запишем текущее состояние
            r_temp = dict(r); r_temp["reason"] = f"{reason} (retry {att}/{max_attempts})"
            _record(r_temp, final=False)
            publisher_fn("running", current_phone=phone)
            queue.append(acc_id)   # ре-queue
            continue

        # Окончательный fail
        logger.warning(f"{logger_prefix} {phone}: {reason} — final (attempts={att})")
        _record(r, final=True)
        publisher_fn("running", current_phone=phone)

    return results


def _refix_progress_key(job_id: str, kind: str) -> str:
    """Redis key где хранится snapshot прогресса refix task'и.
    kind: 'avatar' | 'video'."""
    return f"refix:{job_id}:{kind}:progress"


def _refix_cancel_key(job_id: str, kind: str) -> str:
    """Redis-флаг отмены. Устанавливается API → воркер видит и завершается
    после текущего акка, без запуска следующих ретраев/пауз."""
    return f"refix:{job_id}:{kind}:cancel"


def _publish_refix_progress(redis, key: str, snapshot: dict, ttl: int = 3600):
    try:
        redis.setex(key, ttl, json.dumps(snapshot, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"[refix] publish progress fail: {e}")


@celery_app.task(bind=True, name="tasks.bulk_tasks.refix_channel_avatar_bulk")
def refix_channel_avatar_bulk(
    self,
    job_id: str,
    new_avatar_path: str,
    delay_min_sec: int = 30,
    delay_max_sec: int = 90,
    delete_previous: bool = False,
):
    """Ставит аватарку каналам из старого prep-plan job'а.

    Юзкейс: юзер создал 70 каналов через план, но забыл включить чекбокс
    «аватарка канала». Хочет добавить её постфактум одним файлом на все.

    Для каждого акка:
      1. Из ctx достаём channel_id/access_hash (значит канал был создан)
      2. Опционально снимаем текущее фото (InputChatPhotoEmpty) — если
         delete_previous, с паузой 2-5 сек
      3. Uploadим фото, EditPhotoRequest с InputChatUploadedPhoto
    """
    from utils.redis_pool import get_redis
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock
    from telethon.tl.functions.channels import EditPhotoRequest
    from telethon.tl.types import InputChannel, InputChatUploadedPhoto, InputChatPhotoEmpty
    import random as _r
    redis = get_redis()

    meta_raw = redis.get(f"prep_plan:{job_id}:meta")
    if not meta_raw:
        return {"ok": False, "reason": "job meta not found"}
    meta = json.loads(meta_raw)
    account_ids = meta.get("account_ids") or []

    if not os.path.exists(new_avatar_path):
        return {"ok": False, "reason": f"file not found: {new_avatar_path}"}

    results = []

    async def _process_one(acc_id: int):
        # Phone-lookup ПЕРВЫМ — чтобы early-exit reasons содержали телефон
        # (иначе UI показывает только "acc:63" и юзеру непонятно кто это).
        plan_raw = redis.get(f"prep_plan:{job_id}:acc:{acc_id}")
        phone = None
        if plan_raw:
            try: phone = json.loads(plan_raw).get("phone")
            except: pass
        # Fallback: если plan TTL истёк — берём phone из БД. Иначе
        # UI показывает "acc:63" вместо телефона.
        if not phone:
            phone = await _get_phone_by_acc_id(acc_id)

        ctx_raw = redis.get(f"prep_plan:{job_id}:ctx:{acc_id}")
        if not ctx_raw:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no ctx (канал не создавался)"}
        try: ctx = json.loads(ctx_raw)
        except Exception: return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "corrupted ctx"}
        ch_id = ctx.get("channel_id"); ch_hash = ctx.get("channel_access_hash")
        if not ch_id or not ch_hash:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no channel in ctx"}

        if not phone:
            return {"acc_id": acc_id, "ok": False, "reason": "no phone in plan"}

        client, proxy = await _get_client_for_phone(phone)
        if not client:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no client"}

        acquired_acc = False
        for _ in range(20):
            if acquire_account_lock(acc_id, ttl=300):
                acquired_acc = True; break
            await asyncio.sleep(1)
        if not acquired_acc:
            try: await client.disconnect()
            except: pass
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "account busy"}

        if proxy and not acquire_ip_lock(proxy, ttl=90):
            release_account_lock(acc_id)
            try: await client.disconnect()
            except: pass
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "ip busy"}

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "not authorized"}

            ch_input = InputChannel(channel_id=ch_id, access_hash=ch_hash)

            # SKIP-if-already: если delete_previous=False и у канала уже
            # стоит аватарка — пропускаем этот акк, чтобы не жечь запросы
            # и не менять то что юзер уже поставил.
            if not delete_previous:
                try:
                    from telethon.tl.functions.channels import GetFullChannelRequest
                    from telethon.tl.types import ChatPhoto, ChatPhotoEmpty
                    full = await client(GetFullChannelRequest(ch_input))
                    chat = None
                    for c in (full.chats or []):
                        if c.id == ch_id: chat = c; break
                    has_ava = chat is not None and isinstance(chat.photo, ChatPhoto)
                    if has_ava:
                        return {
                            "acc_id": acc_id, "phone": phone, "ok": True,
                            "channel_id": ch_id, "skipped": True,
                            "reason": "аватарка уже стоит, пропускаем",
                        }
                except Exception as e:
                    # Если проверка упала — не блокируем, идём делать
                    logger.debug(f"[refix_ava] {phone} check has_ava: {e}")

            # Опциональное снятие старого фото
            if delete_previous:
                try:
                    await client(EditPhotoRequest(channel=ch_input, photo=InputChatPhotoEmpty()))
                    await asyncio.sleep(_r.uniform(2.0, 5.0))
                except Exception as e:
                    logger.warning(f"[refix_ava] {phone} delete_prev: {e}")

            try:
                file_handle = await client.upload_file(new_avatar_path)
                await client(EditPhotoRequest(
                    channel=ch_input,
                    photo=InputChatUploadedPhoto(file=file_handle),
                ))
                return {"acc_id": acc_id, "phone": phone, "ok": True, "channel_id": ch_id}
            except Exception as e:
                return {
                    "acc_id": acc_id, "phone": phone, "ok": False,
                    "reason": f"upload/edit: {type(e).__name__}: {str(e)[:150]}",
                }
        finally:
            try: await client.disconnect()
            except: pass
            release_account_lock(acc_id)

    prog_key = _refix_progress_key(job_id, "avatar")

    # Общий контейнер результатов передаётся через closure в publisher
    _state = {"results": []}

    def _pub(status: str, current_phone: str = "", sleeping_sec: int = 0):
        rs = _state["results"]
        # Финальные = у кого есть ok=True/False и attempts >= 1 при этом
        # причина не оканчивается на "(retry N/M)" или "(жду и повторю)".
        def is_final(r):
            if r.get("ok"): return True
            reason = (r.get("reason") or "")
            if "(retry " in reason or "(жду и повторю)" in reason:
                return False
            return True
        finals = [r for r in rs if is_final(r)]
        ok = sum(1 for r in finals if r.get("ok"))
        fail = sum(1 for r in finals if not r.get("ok"))
        _publish_refix_progress(redis, prog_key, {
            "status": status,
            "job_id": job_id, "kind": "avatar",
            "total": len(account_ids),
            "done": len(finals),
            "ok": ok, "failed": fail,
            "pending": len(account_ids) - len(finals),
            "current_phone": current_phone,
            "sleeping_sec": sleeping_sec,
            "results": rs[-20:],
            "ts": int(time.time()),
        })

    cancel_key = _refix_cancel_key(job_id, "avatar")
    # На старте — очищаем стейл-флаг с прошлого запуска
    try: redis.delete(cancel_key)
    except: pass

    def _is_cancelled() -> bool:
        try: return bool(redis.get(cancel_key))
        except: return False

    _pub("running")

    async def _run():
        await _process_by_proxy_parallel(
            account_ids=account_ids,
            process_one_fn=_process_one,
            state_container=_state,
            publisher_fn=_pub,
            delay_min_sec=delay_min_sec,
            delay_max_sec=delay_max_sec,
            max_attempts=5,
            max_flood_wait_sec=1800,
            max_parallel_proxies=4,
            logger_prefix=f"[refix_ava job={job_id[:8]}]",
            cancel_check_fn=_is_cancelled,
        )

    run_async(_run())

    ok_count = sum(1 for r in _state["results"] if r.get("ok"))
    cancelled = _is_cancelled()
    final = {
        "ok": True,
        "cancelled": cancelled,
        "total_processed": len(_state["results"]),
        "successful": ok_count,
        "failed": len(_state["results"]) - ok_count,
        "results": _state["results"],
    }
    _pub("cancelled" if cancelled else "done")
    # Убираем флаг чтобы следующий запуск был чистым
    try: redis.delete(cancel_key)
    except: pass
    return final


# ══════════════════════════════════════════════════════════════
# REFIX ATTACH CHANNEL — прикрепить канал к профилю (personal_channel)
# ══════════════════════════════════════════════════════════════
# Это тот самый маленький значок канала в личном профиле юзера
# (не «пин в диалогах», не «пин сообщения»). Делает `UpdatePersonalChannelRequest`.
#
# Юзкейс: юзер запустил старую подготовку без опции «прикрепить канал в
# профиль» и хочет добавить это постфактум ко всем 70 акаунтам.

@celery_app.task(bind=True, name="tasks.bulk_tasks.refix_attach_channel_bulk")
def refix_attach_channel_bulk(
    self,
    job_id: str,
    delay_min_sec: int = 30,
    delay_max_sec: int = 90,
):
    """Ставит созданный канал как personal_channel в профиле юзера.

    Для каждого акка из meta:
      1. Из ctx достаём channel_id / access_hash (был create_channel)
      2. Проверяем personal_channel_id через GetFullUserRequest — если уже
         стоит наш ID, скипаем.
      3. UpdatePersonalChannelRequest(channel=ch_input)
    """
    from utils.redis_pool import get_redis
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock
    from telethon.tl.functions.account import UpdatePersonalChannelRequest
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.types import InputChannel, InputUserSelf
    import random as _r
    redis = get_redis()

    meta_raw = redis.get(f"prep_plan:{job_id}:meta")
    if not meta_raw:
        return {"ok": False, "reason": "job meta not found"}
    meta = json.loads(meta_raw)
    account_ids = meta.get("account_ids") or []

    async def _process_one(acc_id: int):
        # Сначала пытаемся достать phone из plan — чтобы даже early-exit
        # ошибки типа "no ctx" содержали телефон, а UI не показывал только
        # "acc:63" (юзер не понимает какой это акк).
        plan_raw = redis.get(f"prep_plan:{job_id}:acc:{acc_id}")
        phone = None
        if plan_raw:
            try: phone = json.loads(plan_raw).get("phone")
            except: pass
        # Fallback: если plan TTL истёк — берём phone из БД. Иначе
        # UI показывает "acc:63" вместо телефона.
        if not phone:
            phone = await _get_phone_by_acc_id(acc_id)

        ctx_raw = redis.get(f"prep_plan:{job_id}:ctx:{acc_id}")
        if not ctx_raw:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no ctx (канал не создавался)"}
        try: ctx = json.loads(ctx_raw)
        except Exception: return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "corrupted ctx"}
        ch_id = ctx.get("channel_id"); ch_hash = ctx.get("channel_access_hash")
        if not ch_id or not ch_hash:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no channel in ctx"}

        if not phone:
            return {"acc_id": acc_id, "ok": False, "reason": "no phone in plan"}

        client, proxy = await _get_client_for_phone(phone)
        if not client:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no client"}

        acquired_acc = False
        for _ in range(20):
            if acquire_account_lock(acc_id, ttl=300):
                acquired_acc = True; break
            await asyncio.sleep(1)
        if not acquired_acc:
            try: await client.disconnect()
            except: pass
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "account busy"}

        if proxy and not acquire_ip_lock(proxy, ttl=90):
            release_account_lock(acc_id)
            try: await client.disconnect()
            except: pass
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "ip busy"}

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "not authorized"}

            ch_input = InputChannel(channel_id=ch_id, access_hash=ch_hash)

            # SKIP-if-already: personal_channel_id уже указывает на наш канал
            try:
                full = await client(GetFullUserRequest(InputUserSelf()))
                cur_pc = getattr(full.full_user, "personal_channel_id", 0) or 0
                if int(cur_pc) == int(ch_id):
                    return {
                        "acc_id": acc_id, "phone": phone, "ok": True,
                        "channel_id": ch_id, "skipped": True,
                        "reason": "канал уже прикреплён к профилю",
                    }
            except Exception as e:
                logger.debug(f"[refix_attach] {phone} check personal_channel: {e}")

            try:
                await client(UpdatePersonalChannelRequest(channel=ch_input))
                return {"acc_id": acc_id, "phone": phone, "ok": True, "channel_id": ch_id}
            except Exception as e:
                return {
                    "acc_id": acc_id, "phone": phone, "ok": False,
                    "reason": f"attach: {type(e).__name__}: {str(e)[:150]}",
                }
        finally:
            try: await client.disconnect()
            except: pass
            release_account_lock(acc_id)

    prog_key = _refix_progress_key(job_id, "attach")
    _state = {"results": []}

    def _pub(status: str, current_phone: str = "", sleeping_sec: int = 0):
        rs = _state["results"]
        def is_final(r):
            if r.get("ok"): return True
            reason = (r.get("reason") or "")
            if "(retry " in reason or "(жду и повторю)" in reason:
                return False
            return True
        finals = [r for r in rs if is_final(r)]
        ok = sum(1 for r in finals if r.get("ok"))
        fail = sum(1 for r in finals if not r.get("ok"))
        _publish_refix_progress(redis, prog_key, {
            "status": status,
            "job_id": job_id, "kind": "attach",
            "total": len(account_ids),
            "done": len(finals),
            "ok": ok, "failed": fail,
            "pending": len(account_ids) - len(finals),
            "current_phone": current_phone,
            "sleeping_sec": sleeping_sec,
            "results": rs[-20:],
            "ts": int(time.time()),
        })

    cancel_key = _refix_cancel_key(job_id, "attach")
    try: redis.delete(cancel_key)
    except: pass

    def _is_cancelled() -> bool:
        try: return bool(redis.get(cancel_key))
        except: return False

    _pub("running")

    async def _run():
        await _process_by_proxy_parallel(
            account_ids=account_ids,
            process_one_fn=_process_one,
            state_container=_state,
            publisher_fn=_pub,
            delay_min_sec=delay_min_sec,
            delay_max_sec=delay_max_sec,
            max_attempts=5,
            max_flood_wait_sec=1800,
            max_parallel_proxies=4,
            logger_prefix=f"[refix_attach job={job_id[:8]}]",
            cancel_check_fn=_is_cancelled,
        )

    run_async(_run())

    ok_count = sum(1 for r in _state["results"] if r.get("ok"))
    cancelled = _is_cancelled()
    final = {
        "ok": True,
        "cancelled": cancelled,
        "total_processed": len(_state["results"]),
        "successful": ok_count,
        "failed": len(_state["results"]) - ok_count,
        "results": _state["results"],
    }
    _pub("cancelled" if cancelled else "done")
    try: redis.delete(cancel_key)
    except: pass
    return final


@celery_app.task(bind=True, name="tasks.bulk_tasks.refix_first_post_bulk")
def refix_first_post_bulk(
    self,
    job_id: str,
    new_video_path: str,
    caption: str = "",
    delay_min_sec: int = 30,
    delay_max_sec: int = 90,
):
    """Перезаливает первый пост в каналах из старого prep-plan job'а.

    Зачем: если .mov отправился как документ (без hachoir → крохотный
    серый превью), это исправляет уже висящие каналы:
      1. Читаем meta.account_ids старого job'а
      2. Для каждого акка:
         - Из Redis ctx достаём channel_id / access_hash
         - Подключаемся к акку
         - Ищем первое сообщение с медиа в канале
         - Удаляем его
         - Отправляем новое через _send_media_smart (правильные атрибуты + thumb)
      3. Между акками — рандомная пауза + per-IP throttle
    """
    from utils.redis_pool import get_redis
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock, get_ip_cooldown_remaining
    from telethon.tl.types import InputPeerChannel
    import random as _r
    redis = get_redis()

    meta_raw = redis.get(f"prep_plan:{job_id}:meta")
    if not meta_raw:
        return {"ok": False, "reason": "job meta not found"}
    meta = json.loads(meta_raw)
    account_ids = meta.get("account_ids") or []

    if not os.path.exists(new_video_path):
        return {"ok": False, "reason": f"file not found: {new_video_path}"}

    results = []

    async def _process_one(acc_id: int):
        # Phone ПЕРВЫМ — early-exit reasons должны содержать телефон,
        # чтобы UI не показывал только "acc:63".
        plan_raw = redis.get(f"prep_plan:{job_id}:acc:{acc_id}")
        phone = None
        if plan_raw:
            try:
                plan = json.loads(plan_raw)
                phone = plan.get("phone")
            except Exception:
                pass
        if not phone:
            phone = await _get_phone_by_acc_id(acc_id)

        ctx_raw = redis.get(f"prep_plan:{job_id}:ctx:{acc_id}")
        if not ctx_raw:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no ctx (канал не создавался или ctx истёк)"}
        try:
            ctx = json.loads(ctx_raw)
        except Exception:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "corrupted ctx"}
        ch_id = ctx.get("channel_id")
        ch_hash = ctx.get("channel_access_hash")
        if not ch_id or not ch_hash:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no channel in ctx"}

        if not phone:
            return {"acc_id": acc_id, "ok": False, "reason": "no phone in plan"}

        client, proxy = await _get_client_for_phone(phone)
        if not client:
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "no client"}

        # account_lock
        acquired_acc = False
        for _ in range(20):
            if acquire_account_lock(acc_id, ttl=300):
                acquired_acc = True; break
            await asyncio.sleep(1)
        if not acquired_acc:
            try: await client.disconnect()
            except: pass
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "account busy"}

        # ip_lock — не ждём, скипаем если занят
        if proxy and not acquire_ip_lock(proxy, ttl=90):
            release_account_lock(acc_id)
            try: await client.disconnect()
            except: pass
            return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "ip busy"}

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"acc_id": acc_id, "phone": phone, "ok": False, "reason": "not authorized"}

            entity = InputPeerChannel(channel_id=ch_id, access_hash=ch_hash)

            # Находим первое сообщение с медиа (обычно самое старое)
            first_media_id = None
            first_msg_obj = None
            try:
                async for msg in client.iter_messages(entity, limit=20, reverse=True):
                    if msg.media is not None:
                        first_media_id = msg.id
                        first_msg_obj = msg
                        break
            except Exception as e:
                logger.warning(f"[refix] {phone}: iter_messages fail: {e}")

            # SKIP-if-already: если первое медиа уже "нормальное видео"
            # (MessageMediaDocument с DocumentAttributeVideo и duration > 0),
            # значит наш новый _send_media_smart с hachoir уже сработал —
            # переливать не нужно.
            if first_msg_obj and first_msg_obj.media is not None:
                try:
                    from telethon.tl.types import (
                        MessageMediaDocument, DocumentAttributeVideo,
                    )
                    media = first_msg_obj.media
                    if isinstance(media, MessageMediaDocument) and media.document:
                        for attr in media.document.attributes:
                            if isinstance(attr, DocumentAttributeVideo) and attr.duration > 0:
                                return {
                                    "acc_id": acc_id, "phone": phone, "ok": True,
                                    "channel_id": ch_id, "skipped": True,
                                    "reason": "видео уже норм (duration + attributes), пропускаем",
                                }
                except Exception as e:
                    logger.debug(f"[refix] {phone} check video attrs: {e}")

            if first_media_id:
                try:
                    await client.delete_messages(entity, [first_media_id])
                    await asyncio.sleep(_r.uniform(1.5, 3.0))
                except Exception as e:
                    logger.warning(f"[refix] {phone}: delete fail: {e}")

            # Отправляем новое через smart helper (metadata + thumb для видео)
            try:
                await _send_media_smart(client, entity, new_video_path, caption=caption)
                return {
                    "acc_id": acc_id, "phone": phone,
                    "channel_id": ch_id, "ok": True,
                    "deleted_msg_id": first_media_id,
                }
            except Exception as e:
                return {
                    "acc_id": acc_id, "phone": phone, "ok": False,
                    "reason": f"send: {type(e).__name__}: {str(e)[:150]}",
                }
        finally:
            try: await client.disconnect()
            except: pass
            release_account_lock(acc_id)

    prog_key = _refix_progress_key(job_id, "video")
    _state = {"results": []}

    def _pub(status: str, current_phone: str = "", sleeping_sec: int = 0):
        rs = _state["results"]
        def is_final(r):
            if r.get("ok"): return True
            reason = (r.get("reason") or "")
            if "(retry " in reason or "(жду и повторю)" in reason:
                return False
            return True
        finals = [r for r in rs if is_final(r)]
        ok = sum(1 for r in finals if r.get("ok"))
        fail = sum(1 for r in finals if not r.get("ok"))
        _publish_refix_progress(redis, prog_key, {
            "status": status, "job_id": job_id, "kind": "video",
            "total": len(account_ids),
            "done": len(finals),
            "ok": ok, "failed": fail,
            "pending": len(account_ids) - len(finals),
            "current_phone": current_phone,
            "sleeping_sec": sleeping_sec,
            "results": rs[-20:],
            "ts": int(time.time()),
        })

    cancel_key = _refix_cancel_key(job_id, "video")
    try: redis.delete(cancel_key)
    except: pass

    def _is_cancelled() -> bool:
        try: return bool(redis.get(cancel_key))
        except: return False

    _pub("running")

    async def _run():
        await _process_by_proxy_parallel(
            account_ids=account_ids,
            process_one_fn=_process_one,
            state_container=_state,
            publisher_fn=_pub,
            delay_min_sec=delay_min_sec,
            delay_max_sec=delay_max_sec,
            max_attempts=5,
            max_flood_wait_sec=1800,
            max_parallel_proxies=4,
            logger_prefix=f"[refix_vid job={job_id[:8]}]",
            cancel_check_fn=_is_cancelled,
        )

    run_async(_run())

    ok_count = sum(1 for r in _state["results"] if r.get("ok"))
    cancelled = _is_cancelled()
    final = {
        "ok": True,
        "cancelled": cancelled,
        "total_processed": len(_state["results"]),
        "successful": ok_count,
        "failed": len(_state["results"]) - ok_count,
        "results": _state["results"],
    }
    _pub("cancelled" if cancelled else "done")
    try: redis.delete(cancel_key)
    except: pass
    return final


@celery_app.task(bind=True, name="tasks.bulk_tasks.resume_prep_plan_bulk")
def resume_prep_plan_bulk(
    self,
    old_job_id: str,
    new_job_id: str,
    target_duration_sec: int = 3600,
    sessions_per_acc: int = 2,
):
    """Собирает недоделанные (skipped/failed/cancelled) сессии старого
    prep-plan job'а и планирует их в НОВОМ job'е.

    Идея: для каждого акка старого job'а собираем ВСЕ actions из
    сессий со статусами {skipped, failed, failed_permanent, cancelled}.
    Действия из done-сессий пропускаем — они уже выполнились.
    Дальше распределяем эти "долги" по новому набору сессий и запускаем
    как обычный prep_plan.

    Если у акка ВСЕ старые сессии done — акк не попадает в новый job.

    NB: not_authorized сессии тоже пропускаем — сессия акка мертва,
    возобновить бесполезно.
    """
    from utils.redis_pool import get_redis
    from tasks.prep_plan import _split_into_sessions, generate_plan
    redis = get_redis()

    # ── Читаем старый job ──
    old_meta_raw = redis.get(_prep_plan_meta_key(old_job_id))
    if not old_meta_raw:
        return {"ok": False, "reason": "old job meta not found"}
    old_meta = json.loads(old_meta_raw)
    account_ids = old_meta.get("account_ids") or []

    # ── Для каждого акка собираем недоделанные actions ──
    RESUMABLE_STATUSES = {"skipped", "failed", "failed_permanent", "cancelled"}
    acc_todo: list[dict] = []  # [{"acc_id", "phone", "actions"}]
    for acc_id in account_ids:
        old_plan_raw = redis.get(_prep_plan_key(old_job_id, acc_id))
        if not old_plan_raw:
            continue
        old_plan = json.loads(old_plan_raw)
        # Проверка: если хотя бы одна сессия not_authorized — акк мёртв,
        # пропускаем весь акк. Смысла ещё раз к нему коннектиться нет.
        if any(s.get("status") == "not_authorized" for s in old_plan.get("sessions", [])):
            continue
        # Собираем actions из недоделанных сессий
        undone_actions = []
        for s in old_plan.get("sessions", []):
            if s.get("status") in RESUMABLE_STATUSES:
                undone_actions.extend(s.get("actions", []))
        if not undone_actions:
            continue
        acc_todo.append({
            "acc_id": acc_id,
            "phone": old_plan.get("phone", "?"),
            "actions": undone_actions,
        })

    if not acc_todo:
        # Всё уже сделано или все акки мертвы. Мета — минимальная.
        meta = {
            "job_id": new_job_id,
            "resumed_from": old_job_id,
            "status": "done",
            "phase": "done",
            "start_ts": int(time.time()),
            "target_duration_sec": target_duration_sec,
            "sessions_per_acc": sessions_per_acc,
            "total_accounts": 0,
            "account_ids": [],
            "total_sessions_scheduled": 0,
            "total_actions_scheduled": 0,
        }
        redis.setex(_prep_plan_meta_key(new_job_id), 7 * 86400,
                    json.dumps(meta, ensure_ascii=False))
        return {"ok": True, "message": "нечего продолжать — все акки done",
                "accounts_resumed": 0}

    logger.info(
        f"[resume] {old_job_id[:8]} → {new_job_id[:8]}: "
        f"{len(acc_todo)} акков с недоделанными действиями"
    )

    # ── Генерация нового плана ──
    start_ts = time.time()
    total_sessions_scheduled = 0
    total_actions_scheduled = 0
    all_task_ids: list[str] = []

    for entry in acc_todo:
        acc_id = entry["acc_id"]
        phone = entry["phone"]
        actions = entry["actions"]
        plan = generate_plan(
            account_id=acc_id, phone=phone,
            actions=actions,
            target_start_ts=start_ts,
            target_duration_sec=target_duration_sec,
            n_sessions=sessions_per_acc,
        )
        _save_plan(redis, new_job_id, acc_id, plan)
        _save_ctx(redis, new_job_id, acc_id, {})

        for sess in plan["sessions"]:
            countdown = max(0, sess["connect_ts"] - int(time.time()))
            async_result = celery_app.send_task(
                "tasks.bulk_tasks.execute_prep_session",
                args=[new_job_id, acc_id, sess["idx"]],
                queue="bulk_actions",
                countdown=countdown,
            )
            sess["task_id"] = async_result.id
            all_task_ids.append(async_result.id)
            total_sessions_scheduled += 1
            total_actions_scheduled += len(sess.get("actions", []))
            sess["status"] = "scheduled"
        _save_plan(redis, new_job_id, acc_id, plan)

    if all_task_ids:
        redis.setex(
            _prep_plan_task_ids_key(new_job_id), 7 * 86400,
            json.dumps(all_task_ids),
        )

    meta = {
        "job_id": new_job_id,
        "resumed_from": old_job_id,
        "status": "queued",
        "phase": "scheduled",
        "start_ts": int(start_ts),
        "target_duration_sec": target_duration_sec,
        "sessions_per_acc": sessions_per_acc,
        "total_accounts": len(acc_todo),
        "account_ids": [e["acc_id"] for e in acc_todo],
        "total_sessions_scheduled": total_sessions_scheduled,
        "total_actions_scheduled": total_actions_scheduled,
        "config": old_meta.get("config", {}),
    }
    redis.setex(_prep_plan_meta_key(new_job_id), 7 * 86400,
                json.dumps(meta, ensure_ascii=False))
    return {
        "ok": True,
        "message": (
            f"Resume: {len(acc_todo)} акков, "
            f"{total_sessions_scheduled} сессий, "
            f"{total_actions_scheduled} действий на {target_duration_sec // 60} мин"
        ),
        "accounts_resumed": len(acc_todo),
        "total_sessions_scheduled": total_sessions_scheduled,
        "total_actions_scheduled": total_actions_scheduled,
    }


@celery_app.task(bind=True, name="tasks.bulk_tasks.create_channels_bulk")
def create_channels_bulk(self, accounts: list[dict], title_template: str, description: str = "", delay: float = 4.0):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        name = acc.get("first_name", phone)
        title = title_template.replace("{n}", str(i+1)).replace("{name}", name)
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] Канал '{title}'..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return None
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return None
                ch = await _create_channel(client, phone, title, description)
                await client.disconnect()
                return ch
            channel = run_async(do())
            results.append({"phone": phone, "success": bool(channel), "channel": channel})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
        if i < total - 1:
            time.sleep(delay)
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}