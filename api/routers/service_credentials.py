"""
GramGPT API — routers/service_credentials.py
CRUD для API ключей LLM и внешних сервисов.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.service_credential import ServiceCredential
from schemas.service_credential import ServiceCredentialCreate, ServiceCredentialUpdate, VALID_PROVIDERS

router = APIRouter(prefix="/service-credentials", tags=["service-credentials"])


PROVIDER_META = {
    "claude": {"name": "Claude (Anthropic)",   "icon": "🧠", "color": "#d97757"},
    "openai": {"name": "OpenAI (GPT)",         "icon": "🤖", "color": "#10a37f"},
    "gemini": {"name": "Gemini (Google)",      "icon": "✨", "color": "#4285f4"},
    "groq":   {"name": "Groq (Llama)",         "icon": "⚡", "color": "#ff6b35"},
    "tgstat": {"name": "TGStat",               "icon": "📊", "color": "#3d8bff"},
}


def _mask(key: str) -> str:
    """sk-proj-xxxx...abc1"""
    if not key or len(key) < 14:
        return "•" * max(len(key or ""), 8)
    return f"{key[:6]}…{key[-4:]}"


# ── LIST ─────────────────────────────────────────────────────

@router.get("")
async def list_credentials(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Все ключи пользователя, сгруппированные по провайдеру."""
    result = await db.execute(
        select(ServiceCredential).where(ServiceCredential.user_id == current_user.id)
        .order_by(ServiceCredential.provider, ServiceCredential.created_at.desc())
    )
    creds = result.scalars().all()

    return [{
        "id": c.id,
        "provider": c.provider,
        "provider_name": PROVIDER_META.get(c.provider, {}).get("name", c.provider),
        "provider_icon": PROVIDER_META.get(c.provider, {}).get("icon", "🔑"),
        "provider_color": PROVIDER_META.get(c.provider, {}).get("color", "#888"),
        "label": c.label or "",
        "api_key_masked": _mask(c.api_key),
        "is_active": c.is_active,
        "is_default": c.is_default,
        "notes": c.notes or "",
        "created_at": c.created_at.isoformat() + "Z" if c.created_at else None,
        "updated_at": c.updated_at.isoformat() + "Z" if c.updated_at else None,
    } for c in creds]


# ── META (список провайдеров для UI) ─────────────────────────

@router.get("/providers")
async def list_providers():
    """Список всех доступных провайдеров — для селекта на фронте."""
    return [
        {"key": k, **v}
        for k, v in PROVIDER_META.items()
    ]


# ── STATS ─────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика по ключам пользователя."""
    r = await db.execute(
        select(ServiceCredential.provider, func.count(ServiceCredential.id))
        .where(ServiceCredential.user_id == current_user.id)
        .group_by(ServiceCredential.provider)
    )
    by_provider = {row[0]: row[1] for row in r.all()}

    total = sum(by_provider.values())
    return {
        "total": total,
        "by_provider": by_provider,
        "configured_providers": list(by_provider.keys()),
        "missing_providers": [p for p in VALID_PROVIDERS if p not in by_provider],
    }


# ── CREATE ───────────────────────────────────────────────────

