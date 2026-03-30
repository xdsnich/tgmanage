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

@router.post("/session")
async def import_session_file(
    file: UploadFile = File(...),
    phone: str = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт .session файла (Telethon/Pyrogram).
    Файл загружается, копируется в sessions/, подключается к TG для проверки.
    """
    if not file.filename.endswith(".session"):
        raise HTTPException(status_code=400, detail="Файл должен быть .session формата")

    await acc_svc.check_limit(db, current_user)

    sessions_dir = _get_sessions_dir()
    cli_config = _get_cli_config()

    # Определяем имя файла
    if phone:
        clean_phone = phone.strip().replace("+", "")
    else:
        clean_phone = file.filename.replace(".session", "")

    session_path = sessions_dir / f"{clean_phone}.session"

    # Сохраняем файл
    content = await file.read()
    with open(session_path, "wb") as f:
        f.write(content)

    # Пробуем подключиться и получить данные
    from telethon import TelegramClient

    client = TelegramClient(
        str(session_path).replace(".session", ""),
        cli_config.API_ID,
        cli_config.API_HASH,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="4.14.15",
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            # Файл сохранён, но сессия не активна — создаём аккаунт со статусом pending
            real_phone = f"+{clean_phone}" if not clean_phone.startswith("+") else clean_phone
            account = TelegramAccount(
                user_id=current_user.id,
                phone=real_phone,
                session_file=str(session_path),
                status="unknown",
            )
            db.add(account)
            await db.flush()
            return {
                "success": True,
                "account_id": account.id,
                "phone": real_phone,
                "status": "unknown",
                "message": "Файл загружен, но сессия не активна. Попробуйте авторизовать аккаунт.",
            }

        me = await client.get_me()
        await client.disconnect()

        real_phone = f"+{me.phone}" if me.phone else f"+{clean_phone}"

        # Переименовываем если нужно
        correct_path = sessions_dir / f"{me.phone}.session"
        if session_path != correct_path and me.phone:
            try:
                shutil.move(str(session_path), str(correct_path))
                session_path = correct_path
            except Exception:
                pass

        # Проверяем дубликат
        existing = await acc_svc.get_account_by_phone(db, real_phone, current_user.id)
        if existing:
            existing.session_file = str(session_path)
            existing.status = "active"
            existing.first_name = me.first_name or existing.first_name
            existing.username = me.username or existing.username
            existing.has_photo = bool(me.photo)
            existing.tg_id = me.id
            await db.flush()
            return {
                "success": True,
                "account_id": existing.id,
                "phone": real_phone,
                "status": "active",
                "message": f"Аккаунт {real_phone} обновлён",
                "already_existed": True,
            }

        # Создаём новый
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
            "message": f"Аккаунт {real_phone} успешно импортирован",
        }

    except Exception as e:
        try:
            await client.disconnect()
        except:
            pass
        raise HTTPException(status_code=500, detail=f"Ошибка при подключении: {str(e)}")


# ── Batch session import ─────────────────────────────────────

@router.post("/sessions-batch")
async def import_session_files_batch(
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Пакетный импорт нескольких .session файлов.
    """
    results = []
    imported = 0
    errors = []

    for file in files:
        if not file.filename.endswith(".session"):
            errors.append({"file": file.filename, "error": "Не .session файл"})
            continue

        try:
            # Рекурсивно вызываем одиночный импорт
            clean_phone = file.filename.replace(".session", "")
            sessions_dir = _get_sessions_dir()
            session_path = sessions_dir / f"{clean_phone}.session"

            content = await file.read()
            with open(session_path, "wb") as f:
                f.write(content)

            real_phone = f"+{clean_phone}" if not clean_phone.startswith("+") else clean_phone

            account = TelegramAccount(
                user_id=current_user.id,
                phone=real_phone,
                session_file=str(session_path),
                status="unknown",
            )
            db.add(account)
            imported += 1
            results.append({"file": file.filename, "phone": real_phone, "status": "added"})

        except Exception as e:
            errors.append({"file": file.filename, "error": str(e)})

    await db.flush()

    return {
        "imported": imported,
        "errors": errors,
        "results": results,
        "message": f"Импортировано {imported} файлов. Ошибок: {len(errors)}",
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
