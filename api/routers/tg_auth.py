"""
GramGPT API — routers/tg_auth.py
Веб-авторизация Telegram аккаунтов.
python-socks (async) + dict формат — работает напрямую в uvicorn без потоков.
"""

import asyncio
import json
import os
import importlib.util
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.proxy import Proxy
from services.accounts import get_account_by_phone, sync_from_dict

logger = logging.getLogger(__name__)


def load_cli_config():
    config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config.py"))
    spec = importlib.util.spec_from_file_location("cli_config_external", config_path)
    cli_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_module)
    return cli_module


router = APIRouter(prefix="/tg-auth", tags=["tg-auth"])


# ── Redis ────────────────────────────────────────────────────

def _redis():
    import redis as redis_lib
    return redis_lib.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True
    )

def _session_key(user_id: int, phone: str) -> str:
    return f"tg_auth:{user_id}:{phone}"


# ── Schemas ──────────────────────────────────────────────────

class SendCodeRequest(BaseModel):
    phone: str
    proxy_id: int | None = None

class ConfirmCodeRequest(BaseModel):
    phone: str
    code: str

class Confirm2FARequest(BaseModel):
    phone: str
    password: str

class SendCodeResponse(BaseModel):
    phone: str
    code_type: str
    message: str

class AuthResult(BaseModel):
    success: bool
    phone: str
    first_name: str = ""
    username: str = ""
    account_id: int | None = None
    needs_2fa: bool = False
    message: str = ""


# ── Proxy + Client ───────────────────────────────────────────

def _make_proxy(proxy_row):
    """Dict формат для python-socks — проверено, работает"""
    if not proxy_row:
        return None
    proto_str = proxy_row.protocol.value if hasattr(proxy_row.protocol, 'value') else str(proxy_row.protocol)
    proxy = {
        'proxy_type': proto_str,
        'addr': str(proxy_row.host),
        'port': int(proxy_row.port),
        'rdns': True,
    }
    if proxy_row.login:
        proxy['username'] = proxy_row.login
    if proxy_row.password:
        proxy['password'] = proxy_row.password
    return proxy


def _make_client(phone, proxy_dict=None):
    from telethon import TelegramClient
    cli_config = load_cli_config()
    session_path = str(cli_config.SESSIONS_DIR / phone.replace("+", ""))
    return TelegramClient(
        session_path, cli_config.API_ID, cli_config.API_HASH,
        proxy=proxy_dict,
        device_model="Desktop", system_version="Windows 10", app_version="4.14.15",
        lang_code="ru", system_lang_code="ru",
        timeout=30,
    )


# ── Storage ──────────────────────────────────────────────────

ACTIVE_CLIENTS = {}  # phone → TelegramClient (живой, подключённый)
PENDING_PROXY = {}   # phone → proxy_id


# ── Endpoints ────────────────────────────────────────────────

@router.post("/send-code", response_model=SendCodeResponse)
async def send_code(
    body: SendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    phone = body.phone.strip()
    if not phone.startswith("+"): phone = "+" + phone

    # Очищаем старого клиента
    if phone in ACTIVE_CLIENTS:
        try: await ACTIVE_CLIENTS[phone].disconnect()
        except: pass
        del ACTIVE_CLIENTS[phone]

    # Загружаем прокси
    proxy_dict = None
    if body.proxy_id:
        result = await db.execute(
            select(Proxy).where(Proxy.id == body.proxy_id, Proxy.user_id == current_user.id)
        )
        proxy_row = result.scalar_one_or_none()
        if proxy_row:
            proxy_dict = _make_proxy(proxy_row)
            PENDING_PROXY[phone] = body.proxy_id

    client = _make_client(phone, proxy_dict)

    try:
        await asyncio.wait_for(client.connect(), timeout=45)

        if await client.is_user_authorized():
            await client.disconnect()
            return SendCodeResponse(phone=phone, code_type="already_authorized",
                                    message="Аккаунт уже авторизован.")

        sent = await client.send_code_request(phone)

        # Сохраняем клиента — нужен для confirm
        ACTIVE_CLIENTS[phone] = client

        # Сохраняем hash в Redis
        r = _redis()
        r.setex(_session_key(current_user.id, phone), 600,
                json.dumps({"phone_code_hash": sent.phone_code_hash, "phone": phone}))

        ctn = type(sent.type).__name__
        if "App" in ctn: code_type, msg = "app", "Код отправлен в Telegram"
        elif "Sms" in ctn: code_type, msg = "sms", f"SMS на {phone}"
        else: code_type, msg = "call", f"Код (тип: {ctn})"
        if proxy_dict: msg += " [через прокси]"

        return SendCodeResponse(phone=phone, code_type=code_type, message=msg)

    except asyncio.TimeoutError:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=504, detail="Таймаут подключения. Проверьте прокси.")
    except Exception as e:
        try: await client.disconnect()
        except: pass
        err = str(e)
        if "PHONE_NUMBER_INVALID" in err:
            raise HTTPException(status_code=400, detail="Неверный номер телефона")
        if "FLOOD_WAIT" in err:
            import re; secs = re.search(r"\d+", err)
            raise HTTPException(status_code=429, detail=f"Подожди {secs.group() if secs else '?'} сек")
        if "407" in err or "Authentication Required" in err:
            raise HTTPException(status_code=407, detail="Прокси: неверный логин/пароль")
        if "Unexpected SOCKS version" in err:
            raise HTTPException(status_code=400, detail="Неверный протокол (SOCKS5 ↔ HTTP)")
        raise HTTPException(status_code=500, detail=f"Ошибка: {err[:200]}")