@router.post("")
async def create_credential(
    data: ServiceCredentialCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Добавить новый API ключ."""
    # Если назначаем default — снимаем default со всех остальных этого провайдера
    if data.is_default:
        await db.execute(
            update(ServiceCredential)
            .where(
                ServiceCredential.user_id == current_user.id,
                ServiceCredential.provider == data.provider,
            )
            .values(is_default=False)
        )

    # Автоматически default если это первый ключ этого провайдера
    existing_count = (await db.execute(
        select(func.count(ServiceCredential.id)).where(
            ServiceCredential.user_id == current_user.id,
            ServiceCredential.provider == data.provider,
        )
    )).scalar() or 0

    cred = ServiceCredential(
        user_id=current_user.id,
        provider=data.provider,
        api_key=data.api_key,
        label=data.label.strip(),
        is_default=data.is_default or existing_count == 0,  # первый — автоматически default
        notes=data.notes,
    )
    db.add(cred)
    await db.flush()

    return {
        "id": cred.id,
        "provider": cred.provider,
        "message": f"Ключ для {PROVIDER_META.get(cred.provider, {}).get('name', cred.provider)} добавлен",
    }


# ── UPDATE ───────────────────────────────────────────────────

@router.patch("/{cred_id}")
async def update_credential(
    cred_id: int,
    data: ServiceCredentialUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cred = (await db.execute(
        select(ServiceCredential).where(
            ServiceCredential.id == cred_id,
            ServiceCredential.user_id == current_user.id,
        )
    )).scalar_one_or_none()

    if not cred:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    # Если ставим default — снимаем default с остальных
    if data.is_default:
        await db.execute(
            update(ServiceCredential)
            .where(
                ServiceCredential.user_id == current_user.id,
                ServiceCredential.provider == cred.provider,
                ServiceCredential.id != cred_id,
            )
            .values(is_default=False)
        )

    if data.api_key is not None:
        api_key_stripped = data.api_key.strip()
        if len(api_key_stripped) < 10:
            raise HTTPException(status_code=400, detail="API ключ слишком короткий")
        cred.api_key = api_key_stripped
    if data.label is not None:
        cred.label = data.label.strip()
    if data.is_active is not None:
        cred.is_active = data.is_active
    if data.is_default is not None:
        cred.is_default = data.is_default
    if data.notes is not None:
        cred.notes = data.notes

    await db.flush()
    return {"message": "Обновлено"}


# ── DELETE ───────────────────────────────────────────────────

@router.delete("/{cred_id}")
async def delete_credential(
    cred_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cred = (await db.execute(
        select(ServiceCredential).where(
            ServiceCredential.id == cred_id,
            ServiceCredential.user_id == current_user.id,
        )
    )).scalar_one_or_none()

    if not cred:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    was_default = cred.is_default
    provider = cred.provider

    await db.delete(cred)
    await db.flush()

    # Если удалили default — назначить default на следующий активный
    if was_default:
        next_cred = (await db.execute(
            select(ServiceCredential).where(
                ServiceCredential.user_id == current_user.id,
                ServiceCredential.provider == provider,
                ServiceCredential.is_active == True,
            ).limit(1)
        )).scalar_one_or_none()
        if next_cred:
            next_cred.is_default = True
            await db.flush()

    return {"message": "Ключ удалён"}


# ── TEST ─────────────────────────────────────────────────────

@router.post("/{cred_id}/test")
async def test_credential(
    cred_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Проверить работает ли ключ."""
    cred = (await db.execute(
        select(ServiceCredential).where(
            ServiceCredential.id == cred_id,
            ServiceCredential.user_id == current_user.id,
        )
    )).scalar_one_or_none()

    if not cred:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    import httpx

    try:
        if cred.provider == "claude":
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": cred.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
                if resp.status_code == 200:
                    return {"ok": True, "message": "✅ Claude API работает"}
                return {"ok": False, "message": f"Ошибка {resp.status_code}: {resp.text[:150]}"}

        elif cred.provider == "openai":
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {cred.api_key}"},
                )
                if resp.status_code == 200:
                    return {"ok": True, "message": "✅ OpenAI API работает"}
                return {"ok": False, "message": f"Ошибка {resp.status_code}: {resp.text[:150]}"}

        elif cred.provider == "gemini":
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={cred.api_key}",
                )
                if resp.status_code == 200:
                    return {"ok": True, "message": "✅ Gemini API работает"}
                return {"ok": False, "message": f"Ошибка {resp.status_code}: {resp.text[:150]}"}

        elif cred.provider == "groq":
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {cred.api_key}"},
                )
                if resp.status_code == 200:
                    return {"ok": True, "message": "✅ Groq API работает"}
                return {"ok": False, "message": f"Ошибка {resp.status_code}: {resp.text[:150]}"}

        elif cred.provider == "tgstat":
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"https://api.tgstat.ru/usage/stat?token={cred.api_key}"
                )
                data = resp.json()
                if data.get("status") == "ok":
                    return {"ok": True, "message": "✅ TGStat API работает"}
                return {"ok": False, "message": f"Ошибка: {data.get('error', 'Неверный ключ')}"}

        return {"ok": False, "message": "Неизвестный провайдер"}

    except httpx.TimeoutException:
        return {"ok": False, "message": "Таймаут. Проверь интернет или API провайдера."}
    except Exception as e:
        return {"ok": False, "message": f"Ошибка: {str(e)[:150]}"}
