"""
GramGPT API — routers/account_media.py
Управление медиа-папкой аккаунта (фото для сториз).

Файлы лежат на диске в api/account_media/{account_id}/<uuid>.<ext>
БД не нужна — просто listdir() при запросе.

Используется warmup-движком (action 'post_story'): когда action случайно
выбирается из пула, берётся random файл из папки и постится в сториз через
stories.SendStoryRequest.

ВАЖНО: Telegram пускает SendStoryRequest только Premium-аккаунтам. На
не-Premium запрос отвалится с PremiumAccountRequiredError — это нормально,
warmup просто скипнет action и пойдёт дальше.
"""

import os
import uuid
import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount

router = APIRouter(prefix="/accounts/{account_id}/media", tags=["account-media"])

# Отдельный роутер БЕЗ {account_id} в префиксе — для bulk-операций
bulk_router = APIRouter(prefix="/accounts/bulk/media", tags=["account-media-bulk"])

logger = logging.getLogger(__name__)

# api/account_media/  — относительно директории api/
API_DIR = Path(__file__).resolve().parent.parent
MEDIA_ROOT = API_DIR / "account_media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB на файл — Telegram сториз не любит большие


def _account_dir(account_id: int) -> Path:
    """Папка для медиа конкретного аккаунта."""
    p = MEDIA_ROOT / str(account_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _check_account_owner(db: AsyncSession, account_id: int, user_id: int) -> TelegramAccount:
    acc = (await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return acc


def list_account_media_paths(account_id: int) -> List[Path]:
    """Helper для warmup/plan_executor — возвращает абсолютные пути всех фото аккаунта."""
    p = MEDIA_ROOT / str(account_id)
    if not p.exists():
        return []
    return [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_EXTS]


# ── LIST ─────────────────────────────────────────────────────

@router.get("")
async def list_media(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список фото аккаунта. Возвращает имена файлов + размер."""
    await _check_account_owner(db, account_id, current_user.id)
    files = []
    for f in list_account_media_paths(account_id):
        try:
            files.append({
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "ext": f.suffix.lower(),
            })
        except OSError:
            pass
    return {"count": len(files), "files": sorted(files, key=lambda x: x["filename"])}


# ── UPLOAD (один или несколько файлов) ───────────────────────

@router.post("/upload")
async def upload_media(
    account_id: int,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загрузить одно или несколько фото в папку аккаунта."""
    await _check_account_owner(db, account_id, current_user.id)

    if not files:
        raise HTTPException(status_code=400, detail="Нет файлов")

    saved = []
    rejected = []
    target = _account_dir(account_id)

    for uf in files:
        ext = Path(uf.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTS:
            rejected.append({"filename": uf.filename, "reason": f"Расширение {ext} не поддерживается"})
            continue

        content = await uf.read()
        if len(content) > MAX_FILE_BYTES:
            rejected.append({"filename": uf.filename, "reason": f"Файл > {MAX_FILE_BYTES // 1024 // 1024}MB"})
            continue
        if len(content) < 1024:
            rejected.append({"filename": uf.filename, "reason": "Файл слишком маленький / битый"})
            continue

        unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
        dest = target / unique_name
        try:
            dest.write_bytes(content)
            saved.append({"filename": unique_name, "size_bytes": len(content)})
        except OSError as e:
            rejected.append({"filename": uf.filename, "reason": f"Не удалось сохранить: {e}"})

    return {
        "saved_count": len(saved),
        "rejected_count": len(rejected),
        "saved": saved,
        "rejected": rejected,
    }


# ── SERVE (для превью на фронте) ─────────────────────────────

@router.get("/file/{filename}")
async def get_media_file(
    account_id: int,
    filename: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Отдаёт файл для превью. Имя файла проверяется чтобы не было path traversal."""
    await _check_account_owner(db, account_id, current_user.id)

    # Запрещаем '/', '\', '..' в имени — защита от выхода за пределы директории
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Невалидное имя файла")

    path = _account_dir(account_id) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(path)


# ── DELETE ───────────────────────────────────────────────────

@router.delete("/file/{filename}", status_code=204)
async def delete_media(
    account_id: int,
    filename: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить одно фото."""
    await _check_account_owner(db, account_id, current_user.id)

    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Невалидное имя файла")

    path = _account_dir(account_id) / filename
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Не удалось удалить: {e}")


@router.delete("", status_code=204)
async def clear_media(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить все фото аккаунта."""
    await _check_account_owner(db, account_id, current_user.id)

    target = _account_dir(account_id)
    removed = 0
    for f in target.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return {"removed": removed}


# ══════════════════════════════════════════════════════════════════
# BULK операции — загрузить/очистить медиа сразу для N аккаунтов
# ══════════════════════════════════════════════════════════════════

@bulk_router.post("/upload")
async def bulk_upload_media(
    account_ids: str = Form(...),     # CSV: "1,2,3" — multipart не любит JSON arrays
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Загрузить ОДИН И ТОТ ЖЕ набор фото в папки нескольких аккаунтов.

    Пример: загрузил 10 фото, указал account_ids="48,51,52,53,..." — каждое
    фото скопируется в папку каждого из аккаунтов с уникальным uuid-именем.
    Полезно при онбординге 400 аккаунтов: один раз готовишь пак фоток и
    раскидываешь на всех.

    Все аккаунты должны принадлежать текущему юзеру (проверяется одним SQL).
    """
    try:
        acc_ids = [int(x.strip()) for x in account_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="account_ids должен быть списком чисел через запятую")
    if not acc_ids:
        raise HTTPException(status_code=400, detail="Не указан ни один аккаунт")
    if not files:
        raise HTTPException(status_code=400, detail="Нет файлов")

    # Проверяем владение всеми переданными account_ids одним запросом
    r = await db.execute(
        select(TelegramAccount.id).where(
            TelegramAccount.id.in_(acc_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )
    owned_ids = {row[0] for row in r.fetchall()}
    not_owned = [a for a in acc_ids if a not in owned_ids]
    if not_owned:
        raise HTTPException(status_code=403, detail=f"Аккаунты не найдены или не принадлежат вам: {not_owned}")

    # Читаем все файлы один раз в память (помним про MAX_FILE_BYTES)
    bytes_per_file = []
    rejected_files = []
    for uf in files:
        ext = Path(uf.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTS:
            rejected_files.append({"filename": uf.filename, "reason": f"расш. {ext} не поддерживается"})
            continue
        content = await uf.read()
        if len(content) > MAX_FILE_BYTES:
            rejected_files.append({"filename": uf.filename, "reason": f"> {MAX_FILE_BYTES // 1024 // 1024}MB"})
            continue
        if len(content) < 1024:
            rejected_files.append({"filename": uf.filename, "reason": "битый/слишком маленький"})
            continue
        bytes_per_file.append((ext, content, uf.filename))

    if not bytes_per_file:
        return {
            "accounts_count": len(acc_ids),
            "files_saved_per_account": 0,
            "rejected_files": rejected_files,
            "message": "Все файлы отклонены, ничего не сохранено",
        }

    # Раскидываем по папкам
    per_account_stats = []
    for acc_id in acc_ids:
        target = _account_dir(acc_id)
        saved = 0
        for ext, content, _orig_name in bytes_per_file:
            unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
            try:
                (target / unique_name).write_bytes(content)
                saved += 1
            except OSError as e:
                logger.warning(f"[bulk_media] account {acc_id} {unique_name}: {e}")
        per_account_stats.append({"account_id": acc_id, "saved": saved})

    total_saved = sum(s["saved"] for s in per_account_stats)
    return {
        "accounts_count": len(acc_ids),
        "files_saved_per_account": len(bytes_per_file),
        "total_saved": total_saved,
        "rejected_files": rejected_files,
        "per_account": per_account_stats,
        "message": f"Скопировано {len(bytes_per_file)} фото в {len(acc_ids)} папок (итого {total_saved} файлов)",
    }


class BulkClearMediaRequest:
    """Используем POST/DELETE с query — слишком ограниченно. Делаем POST с JSON."""
    pass


@bulk_router.post("/clear")
async def bulk_clear_media(
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Удалить все фото у нескольких аккаунтов.
    Body: {"account_ids": [1, 2, 3]}.
    """
    acc_ids = payload.get("account_ids", [])
    if not isinstance(acc_ids, list) or not acc_ids:
        raise HTTPException(status_code=400, detail="account_ids must be non-empty list")

    r = await db.execute(
        select(TelegramAccount.id).where(
            TelegramAccount.id.in_(acc_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )
    owned_ids = {row[0] for row in r.fetchall()}
    not_owned = [a for a in acc_ids if a not in owned_ids]
    if not_owned:
        raise HTTPException(status_code=403, detail=f"Не ваши аккаунты: {not_owned}")

    total_removed = 0
    per_account = []
    for acc_id in acc_ids:
        d = _account_dir(acc_id)
        removed = 0
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        total_removed += removed
        per_account.append({"account_id": acc_id, "removed": removed})

    return {
        "accounts_count": len(acc_ids),
        "total_removed": total_removed,
        "per_account": per_account,
    }


@bulk_router.post("/list")
async def bulk_list_media(
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Сколько фото у каждого из переданных аккаунтов.
    Body: {"account_ids": [1, 2, 3]}.
    Используется UI чтобы показать "5 фото / 0 фото" на каждой строке списка.
    """
    acc_ids = payload.get("account_ids", [])
    if not isinstance(acc_ids, list):
        raise HTTPException(status_code=400, detail="account_ids must be a list")
    if not acc_ids:
        return {"counts": {}}

    r = await db.execute(
        select(TelegramAccount.id).where(
            TelegramAccount.id.in_(acc_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )
    owned_ids = {row[0] for row in r.fetchall()}

    counts = {}
    for acc_id in owned_ids:
        counts[acc_id] = len(list_account_media_paths(acc_id))
    return {"counts": counts}
