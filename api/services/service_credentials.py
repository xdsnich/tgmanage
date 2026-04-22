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


async def get_api_key(db: AsyncSession, user_id: int, provider: str) -> str:
    """
    Получает API ключ из БД для пользователя и провайдера.
    Если в БД нет — берёт из env variable.
    """
    provider = provider.lower().strip()

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