@router.post("/confirm", response_model=AuthResult)
async def confirm_code(
    body: ConfirmCodeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    phone = body.phone.strip()
    if not phone.startswith("+"): phone = "+" + phone
    code = body.code.strip().replace(" ", "")

    r = _redis()
    raw = r.get(_session_key(current_user.id, phone))
    if not raw:
        raise HTTPException(status_code=400, detail="Сессия истекла или код не запрошен.")
    session_data = json.loads(raw)
    phone_code_hash = session_data["phone_code_hash"]

    # Берём живого клиента
    client = ACTIVE_CLIENTS.get(phone)
    if not client:
        # Если сервер перезагрузился — создаём нового
        proxy_dict = None
        proxy_id = PENDING_PROXY.get(phone)
        if proxy_id:
            result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
            proxy_row = result.scalar_one_or_none()
            if proxy_row: proxy_dict = _make_proxy(proxy_row)
        client = _make_client(phone, proxy_dict)
        await client.connect()

    try:
        from telethon import errors
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except errors.SessionPasswordNeededError:
            ACTIVE_CLIENTS[phone] = client
            return AuthResult(success=False, phone=phone, needs_2fa=True, message="Нужна 2FA.")
        except errors.PhoneCodeInvalidError:
            raise HTTPException(status_code=400, detail="Неверный код")
        except errors.PhoneCodeExpiredError:
            raise HTTPException(status_code=400, detail="Код истёк — запроси новый")

        # Успех
        me = await client.get_me()
        await client.disconnect()
        ACTIVE_CLIENTS.pop(phone, None)
        r.delete(_session_key(current_user.id, phone))

        db_account = await _save_account(db, current_user, phone, me)

        proxy_id = PENDING_PROXY.pop(phone, None)
        if proxy_id and db_account:
            db_account.proxy_id = proxy_id
            await db.flush()

        return AuthResult(success=True, phone=phone, first_name=me.first_name or "",
                          username=me.username or "", account_id=db_account.id,
                          message="Авторизован!")

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        ACTIVE_CLIENTS.pop(phone, None)
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/confirm-2fa", response_model=AuthResult)
async def confirm_2fa(
    body: Confirm2FARequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    phone = body.phone.strip()
    if not phone.startswith("+"): phone = "+" + phone

    client = ACTIVE_CLIENTS.get(phone)
    if not client:
        proxy_dict = None
        proxy_id = PENDING_PROXY.get(phone)
        if proxy_id:
            result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
            proxy_row = result.scalar_one_or_none()
            if proxy_row: proxy_dict = _make_proxy(proxy_row)
        client = _make_client(phone, proxy_dict)
        await client.connect()

    try:
        from telethon import errors
        try:
            await client.sign_in(password=body.password)
        except errors.PasswordHashInvalidError:
            raise HTTPException(status_code=400, detail="Неверный пароль 2FA")

        me = await client.get_me()
        await client.disconnect()
        ACTIVE_CLIENTS.pop(phone, None)

        r = _redis()
        r.delete(_session_key(current_user.id, phone) + ":needs2fa")
        r.delete(_session_key(current_user.id, phone))

        db_account = await _save_account(db, current_user, phone, me)

        proxy_id = PENDING_PROXY.pop(phone, None)
        if proxy_id and db_account:
            db_account.proxy_id = proxy_id
            await db.flush()

        return AuthResult(success=True, phone=phone, first_name=me.first_name or "",
                          username=me.username or "", account_id=db_account.id,
                          message="Авторизован (2FA)!")

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        ACTIVE_CLIENTS.pop(phone, None)
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/add-already-authorized")
async def add_already_authorized(
    body: SendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    phone = body.phone.strip()
    if not phone.startswith("+"): phone = "+" + phone

    proxy_dict = None
    if body.proxy_id:
        result = await db.execute(
            select(Proxy).where(Proxy.id == body.proxy_id, Proxy.user_id == current_user.id)
        )
        proxy_row = result.scalar_one_or_none()
        if proxy_row: proxy_dict = _make_proxy(proxy_row)

    client = _make_client(phone, proxy_dict)

    try:
        await asyncio.wait_for(client.connect(), timeout=45)
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        me = await client.get_me()
        await client.disconnect()

        db_account = await _save_account(db, current_user, phone, me)
        if body.proxy_id and db_account:
            db_account.proxy_id = body.proxy_id
            await db.flush()

        return {"success": True, "account_id": db_account.id, "phone": phone,
                "first_name": me.first_name or "", "username": me.username or ""}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Helper ───────────────────────────────────────────────────

async def _save_account(db, current_user, phone, me):
    """Сохраняет авторизованный аккаунт в БД"""
    cli_config = load_cli_config()
    import sys
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if root_dir not in sys.path: sys.path.insert(0, root_dir)
    api_config_cache = sys.modules.pop('config', None)
    import trust as trust_module
    from db import make_account_template
    if api_config_cache: sys.modules['config'] = api_config_cache

    account_dict = make_account_template(phone)
    account_dict["id"] = me.id
    account_dict["first_name"] = me.first_name or ""
    account_dict["last_name"] = me.last_name or ""
    account_dict["username"] = me.username or ""
    account_dict["has_photo"] = bool(me.photo)
    account_dict["session_file"] = str(cli_config.SESSIONS_DIR / phone.replace("+", "")) + ".session"
    account_dict["status"] = "active"
    account_dict["trust_score"] = trust_module.calculate(account_dict)

    return await sync_from_dict(db, current_user, account_dict)