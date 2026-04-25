"""
GramGPT API — routers/tdata.py
Импорт аккаунтов через веб: TData архив и .session файлы.
По ТЗ раздел 2.1: TData-формат, Session-файлы, ручная авторизация.
"""

import asyncio
import os
import sys
import shutil
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.api_app import ApiApp
from services import accounts as acc_svc

router = APIRouter(prefix="/import", tags=["import"])


# ── Helpers ──────────────────────────────────────────────────

def _get_cli_config():
    """Загружает корневой config.py с API_ID/API_HASH"""
    import importlib.util
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    config_path = os.path.join(root_dir, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config


def _get_sessions_dir():
    """Путь к папке sessions"""
    cli_config = _get_cli_config()
    return cli_config.SESSIONS_DIR


# ── Session file import ──────────────────────────────────────

# ── Helpers (новые) ──────────────────────────────────────────

async def _resolve_proxy(db, proxy_id: int, user_id: int):
    """Загружает прокси по ID и возвращает (proxy_row, proxy_dict)."""
    from sqlalchemy import select as _sel
    from models.proxy import Proxy as _Proxy
    from routers.tg_auth import _make_proxy

    proxy_r = await db.execute(
        _sel(_Proxy).where(_Proxy.id == proxy_id, _Proxy.user_id == user_id)
    )
    proxy_row = proxy_r.scalar_one_or_none()
    if not proxy_row:
        raise HTTPException(status_code=404, detail=f"Прокси #{proxy_id} не найден")
    return proxy_row, _make_proxy(proxy_row)


async def _resolve_api_app(db, api_app_id: Optional[int], user_id: int):
    """
    Загружает api_app. Возвращает (api_id, api_hash, platform, api_app_id_to_save).
    Если api_app_id=None → fallback на глобальный config + api_app_id_to_save=None.
    """
    if not api_app_id:
        cli_config = _get_cli_config()
        return cli_config.API_ID, cli_config.API_HASH, "android", None

    from sqlalchemy import select as _sel
    app_r = await db.execute(
        _sel(ApiApp).where(
            ApiApp.id == api_app_id,
            ApiApp.user_id == user_id,
            ApiApp.is_active == True,
        )
    )
    api_app = app_r.scalar_one_or_none()
    if not api_app:
        raise HTTPException(status_code=404, detail="API app не найден или неактивен")

    platform = getattr(api_app, 'platform', 'android') or 'android'
    return api_app.api_id, api_app.api_hash, platform, api_app.id


# ── Session file import (одиночный) ──────────────────────────

@router.post("/session")
async def import_session_file(
    file: UploadFile = File(...),
    phone: str = Form(""),
    proxy_id: int = Form(...),                 # ← теперь ОБЯЗАТЕЛЕН
    api_app_id: Optional[int] = Form(None),    # ← НОВОЕ
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт .session файла (Telethon/Pyrogram).
    Проверяет сессию через выбранный прокси + api_app, сохраняет в БД.
    """
    if not file.filename.endswith(".session"):
        raise HTTPException(status_code=400, detail="Файл должен быть .session формата")

    await acc_svc.check_limit(db, current_user)

    # 1. Загружаем прокси
    proxy_row, proxy_dict = await _resolve_proxy(db, proxy_id, current_user.id)
    if not proxy_dict:
        raise HTTPException(status_code=400, detail="Не удалось построить прокси")

    # 2. Загружаем api_app (или fallback на global)
    api_id_use, api_hash_use, platform_use, api_app_id_save = await _resolve_api_app(
        db, api_app_id, current_user.id
    )

    sessions_dir = _get_sessions_dir()

    # Имя файла: либо явный phone, либо берём из имени файла
    if phone:
        clean_phone = phone.strip().replace("+", "")
    else:
        clean_phone = file.filename.replace(".session", "").replace("+", "")

    session_path = sessions_dir / f"{clean_phone}.session"

    # Сохраняем файл
    content = await file.read()
    with open(session_path, "wb") as f:
        f.write(content)

    # Подключаемся через Telethon с правильным device fingerprint для платформы
    from telethon import TelegramClient
    from utils.telegram import _get_device_for_platform

    fp = _get_device_for_platform(clean_phone, platform_use)
    print(f"📄 Session import: api_id={api_id_use}, platform={platform_use}, device={fp['device']}")

    client = TelegramClient(
        str(session_path).replace(".session", ""),
        api_id_use, api_hash_use,
        proxy=proxy_dict,
        device_model=fp["device"],
        system_version=fp["system"],
        app_version=fp["app_version"],
        lang_code="en", system_lang_code="en",
        timeout=30,
    )

    try:
        await asyncio.wait_for(client.connect(), timeout=45)

        if not await client.is_user_authorized():
            await client.disconnect()
            try:
                Path(session_path).unlink(missing_ok=True)
            except Exception:
                pass
            raise HTTPException(
                status_code=400,
                detail="Сессия не авторизована. Возможно файл повреждён или сессия истекла."
            )

        me = await client.get_me()
        await client.disconnect()

        real_phone = f"+{me.phone}" if me.phone else f"+{clean_phone}"

        # Переименовываем session по реальному телефону, если отличается
        correct_path = sessions_dir / f"{me.phone}.session"
        if me.phone and session_path != correct_path:
            try:
                shutil.move(str(session_path), str(correct_path))
                session_path = correct_path
            except Exception:
                pass

        # Device fingerprint для сохранения
        device_fp = f"{fp['device']}|{fp['system']}|{fp['app_version']}"

        # Дубликат?
        existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
        if existing:
            existing.session_file = str(session_path)
            existing.status = "active"
            existing.first_name = me.first_name or existing.first_name
            existing.last_name = me.last_name or existing.last_name
            existing.username = me.username or existing.username
            existing.has_photo = bool(me.photo)
            existing.tg_id = me.id
            existing.proxy_id = proxy_id
            if api_app_id_save:
                existing.api_app_id = api_app_id_save
            existing.device_fingerprint = device_fp
            await db.flush()
            return {
                "success": True,
                "account_id": existing.id,
                "phone": real_phone,
                "first_name": me.first_name or "",
                "username": me.username or "",
                "status": "active",
                "already_existed": True,
                "message": f"Аккаунт {real_phone} обновлён",
            }

        # Новый
        account = TelegramAccount(
            user_id=current_user.id,
            phone=real_phone,
            tg_id=me.id,
            first_name=me.first_name or "",
            last_name=me.last_name or "",
            username=me.username or "",
            has_photo=bool(me.photo),
            session_file=str(session_path),
            status="active",
            trust_score=50,
            proxy_id=proxy_id,
            api_app_id=api_app_id_save,
            device_fingerprint=device_fp,
        )
        db.add(account)
        await db.flush()

        return {
            "success": True,
            "account_id": account.id,
            "phone": real_phone,
            "first_name": me.first_name or "",
            "username": me.username or "",
            "status": "active",
            "message": f"Аккаунт {real_phone} импортирован",
        }

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        try: await client.disconnect()
        except: pass
        try: Path(session_path).unlink(missing_ok=True)
        except: pass
        raise HTTPException(status_code=504, detail="Таймаут подключения. Проверь прокси.")
    except Exception as e:
        try: await client.disconnect()
        except: pass
        try: Path(session_path).unlink(missing_ok=True)
        except: pass
        err = str(e)
        print(f"📄 ❌ Session import error: {type(e).__name__}: {err}")
        if "AUTH_KEY" in err.upper() or "UNREGISTERED" in err.upper():
            raise HTTPException(status_code=400, detail="Auth key не валиден — сессия мёртвая")
        raise HTTPException(status_code=500, detail=f"Ошибка: {err[:200]}")


# ── Batch session import (несколько файлов сразу) ────────────

@router.post("/sessions-batch")
async def import_session_files_batch(
    files: list[UploadFile] = File(...),
    proxy_id: int = Form(...),                 # ← ОБЯЗАТЕЛЕН (один прокси на всю пачку)
    api_app_id: Optional[int] = Form(None),    # ← НОВОЕ
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Пакетный импорт нескольких .session файлов.
    Все файлы получают один и тот же proxy_id + api_app_id.

    Возвращает:
      - imported: количество успешных
      - errors: список ошибок
      - results: детали по каждому файлу
    """
    if not files:
        raise HTTPException(status_code=400, detail="Нет файлов")

    await acc_svc.check_limit(db, current_user)

    proxy_row, proxy_dict = await _resolve_proxy(db, proxy_id, current_user.id)
    if not proxy_dict:
        raise HTTPException(status_code=400, detail="Не удалось построить прокси")

    api_id_use, api_hash_use, platform_use, api_app_id_save = await _resolve_api_app(
        db, api_app_id, current_user.id
    )

    sessions_dir = _get_sessions_dir()

    from telethon import TelegramClient
    from utils.telegram import _get_device_for_platform

    results = []
    imported = 0
    errors = []

    for file in files:
        if not file.filename.endswith(".session"):
            errors.append({"file": file.filename, "error": "Не .session файл"})
            results.append({"file": file.filename, "status": "error", "error": "Не .session файл"})
            continue

        try:
            clean_phone = file.filename.replace(".session", "").replace("+", "")
            session_path = sessions_dir / f"{clean_phone}.session"

            content = await file.read()
            with open(session_path, "wb") as f:
                f.write(content)

            fp = _get_device_for_platform(clean_phone, platform_use)
            device_fp = f"{fp['device']}|{fp['system']}|{fp['app_version']}"

            client = TelegramClient(
                str(session_path).replace(".session", ""),
                api_id_use, api_hash_use,
                proxy=proxy_dict,
                device_model=fp["device"],
                system_version=fp["system"],
                app_version=fp["app_version"],
                lang_code="en", system_lang_code="en",
                timeout=30,
            )

            real_phone = f"+{clean_phone}"
            status = "unknown"
            tg_id = None
            first_name = ""
            last_name = ""
            username = ""
            has_photo = False

            try:
                await asyncio.wait_for(client.connect(), timeout=45)

                if not await client.is_user_authorized():
                    await client.disconnect()
                    try: Path(session_path).unlink(missing_ok=True)
                    except: pass
                    errors.append({"file": file.filename, "error": "Сессия не авторизована"})
                    results.append({"file": file.filename, "phone": real_phone, "status": "error", "error": "Не авторизована"})
                    continue

                me = await client.get_me()
                status = "active"
                tg_id = me.id
                first_name = me.first_name or ""
                last_name = me.last_name or ""
                username = me.username or ""
                has_photo = bool(me.photo)
                if me.phone:
                    real_phone = f"+{me.phone}"
                    correct_path = sessions_dir / f"{me.phone}.session"
                    if session_path != correct_path:
                        try:
                            await client.disconnect()
                            shutil.move(str(session_path), str(correct_path))
                            session_path = correct_path
                        except Exception:
                            pass
                await client.disconnect()
            except asyncio.TimeoutError:
                try: await client.disconnect()
                except: pass
                errors.append({"file": file.filename, "error": "Таймаут — прокси не отвечает"})
                results.append({"file": file.filename, "status": "error", "error": "Таймаут"})
                continue
            except Exception as e:
                try: await client.disconnect()
                except: pass
                err = str(e)
                errors.append({"file": file.filename, "error": err[:150]})
                results.append({"file": file.filename, "status": "error", "error": err[:150]})
                continue

            # Сохраняем в БД
            existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
            if existing:
                existing.session_file = str(session_path)
                existing.status = status
                existing.first_name = first_name or existing.first_name
                existing.last_name = last_name or existing.last_name
                existing.username = username or existing.username
                existing.has_photo = has_photo
                existing.tg_id = tg_id
                existing.proxy_id = proxy_id
                if api_app_id_save:
                    existing.api_app_id = api_app_id_save
                existing.device_fingerprint = device_fp
                imported += 1
                results.append({
                    "file": file.filename, "phone": real_phone, "status": status,
                    "account_id": existing.id, "updated": True,
                })
            else:
                account = TelegramAccount(
                    user_id=current_user.id,
                    phone=real_phone,
                    tg_id=tg_id,
                    first_name=first_name,
                    last_name=last_name,
                    username=username,
                    has_photo=has_photo,
                    session_file=str(session_path),
                    status=status,
                    trust_score=50,
                    proxy_id=proxy_id,
                    api_app_id=api_app_id_save,
                    device_fingerprint=device_fp,
                )
                db.add(account)
                await db.flush()
                imported += 1
                results.append({
                    "file": file.filename, "phone": real_phone, "status": status,
                    "account_id": account.id, "updated": False,
                })

        except Exception as e:
            errors.append({"file": file.filename, "error": str(e)[:150]})
            results.append({"file": file.filename, "status": "error", "error": str(e)[:150]})

    await db.flush()

    return {
        "imported": imported,
        "total": len(files),
        "errors": errors,
        "results": results,
        "message": f"Импортировано {imported}/{len(files)}. Ошибок: {len(errors)}",
    }

# ── TData import ─────────────────────────────────────────────

@router.post("/tdata")
async def import_tdata_archive(
    file: UploadFile = File(...),
    phone: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт TData архива (ZIP файл содержащий папку tdata).
    По ТЗ: TData-формат — загрузка архива папки Telegram Desktop.

    Процесс:
    1. Пользователь загружает ZIP с TData
    2. Распаковываем во временную папку
    3. Конвертируем TData → .session через opentele/telethon-tdata
    4. Подключаемся к TG, получаем данные аккаунта
    5. Сохраняем в БД
    """
    await acc_svc.check_limit(db, current_user)

    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Загрузите ZIP архив с TData папкой")

    # Сохраняем во временную директорию
    tmp_dir = tempfile.mkdtemp(prefix="gramgpt_tdata_")

    try:
        zip_path = os.path.join(tmp_dir, "tdata.zip")
        content = await file.read()
        with open(zip_path, "wb") as f:
            f.write(content)

        # Распаковываем
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(tmp_dir)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Некорректный ZIP файл")

        # Ищем папку tdata внутри
        tdata_path = None
        for root, dirs, files_list in os.walk(tmp_dir):
            if "tdata" in dirs:
                tdata_path = os.path.join(root, "tdata")
                break
            # Может быть что сама папка и есть tdata
            if any(f.startswith("key_") for f in files_list):
                tdata_path = root
                break

        if not tdata_path:
            # Может ZIP содержит файлы tdata прямо в корне
            if any(f.startswith("key_") for f in os.listdir(tmp_dir)):
                tdata_path = tmp_dir
            else:
                raise HTTPException(
                    status_code=400,
                    detail="В архиве не найдена папка TData. Убедитесь что ZIP содержит папку с файлами key_datas, map и т.д.",
                )

        # Вызываем CLI-модуль tdata_importer
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)

        # Безопасный импорт CLI-модулей
        api_config_cache = sys.modules.pop('config', None)
        for mod in ['config', 'ui', 'trust', 'tdata_importer', 'db', 'tg_client']:
            sys.modules.pop(mod, None)

        try:
            import tdata_importer as tdata_mod
            account_dict = await tdata_mod.import_tdata(tdata_path, phone)
        finally:
            if api_config_cache:
                sys.modules['config'] = api_config_cache

        if not account_dict:
            raise HTTPException(
                status_code=400,
                detail="Не удалось импортировать TData. Проверьте что архив корректный и установлена библиотека opentele (pip install opentele).",
            )

        # Сохраняем в БД
        real_phone = account_dict.get("phone", phone)

        existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
        if existing:
            existing.session_file = account_dict.get("session_file", existing.session_file)
            existing.status = "active"
            existing.first_name = account_dict.get("first_name", existing.first_name)
            existing.username = account_dict.get("username", existing.username)
            existing.has_photo = account_dict.get("has_photo", existing.has_photo)
            existing.tg_id = account_dict.get("id")
            existing.trust_score = account_dict.get("trust_score", existing.trust_score)
            await db.flush()

            return {
                "success": True,
                "account_id": existing.id,
                "phone": real_phone,
                "first_name": account_dict.get("first_name", ""),
                "username": account_dict.get("username", ""),
                "status": "active",
                "message": f"TData: аккаунт {real_phone} обновлён",
                "already_existed": True,
            }

        account = TelegramAccount(
            user_id=current_user.id,
            phone=real_phone,
            tg_id=account_dict.get("id"),
            first_name=account_dict.get("first_name", ""),
            last_name=account_dict.get("last_name", ""),
            username=account_dict.get("username", ""),
            has_photo=account_dict.get("has_photo", False),
            session_file=account_dict.get("session_file", ""),
            status="active",
            trust_score=account_dict.get("trust_score", 50),
        )
        db.add(account)
        await db.flush()

        # Auto-create ApiApp from TData's original API credentials
        if account_dict.get("tdata_api_id"):
            try:
                api_app = ApiApp(
                    user_id=current_user.id,
                    api_id=account_dict["tdata_api_id"],
                    api_hash=account_dict.get("tdata_api_hash", ""),
                    title=f"TData import ({real_phone})",
                )
                db.add(api_app)
                await db.flush()
                account.api_app_id = api_app.id
                await db.flush()
            except Exception:
                pass  # Non-critical: account works without dedicated ApiApp

        return {
            "success": True,
            "account_id": account.id,
            "phone": real_phone,
            "first_name": account_dict.get("first_name", ""),
            "username": account_dict.get("username", ""),
            "status": "active",
            "message": f"TData: аккаунт {real_phone} успешно импортирован",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка импорта TData: {str(e)}")
    finally:
        # Чистим временные файлы
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except:
            pass
