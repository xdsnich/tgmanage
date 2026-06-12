"""
GramGPT API — routers/tg_auth.py
Веб-авторизация Telegram аккаунтов.

Мульти-API поддержка:
  - api_app_id передаётся с фронта → используем этот api_id/hash/platform
  - api_app_id не передан → используем глобальный config
  - platform определяет пул устройств (android/ios/desktop/macos)
"""

import asyncio
import json
import os
import importlib.util
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.proxy import Proxy
from models.api_app import ApiApp
from models.account import TelegramAccount
from services.accounts import get_account_by_phone

logger = logging.getLogger(__name__)


def load_cli_config():
    """Конфиг для авторизации: api_id/hash из env (TG_API_ID/TG_API_HASH),
    sessions dir по пути. Раньше грузил легаси root config.py — он удалён
    (переименован в config.py.legacy), поэтому больше не читаем файл."""
    import types
    cfg = types.SimpleNamespace()
    try:
        cfg.API_ID = int(os.getenv("TG_API_ID", "0"))
    except (ValueError, TypeError):
        cfg.API_ID = 0
    cfg.API_HASH = (os.getenv("TG_API_HASH", "") or "").strip()
    cfg.SESSIONS_DIR = Path(os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "sessions")
    ))
    return cfg


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
    proxy_id: Optional[int] = None
    api_app_id: Optional[int] = None

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


def _make_client(phone, proxy_dict=None, api_id=None, api_hash=None, platform="android"):
    """Создаёт Telethon клиент для авторизации."""
    from utils.telegram import _get_device_for_platform
    from telethon import TelegramClient

    fingerprint = _get_device_for_platform(phone, platform)

    if not api_id or not api_hash:
        cli_config = load_cli_config()
        api_id = api_id or cli_config.API_ID
        api_hash = api_hash or cli_config.API_HASH

    # Telethon строго требует api_id: int, api_hash: str. Если из БД пришли
    # другие типы (например ApiApp.api_hash как-то превратилось в int),
    # Telethon упадёт глубоко внутри MTProto сериализации с непонятным
    # TypeError. Приводим явно — безопасно для строки/числа и для None.
    try:
        api_id = int(api_id) if api_id else 0
    except (ValueError, TypeError):
        api_id = 0
    api_hash = str(api_hash).strip() if api_hash else ""

    sessions_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "sessions"
    ))
    os.makedirs(sessions_dir, exist_ok=True)
    session_path = os.path.join(sessions_dir, phone.replace("+", "")) + ".session"

    print(f"🔑 _make_client: api_id={api_id} ({type(api_id).__name__}), "
          f"api_hash_len={len(api_hash)} ({type(api_hash).__name__}), "
          f"platform={platform}, device={fingerprint['device']}")

    return TelegramClient(
        session_path.replace(".session", ""),
        api_id, api_hash,
        proxy=proxy_dict,
        device_model=fingerprint["device"],
        system_version=fingerprint["system"],
        app_version=fingerprint["app_version"],
        lang_code="en",
        system_lang_code="en",
        timeout=30,
    )


async def _load_api_app_creds(db, api_app_id: int, user_id: int):
    """Загружает api_app по ID. Возвращает (api_id, api_hash, platform)."""
    if not api_app_id:
        return None, None, "android"

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
    # Гарантируем правильные типы: Telethon требует api_id:int + api_hash:str
    try:
        api_id_int = int(api_app.api_id)
    except (ValueError, TypeError):
        api_id_int = 0
    api_hash_str = str(api_app.api_hash).strip() if api_app.api_hash else ""
    print(f"🔑 Используем API app #{api_app_id}: api_id={api_id_int}, "
          f"hash_len={len(api_hash_str)}, platform={platform}")
    return api_id_int, api_hash_str, platform


# ── Storage ──────────────────────────────────────────────────

ACTIVE_CLIENTS = {}
PENDING_PROXY = {}
PENDING_API_APP = {}


# ── Endpoints ────────────────────────────────────────────────

