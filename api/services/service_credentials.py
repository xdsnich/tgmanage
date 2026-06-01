"""
GramGPT API — services/service_credentials.py
Helper для получения API ключа провайдера:
1. Default активный ключ из БД
2. Любой активный из БД
3. Fallback на env variable
"""

import os
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.service_credential import ServiceCredential

logger = logging.getLogger(__name__)


# Маппинг: провайдер → env variable (для fallback на старые ключи из .env)
ENV_MAP = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq":   "GROQ_API_KEY",
    "tgstat": "TGSTAT_TOKEN",
}


async def get_api_key(
    db: AsyncSession,
    user_id: int,
    provider: str,
    credential_id: Optional[int] = None,
) -> str:
    """
    Получает API ключ из БД для пользователя и провайдера.

    Args:
        credential_id: если указан, пытаемся использовать конкретный ключ
            (с проверкой что он принадлежит юзеру, активен и совпадает по провайдеру).
            Если ключ не подходит — fallback на default флоу с предупреждением в логе.
    """
    provider = provider.lower().strip()

    # 0. Если указан конкретный credential_id — пробуем его первым
    if credential_id:
        cred = (await db.execute(
            select(ServiceCredential).where(
                ServiceCredential.id == credential_id,
                ServiceCredential.user_id == user_id,
                ServiceCredential.is_active == True,
            )
        )).scalar_one_or_none()

        if cred and cred.provider == provider:
            return cred.api_key

        # Ключ не найден / выключен / другой провайдер — логируем и идём в default
        if cred and cred.provider != provider:
            logger.warning(
                f"[service_creds] credential_id={credential_id} provider={cred.provider}, "
                f"но кампания просит {provider} — использую default."
            )
        else:
            logger.warning(
                f"[service_creds] credential_id={credential_id} не найден/выключен "
                f"для user={user_id} — fallback на default {provider}."
            )

    # 1. Default ключ
    cred = (await db.execute(
        select(ServiceCredential).where(
            ServiceCredential.user_id == user_id,
            ServiceCredential.provider == provider,
            ServiceCredential.is_active == True,
            ServiceCredential.is_default == True,
        )
    )).scalar_one_or_none()

    # 2. Любой активный
    if not cred:
        cred = (await db.execute(
            select(ServiceCredential).where(
                ServiceCredential.user_id == user_id,
                ServiceCredential.provider == provider,
                ServiceCredential.is_active == True,
            ).limit(1)
        )).scalar_one_or_none()

    if cred:
        return cred.api_key

    # 3. Fallback на env (для обратной совместимости)
    env_var = ENV_MAP.get(provider)
    if env_var:
        key = os.getenv(env_var, "")
        if key:
            logger.info(f"[service_creds] Используется env ключ {env_var} для user={user_id}")
        return key

    return ""
