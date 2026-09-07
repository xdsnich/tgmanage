"""GramGPT API — routers/leomatch.py
Массовый онбординг аккаунтов в @leomatchbot.

Endpoints:
  POST   /leomatch/photos          — upload одной или нескольких картинок (multipart)
  POST   /leomatch/tasks           — создать batch (photos_paths + cities + descriptions + accounts)
  GET    /leomatch/tasks           — список batch'ей юзера
  GET    /leomatch/tasks/{id}      — детали + items
  POST   /leomatch/tasks/{id}/start — запустить celery
  DELETE /leomatch/tasks/{id}      — удалить (только pending/done)
"""

import random
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sa_delete

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.leomatch_onboarding import LeomatchOnboardingTask, LeomatchOnboardingItem
from models.account import TelegramAccount

router = APIRouter(prefix="/leomatch", tags=["leomatch"])

# Куда сохраняем фото (не в /uploads чтобы не смешивать со storiz)
UPLOAD_ROOT = Path(__file__).parent.parent.parent / "uploads" / "leomatch"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


# ── Schemas ──────────────────────────────────────────────────

class TaskCreate(BaseModel):
    label: str = ""
    account_ids: list[int]
    cities: list[str]
    descriptions: list[str] = []
    names: list[str] = []
    photos: list[str]                    # относительные пути от UPLOAD_ROOT
    language: str = "🇺🇦 Українська"
    age_min: int = 22
    age_max: int = 32
    seek: str = "male"                   # male | female | any
    gender_mode: str = "auto"            # auto | male | female
    photos_per_acc: int = 1


class TaskShort(BaseModel):
    id: int
    label: str
    status: str
    total_items: int
    done_items: int
    failed_items: int
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ── Photo upload ─────────────────────────────────────────────

@router.post("/photos")
async def upload_photos(
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
):
    """Загрузить N фото в общий пул юзера. Возвращает список относительных путей."""
    user_dir = UPLOAD_ROOT / f"user_{current_user.id}"
    user_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in files:
        # Только картинки/видео
        ext = (Path(f.filename).suffix or ".jpg").lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}:
            continue
        fname = f"{uuid.uuid4().hex}{ext}"
        fpath = user_dir / fname
        content = await f.read()
        # Лимит 15 МБ на файл
        if len(content) > 15 * 1024 * 1024:
            continue
        fpath.write_bytes(content)
        saved.append(str(fpath.relative_to(UPLOAD_ROOT)))
    return {"saved": saved, "count": len(saved)}


# ── Tasks ────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskShort)
async def create_task(
    payload: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not payload.account_ids:
        raise HTTPException(400, "account_ids пуст")
    if not payload.cities:
        raise HTTPException(400, "cities пуст (нужен хотя бы один город)")
    if not payload.photos:
        raise HTTPException(400, "photos пуст (нужно хотя бы одно фото)")

    # Верифицируем acc_ids принадлежат юзеру и валидируем что фото существуют
    accs = (await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id.in_(payload.account_ids),
            TelegramAccount.user_id == current_user.id,
        )
    )).scalars().all()
    if not accs:
        raise HTTPException(400, "не найдено акков (проверь user_id / account_ids)")

    # Абсолютизируем пути фото и проверяем что файлы есть
    abs_photos = []
    for rel in payload.photos:
        p = (UPLOAD_ROOT / rel).resolve()
        # Защита от path traversal
        try:
            p.relative_to(UPLOAD_ROOT.resolve())
        except ValueError:
            raise HTTPException(400, f"путь вне uploads: {rel}")
        if not p.exists():
            raise HTTPException(400, f"фото не найдено: {rel}")
        abs_photos.append(str(p))

    # Создаём task
    task = LeomatchOnboardingTask(
        user_id=current_user.id,
        label=payload.label.strip() or f"batch {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
        cities=payload.cities,
        descriptions=payload.descriptions,
        names=payload.names,
        photos=abs_photos,
        language=payload.language,
        age_min=payload.age_min,
        age_max=payload.age_max,
        seek=payload.seek,
        gender_mode=payload.gender_mode,
        photos_per_acc=max(1, min(3, payload.photos_per_acc)),
    )
    db.add(task)
    await db.flush()

    # Создаём items с рандомным профилем каждому
    for acc in accs:
        # Рандомно распределяем: город/описание/имя/фото
        city = random.choice(payload.cities)
        desc = random.choice(payload.descriptions) if payload.descriptions else ""
        name = random.choice(payload.names) if payload.names else (acc.first_name or "")
        age = random.randint(payload.age_min, payload.age_max)
        n_photos = min(payload.photos_per_acc, len(abs_photos))
        photos_for_acc = random.sample(abs_photos, k=n_photos)

        # Автодетект пола по имени (грубо)
        if payload.gender_mode == "auto":
            fn = (acc.first_name or "").lower()
            female_endings = ("а", "я", "ія", "ева", "ова", "ина")
            own_gender = "female" if fn.endswith(female_endings) else "male"
        else:
            own_gender = payload.gender_mode

        item = LeomatchOnboardingItem(
            task_id=task.id,
            account_id=acc.id,
            profile={
                "language": payload.language,
                "name": name,
                "city": city,
                "description": desc,
                "age": age,
                "own_gender": own_gender,
                "seek": payload.seek,
                "photos": photos_for_acc,
            },
        )
        db.add(item)

    task.total_items = len(accs)
    await db.commit()
    await db.refresh(task)
    return _as_short(task)


