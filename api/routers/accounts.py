"""
GramGPT API — routers/accounts.py
Управление Telegram аккаунтами + Telegram-операции с профилем (через прокси).

Мульти-API поддержка:
  - при import-tdata / import-tdata-batch можно передать api_app_id
  - detect-tdata использует публичный api_id=6 (безопасно, сессия не сохраняется)
  - реальное подключение использует либо выбранный api_app, либо публичный android

ИСПРАВЛЕНИЯ:
  - detect-tdata извлекает tg_user_id и first_name локально (без коннекта)
  - import-tdata-batch использует tdata_acc_idx — НЕ берёт первый аккаунт всегда
  - fingerprint считается по tg_user_id (или uuid если не получилось) — не "temp"
  - device_fingerprint НЕ перезаписывается при повторном импорте
  - молчаливый fallback на api_id=6 заменён на явную ошибку
  - bio проверяется через hasattr перед записью
  - TDATA_SESSIONS чистится по TTL даже если юзер забил
"""

import sys
import os
import time
import uuid
import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from database import get_db
from schemas.account import AccountCreate, AccountUpdate, AccountOut, AccountCheckResult

from services import accounts as acc_svc
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy
from models.api_app import ApiApp

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ══════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЕ КРЕДЫ ДЛЯ ДЕТЕКЦИИ И FALLBACK
# Android api_id — самый безопасный и "обычный" для Telegram
# ══════════════════════════════════════════════════════════════

PUBLIC_ANDROID_API_ID = 6
PUBLIC_ANDROID_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"

# TTL для TDATA_SESSIONS in-memory (1 час)
TDATA_SESSION_TTL_SEC = 3600


# ══════════════════════════════════════════════════════════════
# WEB K — реалистичные браузерные fingerprints
# Используются когда api_id=2496 (Telegram Web K)
# ══════════════════════════════════════════════════════════════

WEB_K_DEVICES = [
    {"device": "Chrome 131", "system": "Windows 11",   "app_version": "2.4.0 K"},
    {"device": "Chrome 131", "system": "macOS 15.1",   "app_version": "2.4.0 K"},
    {"device": "Chrome 131", "system": "Linux x86_64", "app_version": "2.4.0 K"},
    {"device": "Firefox 132","system": "Windows 11",   "app_version": "2.4.0 K"},
    {"device": "Firefox 132","system": "Linux x86_64", "app_version": "2.4.0 K"},
    {"device": "Safari 18",  "system": "macOS 15.1",   "app_version": "2.4.0 K"},
    {"device": "Edge 131",   "system": "Windows 11",   "app_version": "2.4.0 K"},
]


def get_web_k_device(seed: str) -> dict:
    """Детерминированный browser-fingerprint для Web K по seed (user_id или auth_key)."""
    h = int(hashlib.md5(str(seed).encode()).hexdigest(), 16)
    return WEB_K_DEVICES[h % len(WEB_K_DEVICES)]


def get_fingerprint_for_import(seed: str, platform: str, api_id: int) -> dict:
    """
    Подбирает fingerprint для импорта.
    Если api_id=2496 (Web K) → реальный браузер.
    Иначе → пул по платформе.
    """
    from utils.telegram import _get_device_for_platform
    if api_id == 2496:
        return get_web_k_device(seed)
    return _get_device_for_platform(seed, platform)


async def _load_api_app_for_import(
    db: AsyncSession, api_app_id: Optional[int], user_id: int
) -> tuple[int, str, str, Optional[int]]:
    """
    Загружает api_app для импорта. Возвращает (api_id, api_hash, platform, api_app_id_to_save).
    Если api_app_id=None → публичный android (api_app_id_to_save=None).
    """
    if not api_app_id:
        print(f"📦 Импорт через публичный api_id={PUBLIC_ANDROID_API_ID} (android)")
        return PUBLIC_ANDROID_API_ID, PUBLIC_ANDROID_API_HASH, "android", None

    app_r = await db.execute(
        select(ApiApp).where(
            ApiApp.id == api_app_id,
            ApiApp.user_id == user_id,
            ApiApp.is_active == True,
        )
    )
    api_app = app_r.scalar_one_or_none()
    if not api_app:
        raise HTTPException(status_code=404, detail="API app не найден или неактивен")

    platform = getattr(api_app, 'platform', 'android') or 'android'
    print(f"📦 Импорт через API app #{api_app.id}: api_id={api_app.api_id}, platform={platform}")
    return api_app.api_id, api_app.api_hash, platform, api_app.id


def _extract_tdata_local_info(account_td) -> dict:
    """
    Извлекает локальную информацию об аккаунте из TData без коннекта к Telegram.
    Возвращает {tg_user_id, first_name, dc_id} — поля могут быть None если не найдено.
    """
    info = {"tg_user_id": None, "first_name": "", "dc_id": None}

    # tg_user_id — есть в opentele.td.Account как .UserId
    try:
        uid = getattr(account_td, 'UserId', None)
        if uid:
            info["tg_user_id"] = int(uid)
    except Exception:
        pass

    # MainDcId
    try:
        dc = getattr(account_td, 'MainDcId', None)
        if dc:
            info["dc_id"] = int(dc)
    except Exception:
        pass

    # first_name — иногда доступно из локального профиля
    try:
        local = getattr(account_td, '_local', None)
        if local:
            fn = getattr(local, 'firstName', None) or getattr(local, 'first_name', None)
            if fn:
                info["first_name"] = str(fn)[:64]
    except Exception:
        pass

    return info


