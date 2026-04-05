"""
GramGPT API — schemas/api_app.py
Pydantic схемы для управления API-приложениями.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator


class ApiAppCreate(BaseModel):
    api_id:       int
    api_hash:     str
    title:        str = ""
    max_accounts: int = 100
    notes:        str = ""

    @field_validator("api_hash")
    @classmethod
    def validate_hash(cls, v):
        v = v.strip()
        if len(v) < 10:
            raise ValueError("api_hash слишком короткий")
        return v

    @field_validator("max_accounts")
    @classmethod
    def validate_max(cls, v):
        if v < 1:
            raise ValueError("max_accounts должен быть >= 1")
        if v > 500:
            raise ValueError("max_accounts не может быть > 500 (рекомендация Telegram)")
        return v


class ApiAppUpdate(BaseModel):
    title:        Optional[str] = None
    max_accounts: Optional[int] = None
    is_active:    Optional[bool] = None
    notes:        Optional[str] = None


class ApiAppOut(BaseModel):
    id:             int
    api_id:         int
    api_hash:       str
    title:          str
    max_accounts:   int
    is_active:      bool
    notes:          str
    accounts_count: int = 0
    created_at:     datetime
    updated_at:     datetime

    model_config = {"from_attributes": True}
