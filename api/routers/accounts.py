"""
GramGPT API — routers/accounts.py
Управление Telegram аккаунтами + Telegram-операции с профилем (через прокси).
"""

import sys
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from schemas.account import AccountCreate, AccountUpdate, AccountOut, AccountCheckResult
from services import accounts as acc_svc
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy

router = APIRouter(prefix="/accounts", tags=["accounts"])


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
    existing = await acc_svc.get_account_by_phone(db, phone, current_user.id)
    if existing:
        return {"account_id": existing.id, "phone": phone, "status": existing.status.value,
                "already_exists": True, "next_step": "authorize"}

    account = TelegramAccount(user_id=current_user.id, phone=phone)
    db.add(account)
    await db.flush()
    return {"account_id": account.id, "phone": phone, "status": "pending_auth", "next_step": "authorize"}

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
        select(TelegramAccount).where(TelegramAccount.id == account_id, TelegramAccount.user_id == user_id)
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

        # Обновляем в БД тоже
        if body.first_name is not None: acc.first_name = body.first_name
        if body.last_name is not None: acc.last_name = body.last_name
        if body.bio is not None: acc.bio = body.bio[:70]
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

    # Сохраняем файл временно
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
        select(TelegramAccount).where(TelegramAccount.id == account_id, TelegramAccount.user_id == current_user.id)
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
    """Экспорт .session → TData (ZIP). Скачивается архив с tdata папкой."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    import tempfile, shutil, zipfile

    result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == account_id, TelegramAccount.user_id == current_user.id)
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

        # Архивируем
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


@router.post("/import-tdata")
async def import_tdata(
    file: UploadFile = File(...),
    proxy_id: int = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт TData (ZIP) → .session → аккаунт с прокси.
    1. Загружает ZIP с tdata
    2. Конвертирует tdata → .session через opentele
    3. Подключается через прокси
    4. Сохраняет аккаунт в БД
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

    # Получаем sessions dir
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import get_cli_config, _build_proxy
    cli_config = get_cli_config()

    tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_import_")

    try:
        # 1. Сохраняем ZIP
        zip_path = os.path.join(tmp_dir, "upload.zip")
        content = await file.read()
        with open(zip_path, "wb") as f:
            f.write(content)

        # 2. Распаковываем
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_dir)

        print(f"📦 TData ZIP распакован в {tmp_dir}")

        # 3. Ищем tdata папку
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
        print(f"📦 proxy_id получен: {proxy_id}")

        # 4. Конвертируем TData → Telethon session
        tdesk = TDesktop(tdata_path)
        if not tdesk.isLoaded():
            raise HTTPException(status_code=400, detail="Не удалось загрузить TData — повреждена или зашифрована")

        print(f"📦 Аккаунтов в TData: {tdesk.accountsCount}")
        account_td = tdesk.accounts[0]

        # Временная сессия
        temp_session = os.path.join(tmp_dir, "temp_session")
        from opentele.tl import TelegramClient as OpenteleClient
        client = await account_td.ToTelethon(
            session=temp_session,
            flag=UseCurrentSession,
            api_id=cli_config.API_ID,
            api_hash=cli_config.API_HASH,
        )

        # 5. Закрываем opentele клиент (разблокируем SQLite)
        try: await client.disconnect()
        except: pass
        import asyncio as _aio
        await _aio.sleep(0.5)

        # 6. Подключаемся чистым Telethon клиентом (с прокси если указан)
        proxy_dict = None
        if proxy_id:
            proxy_r = await db.execute(select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id))
            proxy_row = proxy_r.scalar_one_or_none()
            if proxy_row:
                proxy_dict = _build_proxy(proxy_row)
                print(f"📦 Прокси: {proxy_row.host}:{proxy_row.port}")

        from telethon import TelegramClient as TelethonClient
        client = TelethonClient(
            temp_session, cli_config.API_ID, cli_config.API_HASH,
            proxy=proxy_dict,
            device_model="Desktop", system_version="Windows 10", app_version="4.14.15",
            lang_code="ru", system_lang_code="ru", timeout=30,
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="TData сессия не авторизована")

        me = await client.get_me()
        real_phone = f"+{me.phone}" if me.phone else ""
        print(f"📦 Авторизован: {me.first_name} ({real_phone})")
        await client.disconnect()

        if not real_phone:
            raise HTTPException(status_code=400, detail="Не удалось получить номер телефона")

        # 6. Копируем .session в sessions/
        final_session = str(cli_config.SESSIONS_DIR / real_phone.replace("+", "")) + ".session"
        shutil.copy2(temp_session + ".session", final_session)
        print(f"📦 Session сохранён: {final_session}")

        # 7. Проверяем нет ли уже такого аккаунта
        existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
        if existing:
            existing.session_file = final_session
            existing.status = "active"
            existing.first_name = me.first_name or ""
            existing.last_name = me.last_name or ""
            existing.username = me.username or ""
            existing.has_photo = bool(me.photo)
            if proxy_id: existing.proxy_id = proxy_id
            await db.flush()
            return {"success": True, "account_id": existing.id, "phone": real_phone,
                    "first_name": me.first_name or "", "message": "Аккаунт обновлён из TData"}

        # 8. Создаём новый аккаунт
        account = TelegramAccount(
            user_id=current_user.id, phone=real_phone,
            session_file=final_session, status="active",
            first_name=me.first_name or "", last_name=me.last_name or "",
            username=me.username or "", has_photo=bool(me.photo),
            tg_id=me.id,
        )
        if proxy_id: account.proxy_id = proxy_id
        db.add(account)
        await db.flush()

        print(f"📦 ✅ Аккаунт импортирован: {real_phone}")
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


# ── Пакетный TData импорт ────────────────────────────────────

TDATA_SESSIONS = {}  # session_id → {tmp_dir, accounts: [{index, phone, name, session_path}]}


@router.post("/detect-tdata")
async def detect_tdata(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Шаг 1: Загрузить ZIP с TData → определить сколько аккаунтов внутри.
    Возвращает session_id + список аккаунтов для назначения прокси.
    """
    import tempfile, shutil, zipfile, uuid
    from pathlib import Path

    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Загрузите ZIP архив")

    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        raise HTTPException(status_code=500, detail="opentele не установлен")

    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import get_cli_config
    cli_config = get_cli_config()

    tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_batch_")

    try:
        # Распаковываем
        zip_path = os.path.join(tmp_dir, "upload.zip")
        content = await file.read()
        with open(zip_path, "wb") as f:
            f.write(content)

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_dir)

        print(f"📦 Batch TData: распакован в {tmp_dir}")

        # Ищем все tdata папки (дедупликация)
        tdata_paths_set = set()
        for root, dirs, files_list in os.walk(tmp_dir):
            if any(f.startswith("key_") for f in files_list):
                tdata_paths_set.add(os.path.normpath(root))
            if "tdata" in dirs:
                candidate = os.path.normpath(os.path.join(root, "tdata"))
                tdata_paths_set.add(candidate)

        # Убираем вложенные (если /a/tdata и /a/tdata оба найдены)
        tdata_paths = sorted(tdata_paths_set)

        if not tdata_paths:
            raise HTTPException(status_code=400, detail="TData папки не найдены в архиве")

        print(f"📦 Найдено TData папок: {len(tdata_paths)}")

        # Конвертируем каждую в .session
        detected = []
        seen_phones = set()
        for i, tdata_path in enumerate(tdata_paths):
            try:
                tdesk = TDesktop(tdata_path)
                if not tdesk.isLoaded():
                    print(f"📦 [{i}] TData не загружена: {tdata_path}")
                    continue

                for acc_idx in range(tdesk.accountsCount):
                    account_td = tdesk.accounts[acc_idx]
                    sess_name = f"tdata_acc_{i}_{acc_idx}"
                    sess_path = os.path.join(tmp_dir, sess_name)

                    from opentele.tl import TelegramClient as OpenteleClient
                    client = await account_td.ToTelethon(
                        session=sess_path,
                        flag=UseCurrentSession,
                        api_id=cli_config.API_ID,
                        api_hash=cli_config.API_HASH,
                    )

                    # Быстро проверяем — авторизован ли
                    phone = ""
                    name = ""
                    username = ""
                    try:
                        await client.connect()
                        if await client.is_user_authorized():
                            me = await client.get_me()
                            phone = f"+{me.phone}" if me.phone else ""
                            name = me.first_name or ""
                            username = me.username or ""
                        await client.disconnect()
                    except:
                        try: await client.disconnect()
                        except: pass

                    import asyncio as _aio
                    await _aio.sleep(0.3)

                    # Дедупликация по телефону
                    if phone and phone in seen_phones:
                        print(f"📦 [{phone}] Дубликат — пропускаю")
                        continue
                    if phone:
                        seen_phones.add(phone)

                    detected.append({
                        "index": len(detected),
                        "phone": phone,
                        "name": name,
                        "username": username,
                        "session_path": sess_path + ".session",
                        "tdata_folder": tdata_path,
                    })
                    print(f"📦 [{len(detected)}] Найден: {phone} ({name})")

            except Exception as e:
                print(f"📦 Ошибка обработки TData {tdata_path}: {e}")

        if not detected:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Не удалось извлечь аккаунты из TData")

        # Сохраняем в памяти
        session_id = str(uuid.uuid4())[:8]
        TDATA_SESSIONS[session_id] = {"tmp_dir": tmp_dir, "accounts": detected}

        print(f"📦 Session ID: {session_id}, аккаунтов: {len(detected)}")

        return {
            "session_id": session_id,
            "accounts": [{"index": a["index"], "phone": a["phone"], "name": a["name"],
                          "username": a["username"]} for a in detected],
            "total": len(detected),
        }

    except HTTPException: raise
    except Exception as e:
        try: shutil.rmtree(tmp_dir, ignore_errors=True)
        except: pass
        print(f"📦 ❌ Ошибка detect: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


class TDataAccountImport(BaseModel):
    index: int
    proxy_string: str = ""  # "ip:port:login:password" или пусто
    proxy_id: int | None = None


class TDataBatchImportRequest(BaseModel):
    session_id: str
    accounts: list[TDataAccountImport]


@router.post("/import-tdata-batch")
async def import_tdata_batch(
    body: TDataBatchImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Шаг 2: Импортировать аккаунты с назначенными прокси.
    Для каждого аккаунта: подключается через прокси, сохраняет в БД.
    Если proxy_string — создаёт новый прокси в БД.
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

    results = []

    for item in body.accounts:
        acc_data = next((a for a in session_data["accounts"] if a["index"] == item.index), None)
        if not acc_data:
            results.append({"index": item.index, "success": False, "error": "Аккаунт не найден"})
            continue

        phone = acc_data["phone"]
        session_path = acc_data["session_path"]

        if not phone:
            results.append({"index": item.index, "success": False, "error": "Не удалось определить телефон"})
            continue

        try:
            # Определяем прокси
            proxy_id = item.proxy_id
            proxy_dict = None
            print(f"📦 [{phone}] proxy_id={proxy_id}, proxy_string='{item.proxy_string}'")

            # Если передана строка прокси — парсим и создаём в БД
            if item.proxy_string and not proxy_id:
                parts = item.proxy_string.strip().split(":")
                if len(parts) >= 2:
                    from models.proxy import Proxy as ProxyModel
                    host = parts[0]
                    port = int(parts[1])
                    login = parts[2] if len(parts) > 2 else ""
                    password = parts[3] if len(parts) > 3 else ""

                    # Проверяем нет ли уже такого
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

            # Копируем session в sessions/
            print(f"📦 [{phone}] итоговый proxy_id={proxy_id}")
            final_session = str(cli_config.SESSIONS_DIR / phone.replace("+", "")) + ".session"
            shutil.copy2(session_path, final_session)

            # Подключаемся через прокси
            from telethon import TelegramClient as TelethonClient
            sess = final_session.replace(".session", "")
            client = TelethonClient(
                sess, cli_config.API_ID, cli_config.API_HASH,
                proxy=proxy_dict,
                device_model="Desktop", system_version="Windows 10", app_version="4.14.15",
                lang_code="ru", system_lang_code="ru", timeout=30,
            )

            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                results.append({"index": item.index, "phone": phone, "success": False, "error": "Сессия не авторизована"})
                continue

            me = await client.get_me()

            # Загружаем bio
            bio = ""
            try:
                from telethon.tl.functions.users import GetFullUserRequest
                full = await client(GetFullUserRequest(me))
                bio = full.full_user.about or ""
            except:
                pass

            await client.disconnect()

            # Сохраняем в БД
            existing = await acc_svc.get_account_by_phone(db, phone, current_user.id)
            if existing:
                existing.session_file = final_session
                existing.status = "active"
                existing.first_name = me.first_name or ""
                existing.last_name = me.last_name or ""
                existing.username = me.username or ""
                existing.bio = bio
                existing.has_photo = bool(me.photo)
                existing.tg_id = me.id
                existing.proxy_id = proxy_id  # Всегда назначаем (даже None)
                await db.flush()
                results.append({"index": item.index, "phone": phone, "success": True,
                                "account_id": existing.id, "name": me.first_name or ""})
            else:
                account = TelegramAccount(
                    user_id=current_user.id, phone=phone, session_file=final_session,
                    status="active", first_name=me.first_name or "", last_name=me.last_name or "",
                    username=me.username or "", bio=bio, has_photo=bool(me.photo), tg_id=me.id,
                    proxy_id=proxy_id,
                )
                db.add(account)
                await db.flush()
                results.append({"index": item.index, "phone": phone, "success": True,
                                "account_id": account.id, "name": me.first_name or ""})

            print(f"📦 ✅ {phone} импортирован")

        except Exception as e:
            print(f"📦 ❌ {phone}: {e}")
            results.append({"index": item.index, "phone": phone, "success": False, "error": str(e)[:200]})

    # Очищаем temp
    try: shutil.rmtree(session_data["tmp_dir"], ignore_errors=True)
    except: pass
    TDATA_SESSIONS.pop(body.session_id, None)

    success = sum(1 for r in results if r.get("success"))
    print(f"📦 Batch импорт: {success}/{len(results)} успешно")

    return {"total": len(results), "success": success, "results": results}