@router.post("/send-code", response_model=SendCodeResponse)
async def send_code(
    body: SendCodeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    phone = body.phone.strip()
    if not phone.startswith("+"): phone = "+" + phone

    print(f"🔑 SEND-CODE: phone={phone}, proxy_id={body.proxy_id}, api_app_id={body.api_app_id}")

    if phone in ACTIVE_CLIENTS:
        try: await ACTIVE_CLIENTS[phone].disconnect()
        except: pass
        del ACTIVE_CLIENTS[phone]

    proxy_dict = None
    if body.proxy_id:
        result = await db.execute(
            select(Proxy).where(Proxy.id == body.proxy_id, Proxy.user_id == current_user.id)
        )
        proxy_row = result.scalar_one_or_none()
        if proxy_row:
            proxy_dict = _make_proxy(proxy_row)
            PENDING_PROXY[phone] = body.proxy_id
            print(f"🔑 ПРОКСИ: {proxy_row.host}:{proxy_row.port} ({proxy_row.protocol})")
        else:
            print(f"🔑 ПРОКСИ НЕ НАЙДЕН: id={body.proxy_id}")
    else:
        print("🔑 БЕЗ ПРОКСИ")

    if not proxy_dict:
        raise HTTPException(status_code=400, detail="Прокси обязателен. Выберите прокси перед авторизацией.")

    api_id_use, api_hash_use, platform_use = await _load_api_app_creds(
        db, body.api_app_id, current_user.id
    )
    if body.api_app_id:
        PENDING_API_APP[phone] = body.api_app_id

    client = _make_client(
        phone, proxy_dict,
        api_id=api_id_use, api_hash=api_hash_use, platform=platform_use,
    )

    try:
        await asyncio.wait_for(client.connect(), timeout=45)

        if await client.is_user_authorized():
            await client.disconnect()
            return SendCodeResponse(phone=phone, code_type="already_authorized",
                                    message="Аккаунт уже авторизован.")

        sent = await client.send_code_request(phone)
        ACTIVE_CLIENTS[phone] = client

        r = _redis()
        r.setex(_session_key(current_user.id, phone), 600,
                json.dumps({"phone_code_hash": sent.phone_code_hash, "phone": phone}))

        ctn = type(sent.type).__name__
        nxt = type(sent.next_type).__name__ if getattr(sent, "next_type", None) else None
        print(f"🔑 ✅ КОД ОТПРАВЛЕН: тип={ctn}"
              + (f", next={nxt}" if nxt else "")
              + f" (api_id={api_id_use or 'default(env)'})")
        if "App" in ctn:
            code_type = "app"
            msg = "Код отправлен в приложение Telegram (на другом устройстве), НЕ по SMS"
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
        import traceback
        print(f"🔑 ❌ SEND-CODE EXCEPTION: {type(e).__name__}: {err}")
        print(f"🔑 ❌ TRACEBACK:\n{traceback.format_exc()}")
        if "RECAPTCHA" in err:
            raise HTTPException(
                status_code=403,
                detail=(
                    "⚠ Telegram требует пройти капчу для этого номера/прокси. "
                    "Причины: подозрительная активность на этом IP, номер с плохой репутацией, "
                    "или слишком много попыток. "
                    "РЕШЕНИЕ: попробуй другой прокси (желательно резидентский) "
                    "или войди в аккаунт через официальный Telegram и экспортни TData."
                )
            )
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

    print(f"🔑 CONFIRM: phone={phone}, has_active_client={phone in ACTIVE_CLIENTS}")

    r = _redis()
    raw = r.get(_session_key(current_user.id, phone))
    if not raw:
        raise HTTPException(status_code=400, detail="Сессия истекла или код не запрошен.")
    session_data = json.loads(raw)
    phone_code_hash = session_data["phone_code_hash"]

    client = ACTIVE_CLIENTS.get(phone)
    if not client:
        print(f"🔑 CONFIRM: нет живого клиента, создаю нового")
        proxy_dict = None
        proxy_id = PENDING_PROXY.get(phone)
        if proxy_id:
            result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
            proxy_row = result.scalar_one_or_none()
            if proxy_row: proxy_dict = _make_proxy(proxy_row)

        api_app_id = PENDING_API_APP.get(phone)
        api_id_use, api_hash_use, platform_use = await _load_api_app_creds(
            db, api_app_id, current_user.id
        )

        client = _make_client(
            phone, proxy_dict,
            api_id=api_id_use, api_hash=api_hash_use, platform=platform_use,
        )
        await client.connect()
    else:
        print(f"🔑 CONFIRM: используем живого клиента")

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

        me = await client.get_me()
        await client.disconnect()
        ACTIVE_CLIENTS.pop(phone, None)
        r.delete(_session_key(current_user.id, phone))

        db_account = await _save_account(db, current_user, phone, me)

        proxy_id = PENDING_PROXY.pop(phone, None)
        if proxy_id and db_account:
            db_account.proxy_id = proxy_id

        api_app_id = PENDING_API_APP.pop(phone, None)
        if api_app_id and db_account:
            db_account.api_app_id = api_app_id
            print(f"🔑 Аккаунт #{db_account.id} привязан к API app #{api_app_id}")

        await db.flush()

        return AuthResult(success=True, phone=phone, first_name=me.first_name or "",
                          username=me.username or "", account_id=db_account.id,
                          message="Авторизован!")

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        ACTIVE_CLIENTS.pop(phone, None)
        import traceback
        print(f"🔑 ❌ CONFIRM EXCEPTION: {type(e).__name__}: {e}")
        print(f"🔑 ❌ TRACEBACK:\n{traceback.format_exc()}")
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

        api_app_id = PENDING_API_APP.get(phone)
        api_id_use, api_hash_use, platform_use = await _load_api_app_creds(
            db, api_app_id, current_user.id
        )

        client = _make_client(
            phone, proxy_dict,
            api_id=api_id_use, api_hash=api_hash_use, platform=platform_use,
        )
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

        api_app_id = PENDING_API_APP.pop(phone, None)
        if api_app_id and db_account:
            db_account.api_app_id = api_app_id
            print(f"🔑 Аккаунт #{db_account.id} привязан к API app #{api_app_id}")

        await db.flush()

        return AuthResult(success=True, phone=phone, first_name=me.first_name or "",
                          username=me.username or "", account_id=db_account.id,
                          message="Авторизован (2FA)!")

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        ACTIVE_CLIENTS.pop(phone, None)
        import traceback
        print(f"🔑 ❌ CONFIRM-2FA EXCEPTION: {type(e).__name__}: {e}")
        print(f"🔑 ❌ TRACEBACK:\n{traceback.format_exc()}")
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

    api_id_use, api_hash_use, platform_use = await _load_api_app_creds(
        db, body.api_app_id, current_user.id
    )

    client = _make_client(
        phone, proxy_dict,
        api_id=api_id_use, api_hash=api_hash_use, platform=platform_use,
    )

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
        if body.api_app_id and db_account:
            db_account.api_app_id = body.api_app_id

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
    """
    Сохраняет авторизованный аккаунт в БД.

    ВАЖНО: пишет session_file НАПРЯМУЮ в модель TelegramAccount,
    а не через sync_from_dict — чтобы избежать багов с user.id.
    """
    # Путь к session файлу
    cli_config = load_cli_config()
    session_file_path = str(cli_config.SESSIONS_DIR / phone.replace("+", "")) + ".session"

    # Device fingerprint по платформе api_app
    api_app_id = PENDING_API_APP.get(phone)
    platform_for_fp = "android"
    if api_app_id:
        app_r = await db.execute(select(ApiApp).where(ApiApp.id == api_app_id))
        api_app = app_r.scalar_one_or_none()
        if api_app:
            platform_for_fp = getattr(api_app, 'platform', 'android') or 'android'

    from utils.telegram import _get_device_for_platform
    fp = _get_device_for_platform(phone, platform_for_fp)
    device_fingerprint = f"{fp['device']}|{fp['system']}|{fp['app_version']}"

    print(f"🔑 _save_account: phone={phone}, session_file={session_file_path}")
    print(f"🔑 Device fingerprint (platform={platform_for_fp}): {fp['device']} / {fp['system']}")

    # Ищем аккаунт в БД
    existing = await get_account_by_phone(db, phone, current_user.id)

    if existing:
        # Обновляем
        existing.tg_id = me.id
        existing.first_name = me.first_name or ""
        existing.last_name = me.last_name or ""
        existing.username = me.username or ""
        existing.has_photo = bool(me.photo)
        existing.session_file = session_file_path
        existing.status = "active"
        existing.device_fingerprint = device_fingerprint
        existing.trust_score = max(existing.trust_score or 0, 50)

        from datetime import datetime
        existing.updated_at = datetime.utcnow()

        await db.flush()
        print(f"🔑 Аккаунт #{existing.id} обновлён (session_file записан)")
        return existing

    # Создаём новый
    account = TelegramAccount(
        user_id=current_user.id,
        phone=phone,
        tg_id=me.id,
        first_name=me.first_name or "",
        last_name=me.last_name or "",
        username=me.username or "",
        has_photo=bool(me.photo),
        session_file=session_file_path,
        status="active",
        trust_score=50,
        device_fingerprint=device_fingerprint,
    )
    db.add(account)
    await db.flush()
    print(f"🔑 Аккаунт #{account.id} создан (session_file записан)")
    return account