"""
GramGPT API — routers/web_session.py
Конвертация сессий из Telegram Web K (localStorage) в Telethon .session файлы.

ОБНОВЛЕНИЯ:
  - Использует WEB_K_DEVICES (реальные браузеры) для api_id=2496
  - Seed для fingerprint = userId (стабильный)
  - Не перезаписывает device_fingerprint при повторном импорте
  - Таймаут на коннект
"""

import asyncio
import os
import sqlite3
import hashlib
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

# Public Telegram Web K credentials
TG_WEB_API_ID = 2496
TG_WEB_API_HASH = "8da85b0d5bfe62527e5b244c209159c3"


# ── Web K реалистичные браузерные fingerprints ──────────────
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
    """Детерминированный browser-fingerprint по seed (user_id или auth_key)."""
    h = int(hashlib.md5(str(seed).encode()).hexdigest(), 16)
    return WEB_K_DEVICES[h % len(WEB_K_DEVICES)]


def _safe_set_attr(obj, name: str, value):
    """Записать в атрибут только если он существует в модели."""
    if hasattr(obj, name):
        try:
            setattr(obj, name, value)
        except Exception as e:
            print(f"⚠ _safe_set_attr({name}): {e}")


# ── Helpers ──────────────────────────────────────────────────

def _get_sessions_dir():
    # Раньше брали через импорт корневого tg_manager1/config.py — но он
    # переименован в config.py.legacy. Логика та же: <repo>/sessions.
    p = Path(__file__).resolve().parent.parent.parent / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


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
    dc_id: int
    auth_key: str
    proxy_id: int
    api_app_id: Optional[int] = None
    phone: Optional[str] = None
    user_id: Optional[int] = None       # ← НОВОЕ: для seed fingerprint


class WebAccountPreview(BaseModel):
    label: str
    dc_id: int
    user_id: Optional[int] = None
    auth_key: str
    fingerprint: Optional[str] = None


class WebStorageParseRequest(BaseModel):
    storage_blob: str


# ── Endpoints ────────────────────────────────────────────────