def _cleanup_expired_tdata_sessions():
    """Удаляет просроченные TDATA_SESSIONS и их tmp-папки."""
    import shutil
    now = time.time()
    expired_keys = []
    for sid, data in TDATA_SESSIONS.items():
        if now - data.get("created_at", 0) > TDATA_SESSION_TTL_SEC:
            expired_keys.append(sid)

    for sid in expired_keys:
        data = TDATA_SESSIONS.pop(sid, None)
        if data:
            tmp = data.get("tmp_dir")
            if tmp and os.path.exists(tmp):
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                    print(f"🧹 TDATA_SESSIONS: удалена просроченная {sid} ({tmp})")
                except Exception:
                    pass


def _safe_set_attr(obj, name: str, value):
    """Записать в атрибут только если он существует в модели — иначе тихо пропустить."""
    if hasattr(obj, name):
        try:
            setattr(obj, name, value)
        except Exception as e:
            print(f"⚠ _safe_set_attr({name}): {e}")


# ── CRUD ─────────────────────────────────────────────────────

@router.get("/", response_model=list[AccountOut])
async def list_accounts(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await acc_svc.get_accounts(db, current_user.id)

@router.get("/stats")
async def get_stats(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await acc_svc.get_stats(db, current_user.id)

@router.get("/filters")
async def get_filters(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Уникальные значения гео и категорий для фильтров"""
    result = await db.execute(
        select(TelegramAccount.geo, TelegramAccount.category).where(TelegramAccount.user_id == current_user.id)
    )
    rows = result.all()
    geos = sorted(set(r[0] for r in rows if r[0]))
    categories = sorted(set(r[1] for r in rows if r[1]))
    return {"geos": geos, "categories": categories}

@router.get("/{account_id}", response_model=AccountOut)
async def get_account(account_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await acc_svc.get_account(db, account_id, current_user.id)

@router.post("/", status_code=201)
async def create_account(data: AccountCreate, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    phone = data.phone.strip()
    if not phone.startswith("+"): phone = "+" + phone

    await acc_svc.check_limit(db, current_user)

    # Валидируем api_app_id (должен принадлежать юзеру)
    api_app_id = None
    if data.api_app_id:
        app = (await db.execute(
            select(ApiApp).where(ApiApp.id == data.api_app_id, ApiApp.user_id == current_user.id)
        )).scalar_one_or_none()
        if not app:
            raise HTTPException(status_code=400, detail="API приложение не найдено")
        api_app_id = app.id

    # Валидируем proxy_id (должен принадлежать юзеру)
    proxy_id = None
    if data.proxy_id:
        proxy = (await db.execute(
            select(Proxy).where(Proxy.id == data.proxy_id, Proxy.user_id == current_user.id)
        )).scalar_one_or_none()
        if not proxy:
            raise HTTPException(status_code=400, detail="Прокси не найден")
        proxy_id = proxy.id

    existing = await acc_svc.get_account_by_phone(db, phone, current_user.id)
    if existing:
        # повторное добавление — проставим выбранные api_app/proxy если переданы
        if api_app_id:
            existing.api_app_id = api_app_id
        if proxy_id:
            existing.proxy_id = proxy_id
        await db.flush()
        return {"account_id": existing.id, "phone": phone, "status": existing.status.value,
                "already_exists": True, "next_step": "authorize"}

    account = TelegramAccount(user_id=current_user.id, phone=phone,
                              api_app_id=api_app_id, proxy_id=proxy_id)
    db.add(account)
    await db.flush()
    return {"account_id": account.id, "phone": phone, "status": "pending_auth",
            "api_app_id": api_app_id, "proxy_id": proxy_id, "next_step": "authorize"}

@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(account_id: int, data: AccountUpdate,
                         current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await acc_svc.get_account(db, account_id, current_user.id)
    return await acc_svc.update_account(db, account, data)

@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await acc_svc.get_account(db, account_id, current_user.id)
    await acc_svc.delete_account(db, account)

@router.post("/import-json")
async def import_from_json(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    import json
    from pathlib import Path
    json_path = Path(__file__).parent.parent.parent / "data" / "accounts.json"
    if not json_path.exists():
        return {"detail": "accounts.json не найден", "imported": 0}
    with open(json_path, "r", encoding="utf-8") as f:
        accounts_data = json.load(f)
    imported, errors = 0, []
    for acc_dict in accounts_data:
        try:
            await acc_svc.sync_from_dict(db, current_user, acc_dict)
            imported += 1
        except Exception as e:
            errors.append({"phone": acc_dict.get("phone"), "error": str(e)})
    return {"imported": imported, "errors": errors, "total": len(accounts_data)}


# ── Telegram Profile Operations (через прокси) ───────────────

async def _get_acc_and_client(account_id: int, user_id: int, db: AsyncSession):
    """Загружает аккаунт + создаёт TelegramClient с прокси."""
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == account_id, TelegramAccount.user_id == user_id)
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import make_telethon_client

    proxy = None
    if acc.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    client = make_telethon_client(acc, proxy)
    if not client:
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    return acc, client


class TelegramProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    bio: Optional[str] = None


@router.post("/{account_id}/update-telegram-profile")
async def update_telegram_profile(
    account_id: int,
    body: TelegramProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Обновить имя/фамилию/био в Telegram (через прокси)."""
    acc, client = await _get_acc_and_client(account_id, current_user.id, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.account import UpdateProfileRequest
        kwargs = {}
        if body.first_name is not None: kwargs['first_name'] = body.first_name
        if body.last_name is not None: kwargs['last_name'] = body.last_name
        if body.bio is not None: kwargs['about'] = body.bio[:70]

        if not kwargs:
            await client.disconnect()
            return {"success": True, "message": "Нечего обновлять"}

        await client(UpdateProfileRequest(**kwargs))
        await client.disconnect()

        if body.first_name is not None: acc.first_name = body.first_name
        if body.last_name is not None: acc.last_name = body.last_name
        if body.bio is not None: _safe_set_attr(acc, 'bio', body.bio[:70])
        await db.flush()

        print(f"  ✅ [{acc.phone}] Профиль обновлён в Telegram")
        return {"success": True, "message": "Профиль обновлён в Telegram"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/{account_id}/set-avatar")
async def set_avatar(
    account_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загрузить аватарку в Telegram (через прокси)."""
    acc, client = await _get_acc_and_client(account_id, current_user.id, db)

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    content = await file.read()
    tmp.write(content)
    tmp.close()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        uploaded = await client.upload_file(tmp.name)
        await client(UploadProfilePhotoRequest(file=uploaded))
        await client.disconnect()

        acc.has_photo = True
        await db.flush()

        print(f"  ✅ [{acc.phone}] Аватарка установлена")
        return {"success": True, "message": "Аватарка установлена"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")
    finally:
        try: os.unlink(tmp.name)
        except: pass


class PinChannelRequest(BaseModel):
    channel_link: str


@router.get("/{account_id}/download-session")
async def download_session(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Скачать .session файл аккаунта."""
    from fastapi.responses import FileResponse
    from pathlib import Path

    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.session_file or not Path(acc.session_file).exists():
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    filename = Path(acc.session_file).name
    return FileResponse(acc.session_file, filename=filename, media_type="application/octet-stream")


@router.get("/{account_id}/export-tdata")
async def export_tdata(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Экспорт .session → TData (ZIP)."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    import tempfile, shutil, zipfile

    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.session_file or not Path(acc.session_file).exists():
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    try:
        from opentele.tl import TelegramClient as OpenteleClient
        from opentele.api import UseCurrentSession
    except ImportError:
        raise HTTPException(status_code=500, detail="opentele не установлен. pip install opentele")

    tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_export_")
    tdata_dir = os.path.join(tmp_dir, "tdata")
    zip_path = os.path.join(tmp_dir, f"{acc.phone.replace('+', '')}_tdata.zip")

    try:
        session_path = acc.session_file.replace(".session", "")
        print(f"📦 Экспорт TData: {acc.phone}, session={session_path}")

        client = OpenteleClient(session_path)
        tdesk = await client.ToTDesktop(flag=UseCurrentSession)
        tdesk.SaveTData(tdata_dir)

        print(f"📦 TData сохранён в {tdata_dir}")

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(tdata_dir):
                for f in files:
                    file_path = os.path.join(root, f)
                    arcname = os.path.relpath(file_path, tmp_dir)
                    zf.write(file_path, arcname)

        print(f"📦 ZIP создан: {zip_path}")
        filename = f"{acc.phone.replace('+', '')}_tdata.zip"
        return FileResponse(zip_path, filename=filename, media_type="application/zip")

    except Exception as e:
        print(f"📦 Ошибка экспорта TData: {e}")
        try: shutil.rmtree(tmp_dir)
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка экспорта: {str(e)[:200]}")


# ══════════════════════════════════════════════════════════════
# IMPORT TDATA (одиночный ZIP)
# ══════════════════════════════════════════════════════════════

@router.post("/import-tdata")
async def import_tdata(
    file: UploadFile = File(...),
    proxy_id: int = Form(None),
    api_app_id: int = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт одного TData ZIP → .session → аккаунт с прокси.
    api_app_id определяет какой api_id/platform использовать.
    """
    import tempfile, shutil, zipfile
    from pathlib import Path

    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Загрузите ZIP архив с TData")

    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        raise HTTPException(status_code=500, detail="opentele не установлен. pip install opentele")

    await acc_svc.check_limit(db, current_user)

    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import get_cli_config, _build_proxy, _get_device_for_platform
    cli_config = get_cli_config()

    # Загружаем api_app
    use_api_id, use_api_hash, use_platform, use_api_app_id = await _load_api_app_for_import(
        db, api_app_id, current_user.id
    )

    # Прокси обязателен — проверяем СРАЗУ перед распаковкой
    if not proxy_id:
        raise HTTPException(status_code=400, detail="Прокси обязателен для импорта TData")

    proxy_r = await db.execute(select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id))
    proxy_row = proxy_r.scalar_one_or_none()
    if not proxy_row:
        raise HTTPException(status_code=404, detail=f"Прокси #{proxy_id} не найден")
    proxy_dict = _build_proxy(proxy_row)
    if not proxy_dict:
        raise HTTPException(status_code=400, detail="Не удалось построить прокси")

    print(f"📦 Прокси: {proxy_row.host}:{proxy_row.port}")

    tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_import_")

    try:
        zip_path = os.path.join(tmp_dir, "upload.zip")
        content = await file.read()
        with open(zip_path, "wb") as f:
            f.write(content)

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_dir)

        print(f"📦 TData ZIP распакован в {tmp_dir}")

        # Ищем tdata папку
        tdata_path = None
        for root, dirs, files_list in os.walk(tmp_dir):
            if "tdata" in dirs:
                tdata_path = os.path.join(root, "tdata")
                break
            if any(f.startswith("key_") for f in files_list):
                tdata_path = root
                break

        if not tdata_path:
            if any(f.startswith("key_") for f in os.listdir(tmp_dir)):
                tdata_path = tmp_dir
            else:
                raise HTTPException(status_code=400, detail="В архиве не найдена папка TData")

        print(f"📦 TData найдена: {tdata_path}")

        tdesk = TDesktop(tdata_path)
        if not tdesk.isLoaded():
            raise HTTPException(status_code=400, detail="Не удалось загрузить TData — повреждена или зашифрована")

        print(f"📦 Аккаунтов в TData: {tdesk.accountsCount}")
        account_td = tdesk.accounts[0]

        # Достаём локальную инфу (без коннекта)
        local_info = _extract_tdata_local_info(account_td)
        print(f"📦 Local info: tg_user_id={local_info['tg_user_id']}, "
              f"first_name='{local_info['first_name']}', dc={local_info['dc_id']}")

        # Seed для fingerprint — реальный, не "temp"
        if local_info['tg_user_id']:
            fp_seed = str(local_info['tg_user_id'])
        else:
            fp_seed = f"tdata_{uuid.uuid4().hex[:16]}"

        # Конвертируем TData с правильными api_id/hash
        temp_session = os.path.join(tmp_dir, "temp_session")
        client = await account_td.ToTelethon(
            session=temp_session,
            flag=UseCurrentSession,
            api_id=use_api_id,
            api_hash=use_api_hash,
        )
        try: await client.disconnect()
        except: pass
        import asyncio as _aio
        await _aio.sleep(0.5)

        # Подключаемся через прокси с реалистичным fingerprint от seed
        from telethon import TelegramClient as TelethonClient
        fp = get_fingerprint_for_import(fp_seed, use_platform, use_api_id)
        print(f"📦 Fingerprint: {fp['device']} / {fp['system']} / {fp['app_version']}")

        client = TelethonClient(
            temp_session, use_api_id, use_api_hash,
            proxy=proxy_dict,
            device_model=fp["device"], system_version=fp["system"],
            app_version=fp["app_version"],
            lang_code="en", system_lang_code="en", timeout=30,
        )

        await _aio.wait_for(client.connect(), timeout=45)

        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="TData сессия не авторизована")

        me = await client.get_me()
        real_phone = f"+{me.phone}" if me.phone else ""
        print(f"📦 Авторизован: {me.first_name} ({real_phone})")

        # Загружаем bio пока коннект жив
        bio = ""
        try:
            from telethon.tl.functions.users import GetFullUserRequest
            full = await client(GetFullUserRequest(me))
            bio = full.full_user.about or ""
        except Exception:
            pass

        await client.disconnect()

        if not real_phone:
            raise HTTPException(status_code=400, detail="Не удалось получить номер телефона")

        # Если local_info не дал tg_user_id — пересчитываем fingerprint от real_phone,
        # ОДНАКО Telegram уже видит "fp" (от uuid). Это первый коннект — он палевный
        # только в случае нескольких импортов одновременно с одним uuid (никогда).
        # Сохраняем тот fingerprint, с которым реально коннектились.
        fingerprint_str = f"{fp['device']}|{fp['system']}|{fp['app_version']}"

        # Копируем .session в sessions/
        final_session = str(cli_config.SESSIONS_DIR / real_phone.replace("+", "")) + ".session"
        shutil.copy2(temp_session + ".session", final_session)
        print(f"📦 Session сохранён: {final_session}")

        # Проверяем нет ли уже такого аккаунта
        existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
        if existing:
            existing.session_file = final_session
            existing.status = "active"
            existing.first_name = me.first_name or ""
            existing.last_name = me.last_name or ""
            existing.username = me.username or ""
            _safe_set_attr(existing, 'bio', bio)
            existing.has_photo = bool(me.photo)
            existing.tg_id = me.id
            if proxy_id: existing.proxy_id = proxy_id
            if use_api_app_id: existing.api_app_id = use_api_app_id
            # НЕ перезаписываем fingerprint если он уже есть
            if not existing.device_fingerprint:
                existing.device_fingerprint = fingerprint_str
                print(f"📦 Установлен fingerprint впервые")
            else:
                print(f"📦 Сохраняем существующий fingerprint: {existing.device_fingerprint}")
            await db.flush()
            return {"success": True, "account_id": existing.id, "phone": real_phone,
                    "first_name": me.first_name or "", "message": "Аккаунт обновлён из TData"}

        # Создаём новый аккаунт
        account = TelegramAccount(
            user_id=current_user.id, phone=real_phone,
            session_file=final_session, status="active",
            first_name=me.first_name or "", last_name=me.last_name or "",
            username=me.username or "", has_photo=bool(me.photo),
            tg_id=me.id,
            device_fingerprint=fingerprint_str,
        )
        _safe_set_attr(account, 'bio', bio)
        if proxy_id: account.proxy_id = proxy_id
        if use_api_app_id: account.api_app_id = use_api_app_id
        db.add(account)
        await db.flush()

        print(f"📦 ✅ Аккаунт импортирован: {real_phone} (platform={use_platform}, api_app={use_api_app_id})")
        return {"success": True, "account_id": account.id, "phone": real_phone,
                "first_name": me.first_name or "", "message": "Аккаунт импортирован из TData"}

    except HTTPException: raise
    except Exception as e:
        print(f"📦 ❌ Ошибка импорта TData: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")
    finally:
        try: shutil.rmtree(tmp_dir)
        except: pass


@router.post("/{account_id}/pin-channel")
async def pin_channel_to_profile(
    account_id: int,
    body: PinChannelRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Закрепить канал в профиле аккаунта (через прокси)."""
    acc, client = await _get_acc_and_client(account_id, current_user.id, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        link = body.channel_link
        if link.startswith("@"): link = f"https://t.me/{link[1:]}"
        elif not link.startswith("http"): link = f"https://t.me/{link}"

        entity = await client.get_entity(link)

        from telethon.tl.functions.account import UpdatePersonalChannelRequest
        await client(UpdatePersonalChannelRequest(channel=entity))
        await client.disconnect()

        print(f"  ✅ [{acc.phone}] Канал {link} закреплён в профиле")
        return {"success": True, "message": f"Канал {link} закреплён"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


# ══════════════════════════════════════════════════════════════
# DETECT TDATA (шаг 1) — использует ПУБЛИЧНЫЙ api_id
# Извлекает локальную инфу из TData без коннекта к Telegram.
# ══════════════════════════════════════════════════════════════

TDATA_SESSIONS = {}


@router.post("/detect-tdata")
async def detect_tdata(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Шаг 1: Загрузить ZIP с TData → определить сколько аккаунтов внутри.
    Извлекает tg_user_id и first_name локально из TData без коннекта к Telegram.
    """
    import tempfile, shutil, zipfile

    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Загрузите ZIP архив")

    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        raise HTTPException(status_code=500, detail="opentele не установлен")

    # Чистим просроченные сессии
    _cleanup_expired_tdata_sessions()

    print(f"📦 detect-tdata: используем публичный api_id={PUBLIC_ANDROID_API_ID}")

    tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_batch_")

    try:
        zip_path = os.path.join(tmp_dir, "upload.zip")
        content = await file.read()
        with open(zip_path, "wb") as f:
            f.write(content)

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_dir)

        print(f"📦 Batch TData: распакован в {tmp_dir}")

        tdata_paths_set = set()
        for root, dirs, files_list in os.walk(tmp_dir):
            if any(f.startswith("key_") for f in files_list):
                tdata_paths_set.add(os.path.normpath(root))
            if "tdata" in dirs:
                candidate = os.path.normpath(os.path.join(root, "tdata"))
                tdata_paths_set.add(candidate)

        tdata_paths = sorted(tdata_paths_set)

        if not tdata_paths:
            raise HTTPException(status_code=400, detail="TData папки не найдены в архиве")

        print(f"📦 Найдено TData папок: {len(tdata_paths)}")

        detected = []
        seen_user_ids = set()

        for i, tdata_path in enumerate(tdata_paths):
            try:
                tdesk = TDesktop(tdata_path)
                if not tdesk.isLoaded():
                    print(f"📦 [{i}] TData не загружена: {tdata_path}")
                    continue

                for acc_idx in range(tdesk.accountsCount):
                    account_td = tdesk.accounts[acc_idx]

                    # Локальная инфа без коннекта
                    local_info = _extract_tdata_local_info(account_td)
                    tg_uid = local_info["tg_user_id"]
                    first_name = local_info["first_name"]
                    dc_id = local_info["dc_id"]

                    # Дедупликация по tg_user_id (если есть)
                    if tg_uid and tg_uid in seen_user_ids:
                        print(f"📦 [tg_id={tg_uid}] Дубликат — пропускаю")
                        continue
                    if tg_uid:
                        seen_user_ids.add(tg_uid)

                    # Конвертируем в .session с публичным api_id
                    # (потом если выберут другой — пересоздадим)
                    sess_name = f"tdata_acc_{i}_{acc_idx}"
                    sess_path = os.path.join(tmp_dir, sess_name)

                    try:
                        client = await account_td.ToTelethon(
                            session=sess_path,
                            flag=UseCurrentSession,
                            api_id=PUBLIC_ANDROID_API_ID,
                            api_hash=PUBLIC_ANDROID_API_HASH,
                        )
                        try: await client.disconnect()
                        except: pass
                    except Exception as conv_e:
                        print(f"📦 [{i}/{acc_idx}] ❌ Ошибка конверсии: {conv_e}")
                        continue

                    import asyncio as _aio
                    await _aio.sleep(0.2)

                    detected.append({
                        "index": len(detected),
                        "tg_user_id": tg_uid,
                        "first_name": first_name,
                        "dc_id": dc_id,
                        "session_path": sess_path + ".session",
                        "tdata_folder": tdata_path,
                        "tdata_acc_idx": acc_idx,  # ← КРИТИЧЕСКИ ВАЖНО для re-convert
                    })
                    print(f"📦 [{len(detected)}] tg_id={tg_uid}, name='{first_name}', dc={dc_id}")

            except Exception as e:
                print(f"📦 Ошибка обработки TData {tdata_path}: {e}")

        if not detected:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Не удалось извлечь аккаунты из TData")

        session_id = str(uuid.uuid4())[:8]
        TDATA_SESSIONS[session_id] = {
            "tmp_dir": tmp_dir,
            "accounts": detected,
            "created_at": time.time(),
        }

        print(f"📦 Session ID: {session_id}, аккаунтов: {len(detected)}")

        return {
            "session_id": session_id,
            "accounts": [{
                "index": a["index"],
                "tg_user_id": a["tg_user_id"],
                "name": a["first_name"],
                "first_name": a["first_name"],
                "dc_id": a["dc_id"],
                # Backward compat для старого фронта:
                "phone": str(a["tg_user_id"]) if a["tg_user_id"] else "",
                "username": "",
            } for a in detected],
            "total": len(detected),
        }

    except HTTPException: raise
    except Exception as e:
        try: shutil.rmtree(tmp_dir, ignore_errors=True)
        except: pass
        print(f"📦 ❌ Ошибка detect: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


# ══════════════════════════════════════════════════════════════
# IMPORT TDATA BATCH (шаг 2) — с api_app_id
# ══════════════════════════════════════════════════════════════

class TDataAccountImport(BaseModel):
    index: int
    proxy_string: str = ""
    proxy_id: int | None = None


class TDataBatchImportRequest(BaseModel):
    session_id: str
    accounts: list[TDataAccountImport]
    api_app_id: Optional[int] = None


@router.post("/import-tdata-batch")
async def import_tdata_batch(
    body: TDataBatchImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Шаг 2: Импортировать аккаунты с назначенными прокси и выбранным api_app.

    ИСПРАВЛЕНИЯ:
      - При re-convert берётся ПРАВИЛЬНЫЙ acc_idx из TData (не [0] всегда!)
      - Fingerprint считается по tg_user_id (стабильный, не "temp")
      - При ошибке re-convert — явная ошибка юзеру (не молчаливый fallback на api_id=6)
      - device_fingerprint не перезаписывается при повторном импорте
    """
    import shutil
    from pathlib import Path

    session_data = TDATA_SESSIONS.get(body.session_id)
    if not session_data:
        raise HTTPException(status_code=400, detail="Сессия не найдена или истекла")

    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import get_cli_config, _build_proxy
    cli_config = get_cli_config()

    # Загружаем api_app для всего batch
    use_api_id, use_api_hash, use_platform, use_api_app_id = await _load_api_app_for_import(
        db, body.api_app_id, current_user.id
    )

    results = []

    for item in body.accounts:
        acc_data = next((a for a in session_data["accounts"] if a["index"] == item.index), None)
        if not acc_data:
            results.append({"index": item.index, "success": False, "error": "Аккаунт не найден"})
            continue

        session_path_from_detect = acc_data["session_path"]
        tdata_acc_idx = acc_data.get("tdata_acc_idx", 0)
        tdata_folder = acc_data["tdata_folder"]
        tg_user_id = acc_data.get("tg_user_id")

        try:
            # ── Прокси ──
            proxy_id = item.proxy_id
            proxy_dict = None
            print(f"📦 [{item.index}] proxy_id={proxy_id}, proxy_string='{item.proxy_string}', "
                  f"tdata_acc_idx={tdata_acc_idx}, tg_user_id={tg_user_id}")

            if item.proxy_string and not proxy_id:
                parts = item.proxy_string.strip().split(":")
                if len(parts) >= 2:
                    host = parts[0]
                    port = int(parts[1])
                    login = parts[2] if len(parts) > 2 else ""
                    password = parts[3] if len(parts) > 3 else ""

                    existing_p = await db.execute(
                        select(Proxy).where(Proxy.host == host, Proxy.port == port, Proxy.user_id == current_user.id)
                    )
                    proxy_row = existing_p.scalar_one_or_none()

                    if not proxy_row:
                        proxy_row = Proxy(user_id=current_user.id, host=host, port=port,
                                          login=login, password=password, protocol="socks5")
                        db.add(proxy_row)
                        await db.flush()
                        print(f"📦 Создан прокси: {host}:{port}")

                    proxy_id = proxy_row.id
                    proxy_dict = _build_proxy(proxy_row)

            elif proxy_id:
                proxy_r = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
                proxy_row = proxy_r.scalar_one_or_none()
                if proxy_row:
                    proxy_dict = _build_proxy(proxy_row)

            if not proxy_dict:
                results.append({"index": item.index, "success": False,
                                "error": "Прокси обязателен для импорта"})
                continue

            # ── Re-convert если api_id отличается от публичного ──
            needs_reconvert = (use_api_id != PUBLIC_ANDROID_API_ID)
            sess_base = session_path_from_detect.replace(".session", "")

            if needs_reconvert:
                # Удаляем старый session (от публичного api_id)
                try: os.unlink(session_path_from_detect)
                except: pass
                try: os.unlink(sess_base + ".session-journal")
                except: pass

                # Re-convert TData с нужным api_id ИЗ ПРАВИЛЬНОГО АККАУНТА
                try:
                    from opentele.td import TDesktop
                    from opentele.api import UseCurrentSession
                    tdesk = TDesktop(tdata_folder)

                    if tdata_acc_idx >= tdesk.accountsCount:
                        raise ValueError(f"acc_idx={tdata_acc_idx} >= accountsCount={tdesk.accountsCount}")

                    # ✅ ИСПРАВЛЕНО: берём правильный аккаунт по acc_idx, а не всегда [0]
                    acc_td = tdesk.accounts[tdata_acc_idx]

                    reconv_client = await acc_td.ToTelethon(
                        session=sess_base,
                        flag=UseCurrentSession,
                        api_id=use_api_id,
                        api_hash=use_api_hash,
                    )
                    try: await reconv_client.disconnect()
                    except: pass
                    import asyncio as _aio
                    await _aio.sleep(0.3)
                    print(f"📦 [{item.index}] session пересоздан с api_id={use_api_id}, acc_idx={tdata_acc_idx}")

                except Exception as e:
                    # ✅ ИСПРАВЛЕНО: явная ошибка вместо молчаливого fallback
                    err_msg = (
                        f"Не удалось пересоздать session с api_id={use_api_id}: {str(e)[:120]}. "
                        f"Попробуй другой API ключ (рекомендуется: Telegram Desktop 2040 для TData)."
                    )
                    print(f"📦 [{item.index}] ❌ {err_msg}")
                    results.append({"index": item.index, "success": False, "error": err_msg})
                    continue

            # ── Подключение через прокси с реальным fingerprint ──
            # Seed для fingerprint — tg_user_id (стабильный, не "temp")
            if tg_user_id:
                fp_seed = str(tg_user_id)
            else:
                fp_seed = f"tdata_{item.index}_{uuid.uuid4().hex[:16]}"

            fp = get_fingerprint_for_import(fp_seed, use_platform, use_api_id)
            print(f"📦 [{item.index}] FP: {fp['device']} / {fp['system']} / {fp['app_version']} (seed={fp_seed[:32]})")

            from telethon import TelegramClient as TelethonClient
            import asyncio as _aio
            client = TelethonClient(
                sess_base, use_api_id, use_api_hash,
                proxy=proxy_dict,
                device_model=fp["device"], system_version=fp["system"],
                app_version=fp["app_version"],
                lang_code="en", system_lang_code="en", timeout=30,
            )

            try:
                await _aio.wait_for(client.connect(), timeout=45)
            except _aio.TimeoutError:
                try: await client.disconnect()
                except: pass
                results.append({"index": item.index, "success": False,
                                "error": "Таймаут подключения — проверь прокси"})
                continue

            if not await client.is_user_authorized():
                await client.disconnect()
                results.append({"index": item.index, "success": False, "error": "Сессия не авторизована"})
                continue

            me = await client.get_me()
            real_phone = f"+{me.phone}" if me.phone else ""

            if not real_phone:
                await client.disconnect()
                results.append({"index": item.index, "success": False, "error": "Не удалось получить номер"})
                continue

            # Загружаем bio
            bio = ""
            try:
                from telethon.tl.functions.users import GetFullUserRequest
                full = await client(GetFullUserRequest(me))
                bio = full.full_user.about or ""
            except Exception:
                pass

            await client.disconnect()

            # Сохраняем тот fingerprint, с которым реально коннектились
            fingerprint_str = f"{fp['device']}|{fp['system']}|{fp['app_version']}"

            # Копируем session в sessions/
            final_session = str(cli_config.SESSIONS_DIR / real_phone.replace("+", "")) + ".session"
            shutil.copy2(sess_base + ".session", final_session)

            # ── Сохраняем в БД ──
            existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
            if existing:
                existing.session_file = final_session
                existing.status = "active"
                existing.first_name = me.first_name or ""
                existing.last_name = me.last_name or ""
                existing.username = me.username or ""
                _safe_set_attr(existing, 'bio', bio)
                existing.has_photo = bool(me.photo)
                existing.tg_id = me.id
                existing.proxy_id = proxy_id
                if use_api_app_id:
                    existing.api_app_id = use_api_app_id
                # ✅ ИСПРАВЛЕНО: НЕ перезаписываем fingerprint если уже есть
                if not existing.device_fingerprint:
                    existing.device_fingerprint = fingerprint_str
                    print(f"📦 [{item.index}] Установлен fingerprint впервые")
                else:
                    print(f"📦 [{item.index}] Сохраняем существующий fingerprint")
                await db.flush()
                results.append({"index": item.index, "phone": real_phone, "success": True,
                                "account_id": existing.id, "name": me.first_name or "",
                                "updated": True})
            else:
                account = TelegramAccount(
                    user_id=current_user.id, phone=real_phone, session_file=final_session,
                    status="active", first_name=me.first_name or "", last_name=me.last_name or "",
                    username=me.username or "", has_photo=bool(me.photo), tg_id=me.id,
                    proxy_id=proxy_id,
                    device_fingerprint=fingerprint_str,
                )
                _safe_set_attr(account, 'bio', bio)
                db.add(account)
                await db.flush()

                # Привязываем api_app: если пользователь явно выбрал → его, иначе авто
                if use_api_app_id:
                    account.api_app_id = use_api_app_id
                else:
                    try:
                        from services.api_apps import pick_best_app
                        best_app = await pick_best_app(db, current_user.id)
                        if best_app:
                            account.api_app_id = best_app.id
                    except Exception as e:
                        print(f"📦 [{item.index}] ⚠ pick_best_app failed: {e}")
                await db.flush()

                results.append({"index": item.index, "phone": real_phone, "success": True,
                                "account_id": account.id, "name": me.first_name or ""})

            print(f"📦 ✅ {real_phone} импортирован (platform={use_platform}, api_app={use_api_app_id})")

        except Exception as e:
            print(f"📦 ❌ [{item.index}]: {type(e).__name__}: {e}")
            results.append({"index": item.index, "success": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})

    # Очищаем temp
    try: shutil.rmtree(session_data["tmp_dir"], ignore_errors=True)
    except: pass
    TDATA_SESSIONS.pop(body.session_id, None)

    success = sum(1 for r in results if r.get("success"))
    print(f"📦 Batch импорт: {success}/{len(results)} успешно")

    return {"total": len(results), "success": success, "results": results}


# ── ПОДКЛЮЧЕНИЯ (история + статистика) ──────────────────────

@router.get("/connections/stats-today")
async def get_all_connections_today(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сколько подключений сегодня у каждого аккаунта + последнее время."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from models.account_connection import AccountConnection
    from utils.connection_limiter import MAX_DAILY_CONNECTIONS

    local_now = datetime.utcnow() + timedelta(hours=3)
    today_local = local_now.date()
    today_start_utc = datetime(today_local.year, today_local.month, today_local.day) - timedelta(hours=3)

    accs_r = await db.execute(
        select(TelegramAccount.id).where(TelegramAccount.user_id == current_user.id)
    )
    account_ids = [r[0] for r in accs_r.all()]
    if not account_ids:
        return {}

    result = await db.execute(
        select(
            AccountConnection.account_id,
            func.count(AccountConnection.id).label("cnt"),
            func.max(AccountConnection.connected_at).label("last_at"),
        )
        .where(
            AccountConnection.account_id.in_(account_ids),
            AccountConnection.connected_at >= today_start_utc,
        )
        .group_by(AccountConnection.account_id)
    )

    out = {}
    for row in result.all():
        out[str(row[0])] = {
            "count": row[1],
            "limit": MAX_DAILY_CONNECTIONS,
            "last_at": row[2].isoformat() + "Z" if row[2] else None,
        }
    return out


@router.get("/{account_id}/connections")
async def get_account_connections(
    account_id: int,
    limit: int = 100,
    days: int = 7,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """История подключений аккаунта за последние N дней."""
    from datetime import datetime, timedelta
    from models.account_connection import AccountConnection

    acc = (await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    since = datetime.utcnow() - timedelta(days=days)

    result = await db.execute(
        select(AccountConnection).where(
            AccountConnection.account_id == account_id,
            AccountConnection.connected_at >= since,
        ).order_by(AccountConnection.connected_at.desc()).limit(limit)
    )
    conns = result.scalars().all()

    return [{
        "id": c.id,
        "connected_at": c.connected_at.isoformat() + "Z" if c.connected_at else None,
        "source": c.source,
        "success": c.success,
        "error": c.error,
        "proxy_id": c.proxy_id,
    } for c in conns]


@router.get("/{account_id}/connections/stats")
async def get_account_connections_stats(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика подключений: сегодня, неделя, по источникам."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from models.account_connection import AccountConnection
    from utils.connection_limiter import MAX_DAILY_CONNECTIONS

    acc = (await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    local_now = datetime.utcnow() + timedelta(hours=3)
    today_local = local_now.date()
    today_start_utc = datetime(today_local.year, today_local.month, today_local.day) - timedelta(hours=3)
    week_start = datetime.utcnow() - timedelta(days=7)

    today_count = (await db.execute(
        select(func.count(AccountConnection.id)).where(
            AccountConnection.account_id == account_id,
            AccountConnection.connected_at >= today_start_utc,
        )
    )).scalar() or 0

    week_count = (await db.execute(
        select(func.count(AccountConnection.id)).where(
            AccountConnection.account_id == account_id,
            AccountConnection.connected_at >= week_start,
        )
    )).scalar() or 0

    by_source = (await db.execute(
        select(
            AccountConnection.source,
            func.count(AccountConnection.id),
        ).where(
            AccountConnection.account_id == account_id,
            AccountConnection.connected_at >= today_start_utc,
        ).group_by(AccountConnection.source)
    )).all()

    return {
        "today": today_count,
        "limit": MAX_DAILY_CONNECTIONS,
        "remaining": max(0, MAX_DAILY_CONNECTIONS - today_count),
        "week": week_count,
        "by_source_today": {row[0]: row[1] for row in by_source},
    }


# ═══════════════════════════════════════════════════════════
# DEBUG — что Telegram реально видит про нашу сессию
# ═══════════════════════════════════════════════════════════

@router.get("/{account_id}/debug/telegram-sees")
async def debug_telegram_sees(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Подключается к Telegram и показывает что он видит про нашу сессию."""
    from telethon.tl.functions.account import GetAuthorizationsRequest

    acc, client = await _get_acc_and_client(account_id, current_user.id, db)

    fp_in_db = acc.device_fingerprint or "(none)"
    api_app_info = None
    if acc.api_app:
        api_app_info = {
            "id": acc.api_app.id,
            "title": acc.api_app.title,
            "api_id": acc.api_app.api_id,
            "platform": getattr(acc.api_app, 'platform', 'android'),
        }

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"error": "Сессия не активна", "db_fingerprint": fp_in_db, "api_app": api_app_info}

        result = await client(GetAuthorizationsRequest())

        sessions = []
        current_session = None
        for auth in result.authorizations:
            sess = {
                "hash": str(auth.hash),
                "current": auth.current,
                "device_model": auth.device_model,
                "system_version": auth.system_version,
                "app_name": auth.app_name,
                "app_version": auth.app_version,
                "platform": auth.platform,
                "country": auth.country,
                "region": auth.region,
                "ip": auth.ip,
                "date_created": str(auth.date_created),
                "date_active": str(auth.date_active),
            }
            sessions.append(sess)
            if auth.current:
                current_session = sess

        await client.disconnect()

        analysis = {"match": False, "reason": ""}
        if current_session and fp_in_db and "|" in fp_in_db:
            db_device = fp_in_db.split("|")[0]
            tg_device = current_session["device_model"]
            if db_device == tg_device:
                analysis["match"] = True
                analysis["reason"] = f"OK: БД и Telegram совпадают: {db_device}"
            else:
                analysis["match"] = False
                analysis["reason"] = (
                    f"MISMATCH: в БД '{db_device}', Telegram видит '{tg_device}'. "
                    f"device_model не применился при connect()."
                )

        return {
            "account_id": acc.id,
            "phone": acc.phone,
            "db_fingerprint": fp_in_db,
            "api_app": api_app_info,
            "current_telegram_session": current_session,
            "all_telegram_sessions": sessions,
            "analysis": analysis,
        }

    except Exception as e:
        try: await client.disconnect()
        except: pass
        import traceback
        return {
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-2000:],
            "db_fingerprint": fp_in_db,
            "api_app": api_app_info,
        }