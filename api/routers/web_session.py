"""
GramGPT API — routers/web_session.py
Конвертация сессий из Telegram Web K (localStorage) в Telethon .session файлы.

Web K хранит auth_key в localStorage в полях accountN = JSON{dcId, dc{N}_auth_key, ...}
Этот endpoint:
  1. Парсит JSON-блок аккаунта
  2. Извлекает auth_key для главного DC
  3. Создаёт SQLite .session файл совместимый с Telethon
  4. Подключается через прокси, проверяет авторизацию, сохраняет в БД
"""

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.api_app import ApiApp
from models.proxy import Proxy
from services import accounts as acc_svc

router = APIRouter(prefix="/import", tags=["import"])


# ── Production DC IPs (Telegram) ─────────────────────────────
DC_IPS = {
    1: ("149.154.175.53",  443),
    2: ("149.154.167.51",  443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91",  443),
    5: ("91.108.56.130",   443),
}

# Public Telegram Web K credentials (one of multiple known)
TG_WEB_API_ID = 2496
TG_WEB_API_HASH = "8da85b0d5bfe62527e5b244c209159c3"


# ── Helpers ──────────────────────────────────────────────────

def _get_sessions_dir():
    import importlib.util
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    config_path = os.path.join(root_dir, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config.SESSIONS_DIR


def _create_telethon_session_file(session_path: Path, dc_id: int, auth_key_hex: str):
    """Создаёт SQLite .session файл совместимый с Telethon из auth_key."""
    auth_key_bytes = bytes.fromhex(auth_key_hex.strip().replace(" ", ""))
    if len(auth_key_bytes) != 256:
        raise ValueError(f"auth_key должен быть 256 байт, получено {len(auth_key_bytes)}")

    if dc_id not in DC_IPS:
        raise ValueError(f"Неизвестный DC {dc_id}")

    server_address, port = DC_IPS[dc_id]

    if session_path.exists():
        session_path.unlink()
    session_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(session_path))
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE version (version INTEGER PRIMARY KEY);
    CREATE TABLE sessions (
        dc_id INTEGER PRIMARY KEY,
        server_address TEXT,
        port INTEGER,
        auth_key BLOB,
        takeout_id INTEGER
    );
    CREATE TABLE entities (
        id INTEGER PRIMARY KEY,
        hash INTEGER NOT NULL,
        username TEXT,
        phone INTEGER,
        name TEXT,
        date INTEGER
    );
    CREATE TABLE sent_files (
        md5_digest BLOB,
        file_size INTEGER,
        type INTEGER,
        id INTEGER,
        hash INTEGER,
        PRIMARY KEY(md5_digest, file_size, type)
    );
    CREATE TABLE update_state (
        id INTEGER PRIMARY KEY,
        pts INTEGER,
        qts INTEGER,
        date INTEGER,
        seq INTEGER
    );
    INSERT INTO version VALUES (7);
    """)
    cur.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)",
        (dc_id, server_address, port, auth_key_bytes)
    )
    conn.commit()
    conn.close()


# ── Models ───────────────────────────────────────────────────

class WebSessionImportRequest(BaseModel):
    """Импорт одного аккаунта из Web localStorage."""
    dc_id: int                      # из поля dcId внутри accountN
    auth_key: str                   # hex-строка из dc{dc_id}_auth_key
    proxy_id: int                   # обязательный прокси
    api_app_id: Optional[int] = None  # если None → используем 2496 (Telegram Web K)
    phone: Optional[str] = None     # опционально, авто-определится через get_me()


class WebAccountPreview(BaseModel):
    """Превью одного аккаунта при парсинге блоба localStorage."""
    label: str          # account1 / account2 / ...
    dc_id: int
    user_id: Optional[int] = None
    auth_key: str       # hex для главного DC
    fingerprint: Optional[str] = None


class WebStorageParseRequest(BaseModel):
    """Принимает текстовый блоб из localStorage и возвращает превью аккаунтов."""
    storage_blob: str   # сырой JSON-блок (либо просто экспорт всего localStorage)


# ── Endpoints ────────────────────────────────────────────────

@router.post("/web-storage-parse")
async def parse_web_storage(
    body: WebStorageParseRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Парсит блоб из localStorage Telegram Web K и возвращает список аккаунтов.
    Не подключается к Telegram, не сохраняет в БД — просто превью.
    """
    import json
    import re

    blob = body.storage_blob.strip()
    accounts = []

    # Пытаемся разными способами найти account1, account2, ... в блобе
    # Способ 1: блоб уже валидный JSON-словарь с ключами accountN
    parsed_dict = None
    try:
        parsed_dict = json.loads(blob)
    except Exception:
        pass

    if isinstance(parsed_dict, dict):
        for key, val in parsed_dict.items():
            if not key.startswith("account"):
                continue
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    continue
            accounts.append((key, val))

    # Способ 2: ищем регуляркой подстроки accountN{...} в сыром тексте
    if not accounts:
        pattern = re.compile(r'account(\d+)\s*({[^}]*"dcId"[^}]*})', re.DOTALL)
        for m in pattern.finditer(blob):
            label = f"account{m.group(1)}"
            try:
                val = json.loads(m.group(2))
                accounts.append((label, val))
            except Exception:
                continue

    if not accounts:
        raise HTTPException(
            status_code=400,
            detail="Не найдено ни одного аккаунта. Убедись что вставил данные из localStorage Telegram Web K."
        )

    result = []
    for label, data in accounts:
        dc_id = data.get("dcId")
        if not dc_id:
            continue
        auth_key_field = f"dc{dc_id}_auth_key"
        auth_key = data.get(auth_key_field)
        if not auth_key:
            continue
        result.append(WebAccountPreview(
            label=label,
            dc_id=int(dc_id),
            user_id=data.get("userId"),
            auth_key=auth_key,
            fingerprint=data.get("auth_key_fingerprint"),
        ).dict())

    if not result:
        raise HTTPException(
            status_code=400,
            detail="Найдены блоки accountN, но в них нет dc{N}_auth_key. Проверь, что аккаунт реально авторизован в Web."
        )

    return {"accounts": result, "count": len(result)}


@router.post("/web-session")
async def import_web_session(
    body: WebSessionImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импорт одного аккаунта из Web. Создаёт .session, подключается через прокси,
    делает get_me(), сохраняет аккаунт в БД.
    """
    await acc_svc.check_limit(db, current_user)

    # 1. Прокси
    proxy_r = await db.execute(
        select(Proxy).where(Proxy.id == body.proxy_id, Proxy.user_id == current_user.id)
    )
    proxy_row = proxy_r.scalar_one_or_none()
    if not proxy_row:
        raise HTTPException(status_code=404, detail=f"Прокси #{body.proxy_id} не найден")

    from routers.tg_auth import _make_proxy
    proxy_dict = _make_proxy(proxy_row)
    if not proxy_dict:
        raise HTTPException(status_code=400, detail="Не удалось построить прокси")

    # 2. API app — если не выбран, используем Telegram Web K (2496)
    api_id_use = TG_WEB_API_ID
    api_hash_use = TG_WEB_API_HASH
    platform_use = "desktop"
    api_app_id_save = None

    if body.api_app_id:
        app_r = await db.execute(
            select(ApiApp).where(
                ApiApp.id == body.api_app_id,
                ApiApp.user_id == current_user.id,
                ApiApp.is_active == True,
            )
        )
        api_app = app_r.scalar_one_or_none()
        if not api_app:
            raise HTTPException(status_code=404, detail="API app не найден")
        api_id_use = api_app.api_id
        api_hash_use = api_app.api_hash
        platform_use = getattr(api_app, 'platform', 'desktop') or 'desktop'
        api_app_id_save = api_app.id

    # 3. Создаём .session файл
    sessions_dir = _get_sessions_dir()
    # Временное имя — переименуем после get_me()
    tmp_phone = body.phone.strip().replace("+", "") if body.phone else f"web_{body.dc_id}_{body.auth_key[:8]}"
    session_path = Path(sessions_dir) / f"{tmp_phone}.session"

    try:
        _create_telethon_session_file(session_path, body.dc_id, body.auth_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Ошибка формирования сессии: {e}")

    # 4. Подключаемся, проверяем
    from telethon import TelegramClient
    from utils.telegram import _get_device_for_platform

    # Для Web K device_model должен быть desktop-style
    fp = _get_device_for_platform(tmp_phone, platform_use)
    print(f"🌐 Web import: dc={body.dc_id}, api_id={api_id_use}, platform={platform_use}, device={fp['device']}")

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
            try: session_path.unlink(missing_ok=True)
            except: pass
            raise HTTPException(
                status_code=400,
                detail="Auth key не валиден или сессия истекла. Возможно нужно перелогиниться в Web."
            )

        me = await client.get_me()
        await client.disconnect()

        if not me.phone:
            try: session_path.unlink(missing_ok=True)
            except: pass
            raise HTTPException(status_code=400, detail="Не удалось получить номер телефона из аккаунта")

        real_phone = f"+{me.phone}"

        # Переименовываем session по реальному номеру
        correct_path = Path(sessions_dir) / f"{me.phone}.session"
        if session_path != correct_path:
            try:
                if correct_path.exists():
                    correct_path.unlink()
                session_path.rename(correct_path)
                session_path = correct_path
            except Exception as e:
                print(f"🌐 Не удалось переименовать: {e}")

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
            existing.proxy_id = body.proxy_id
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
                "already_existed": True,
                "message": f"Аккаунт {real_phone} обновлён из Web сессии",
            }

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
            proxy_id=body.proxy_id,
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
            "message": f"Аккаунт {real_phone} импортирован из Web",
        }

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        try: await client.disconnect()
        except: pass
        try: session_path.unlink(missing_ok=True)
        except: pass
        raise HTTPException(status_code=504, detail="Таймаут — проверь прокси")
    except Exception as e:
        try: await client.disconnect()
        except: pass
        try: session_path.unlink(missing_ok=True)
        except: pass
        err = str(e)
        print(f"🌐 ❌ Web import error: {type(e).__name__}: {err}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {err[:200]}")