@router.post("/web-storage-parse")
async def parse_web_storage(
    body: WebStorageParseRequest,
    current_user: User = Depends(get_current_user),
):
    """Парсит блоб localStorage Telegram Web K → возвращает превью аккаунтов."""
    import json
    import re

    blob = body.storage_blob.strip()
    accounts = []

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

    if not accounts:
        # DevTools Application-tab формат: парсим разными способами,
        # покрывая разные форматы сессий (Web K разных версий и подмены
        # антидетект-браузеров иногда дают неполный JSON: avatarUri с
        # base64 обрезается clipboard'ом, лишние поля в начале и т.п.).
        #
        # Стратегия:
        #   1) Найти все `accountN` метки.
        #   2) Для каждой ограничить «секцию» до следующей метки accountM
        #      (или конца) — чтобы balanced-brace не сожрал чужие данные.
        #   3) Path A: внутри секции читаем balanced `{...}` и парсим JSON.
        #   4) Path B (fallback): если JSON обрезан / не парсится —
        #      достаём dcId / dc{N}_auth_key / userId / phone /
        #      auth_key_fingerprint через regex прямо по секции. Эти
        #      поля всегда лежат В НАЧАЛЕ JSON, до avatarUri, поэтому
        #      даже сильно обрезанная сессия отдаёт всё нужное.
        #   5) Дополнительный fallback: если внутри секции нет dc{N}_auth_key
        #      для своего dcId — ищем top-level `dcN_auth_key "..."` строки.
        def _extract_braces(text: str, start: int, end: int):
            i = text.find("{", start, end)
            if i < 0 or i >= end:
                return None
            depth = 0
            for j in range(i, min(len(text), end)):
                c = text[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[i:j + 1]
            return None  # JSON не завершён

        def _regex_partial(section: str) -> dict:
            partial = {}
            mm = re.search(r'"dcId"\s*:\s*(\d+)', section)
            if mm:
                partial["dcId"] = int(mm.group(1))
            for dc_n in (1, 2, 3, 4, 5):
                mk = re.search(rf'"dc{dc_n}_auth_key"\s*:\s*"([0-9a-fA-F]+)"', section)
                if mk:
                    partial[f"dc{dc_n}_auth_key"] = mk.group(1)
            mu = re.search(r'"userId"\s*:\s*"?(\d+)"?', section)
            if mu:
                partial["userId"] = mu.group(1)
            mp = re.search(r'"phone"\s*:\s*"?(\d+)"?', section)
            if mp:
                partial["phone"] = mp.group(1)
            mfn = re.search(r'"firstName"\s*:\s*"([^"]*)"', section)
            if mfn:
                partial["firstName"] = mfn.group(1)
            mfp = re.search(r'"auth_key_fingerprint"\s*:\s*"([0-9a-fA-F]+)"', section)
            if mfp:
                partial["auth_key_fingerprint"] = mfp.group(1)
            return partial

        # Top-level dcN_auth_key как pool на случай если у account-блока
        # auth_key обрезан / отсутствует. Web K часто дублирует primary
        # auth_key на верхнем уровне localStorage.
        toplevel_keys = {}
        for tk in re.finditer(r'^(dc\d_auth_key)\s+"([0-9a-fA-F]+)"\s*$', blob, re.MULTILINE):
            toplevel_keys[tk.group(1)] = tk.group(2)

        account_matches = list(re.finditer(r'\baccount(\d+)\b', blob))
        for idx, m in enumerate(account_matches):
            label = f"account{m.group(1)}"
            sec_start = m.end()
            sec_end = account_matches[idx + 1].start() if idx + 1 < len(account_matches) else len(blob)

            val = None
            # Path A: balanced-brace JSON в рамках секции
            json_str = _extract_braces(blob, sec_start, sec_end)
            if json_str:
                try:
                    parsed = json.loads(json_str)
                    if isinstance(parsed, dict) and parsed.get("dcId"):
                        val = parsed
                except Exception:
                    pass

            # Path B: обрезанный JSON → regex partial по секции
            if val is None:
                section = blob[sec_start:sec_end]
                partial = _regex_partial(section)
                if partial.get("dcId"):
                    val = partial

            if val is None:
                continue

            # Доберём auth_key из top-level pool если в самом блоке его нет
            dc_id = val.get("dcId")
            if dc_id:
                ak_field = f"dc{dc_id}_auth_key"
                if not val.get(ak_field) and toplevel_keys.get(ak_field):
                    val[ak_field] = toplevel_keys[ak_field]

            accounts.append((label, val))

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
    """Импорт одного аккаунта из Web."""
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

    # 2. API app
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
    else:
        # АВТОМАТИЧНИЙ ДЕФОЛТ: Шукаємо Web K (api_id=2496) у базі
        app_r = await db.execute(
            select(ApiApp).where(
                ApiApp.api_id == TG_WEB_API_ID, # Це 2496
                ApiApp.user_id == current_user.id
            )
        )
        web_app = app_r.scalar_one_or_none()
        
        if web_app:
            api_id_use = web_app.api_id
            api_hash_use = web_app.api_hash
            platform_use = getattr(web_app, 'platform', 'desktop') or 'desktop'
            api_app_id_save = web_app.id  # Ось тут збережеться твоя цифра 7 з БД
        else:
            raise HTTPException(
                status_code=400, 
                detail="В базі даних не знайдено дефолтний додаток Telegram Web K (api_id=2496)"
            )


    # 3. Создаём .session файл
    sessions_dir = _get_sessions_dir()
    tmp_phone = body.phone.strip().replace("+", "") if body.phone else f"web_{body.dc_id}_{body.auth_key[:8]}"
    session_path = Path(sessions_dir) / f"{tmp_phone}.session"

    try:
        _create_telethon_session_file(session_path, body.dc_id, body.auth_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Ошибка формирования сессии: {e}")

    # 4. Fingerprint — реальный, не temp
    if body.user_id:
        fp_seed = str(body.user_id)
    else:
        fp_seed = f"{body.dc_id}_{body.auth_key[:32]}"

    if api_id_use == TG_WEB_API_ID:
        fp = get_web_k_device(fp_seed)
        print(f"🌐 Web K device: {fp['device']} / {fp['system']} (seed={fp_seed[:16]})")
    else:
        from utils.telegram import _get_device_for_platform
        fp = _get_device_for_platform(fp_seed, platform_use)
        print(f"🌐 Custom api device: {fp['device']} / {fp['system']} (seed={fp_seed[:16]})")

    # 5. Подключаемся
    from telethon import TelegramClient
    print(f"🌐 Web import: dc={body.dc_id}, api_id={api_id_use}, platform={platform_use}")

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
            # ✅ НЕ перезаписываем fingerprint если уже есть
            if not existing.device_fingerprint:
                existing.device_fingerprint = device_fp
                print(f"🌐 Установлен fingerprint впервые")
            else:
                print(f"🌐 Сохраняем существующий fingerprint: {existing.device_fingerprint}")
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