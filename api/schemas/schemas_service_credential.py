"""
GramGPT API — schemas/service_credential.py
Схемы для API ключей внешних сервисов.
"""

from pydantic import BaseModel, field_validator
from typing import Optional


VALID_PROVIDERS = {"claude", "openai", "gemini", "groq", "tgstat"}


class ServiceCredentialCreate(BaseModel):
    provider:   str
    api_key:    str
    label:      str = ""
    is_default: bool = False
    notes:      str = ""

    @field_validator("provider")
    @classmethod
    def check_provider(cls, v):
        v = v.lower().strip()
        if v not in VALID_PROVIDERS:
            raise ValueError(f"Неизвестный провайдер. Доступные: {', '.join(sorted(VALID_PROVIDERS))}")
        return v

    @field_validator("api_key")
    @classmethod
    def check_api_key(cls, v):
        v = v.strip()
        if len(v) < 10:
            raise ValueError("API ключ слишком короткий")
        return v


class ServiceCredentialUpdate(BaseModel):
    api_key:    Optional[str] = None
    label:      Optional[str] = None
    is_active:  Optional[bool] = None
    is_default: Optional[bool] = None
    notes:      Optional[str] = None