@router.get("/tasks", response_model=list[TaskShort])
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(LeomatchOnboardingTask)
        .where(LeomatchOnboardingTask.user_id == current_user.id)
        .order_by(LeomatchOnboardingTask.created_at.desc())
        .limit(100)
    )
    return [_as_short(t) for t in r.scalars().all()]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")

    items_q = await db.execute(
        select(LeomatchOnboardingItem, TelegramAccount.phone, TelegramAccount.first_name)
        .join(TelegramAccount, TelegramAccount.id == LeomatchOnboardingItem.account_id)
        .where(LeomatchOnboardingItem.task_id == task_id)
        .order_by(LeomatchOnboardingItem.id.asc())
    )
    items = []
    for item, phone, first_name in items_q.all():
        items.append({
            "id": item.id,
            "account_id": item.account_id,
            "phone": phone,
            "first_name": first_name,
            "status": item.status,
            "error": item.error,
            "profile": item.profile,
            "steps_log": item.steps_log,
            "started_at": item.started_at.isoformat() + "Z" if item.started_at else None,
            "finished_at": item.finished_at.isoformat() + "Z" if item.finished_at else None,
        })

    # live-счётчики
    counts = {"pending": 0, "running": 0, "done": 0, "error": 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1

    return {
        "task": _as_short(t).model_dump(),
        "config": {
            "cities": t.cities,
            "descriptions": t.descriptions,
            "names": t.names,
            "photos_count": len(t.photos or []),
            "language": t.language,
            "age_min": t.age_min,
            "age_max": t.age_max,
            "seek": t.seek,
            "gender_mode": t.gender_mode,
            "photos_per_acc": t.photos_per_acc,
        },
        "counts": counts,
        "items": items,
    }


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    if t.status == "running":
        raise HTTPException(400, "уже running")

    from tasks.leomatch_onboarding import execute_batch
    execute_batch.delay(task_id)

    t.status = "running"
    t.error = ""
    await db.commit()
    return {"status": "queued", "task_id": task_id}


class LikesConfig(BaseModel):
    likes_min: int = 5
    likes_max: int = 11
    max_parallel: int = 3


@router.get("/tasks/{task_id}/likes-status")
async def likes_status(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Live progress лайков (из Redis). Возвращает stats если задача running/недавно
    завершилась (TTL 1ч), иначе {status: 'idle'}."""
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    try:
        from utils.redis_pool import get_redis
        import json
        raw = get_redis().get(f"gramgpt:leomatch:likes:{task_id}")
        if not raw:
            return {"status": "idle"}
        data = json.loads(raw)
        return data
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/tasks/{task_id}/likes-stop")
async def likes_stop(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force stop лайков: ставим Redis-флаг, scheduler увидит и завершит после
    текущего цикла (in-flight items доработают, новые не запускаются)."""
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    try:
        from utils.redis_pool import get_redis
        get_redis().setex(f"gramgpt:leomatch:likes:{task_id}:stop", 3600, "1")
        return {"status": "stop_requested"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/tasks/{task_id}/send-likes")
async def send_likes_batch(
    task_id: int,
    cfg: LikesConfig,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Пройти по done акках batch'а — для каждого случайное количество лайков
    в @leomatchbot. Параллельно (2-4 акка), не более 1 акка на прокси одновременно."""
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    lmin = max(1, min(50, cfg.likes_min))
    lmax = max(lmin, min(50, cfg.likes_max))
    par = max(2, min(4, cfg.max_parallel))
    from tasks.leomatch_onboarding import execute_likes_batch
    execute_likes_batch.delay(task_id, lmin, lmax, par)
    return {"status": "queued", "task_id": task_id, "likes_min": lmin, "likes_max": lmax, "max_parallel": par}


@router.post("/tasks/{task_id}/replace-photos")
async def replace_photos(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Проверить каждый done акк — если @leomatchbot пометил анкету 'отключена
    из-за неподходящего фото', заменить на случайную вариацию из variations/."""
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    from tasks.leomatch_onboarding import execute_photo_replace
    execute_photo_replace.delay(task_id)
    return {"status": "queued", "task_id": task_id}


@router.post("/tasks/{task_id}/fix-privacy")
async def fix_privacy(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Пройти по всем done items batch'а и у каждого акка выставить
    Privacy → Forwards → Allow All. Параллельно, но не 2 акка с одним прокси.
    Нужно потому что @leomatchbot не показывает анкету если у акка ограничена
    пересылка сообщений."""
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    from tasks.leomatch_onboarding import execute_privacy_fix
    execute_privacy_fix.delay(task_id)
    return {"status": "queued", "task_id": task_id}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force delete — работает даже для running (celery job упадёт на
    следующем commit'е, это ожидаемо). Items каскадно удаляются."""
    t = (await db.execute(select(LeomatchOnboardingTask).where(
        LeomatchOnboardingTask.id == task_id,
        LeomatchOnboardingTask.user_id == current_user.id,
    ))).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "not found")
    was_running = t.status == "running"
    await db.delete(t)
    await db.commit()
    return {"deleted": task_id, "was_running": was_running}


def _as_short(t: LeomatchOnboardingTask) -> TaskShort:
    return TaskShort(
        id=t.id, label=t.label, status=t.status,
        total_items=t.total_items, done_items=t.done_items, failed_items=t.failed_items,
        created_at=t.created_at, started_at=t.started_at, finished_at=t.finished_at,
    )
