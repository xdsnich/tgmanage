"""
GramGPT API — schemas/api_app.py
Pydantic схемы для управления API-приложениями.
"""

from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, field_validator


# ═══════════════════════════════════════════════════════════
# СПРАВОЧНИК ИЗВЕСТНЫХ ПУБЛИЧНЫХ API_ID
#
# api_id официальных клиентов Telegram, которые давно известны
# и используются для безопасного масштабирования.
#
# ВАЖНО: эти ключи ПУБЛИЧНЫЕ (лежат в открытых репозиториях на GitHub).
# Для Telegram использование "Telegram for Android" (api_id=6) выглядит
# как самый обычный пользователь с официальным клиентом.
# ═══════════════════════════════════════════════════════════

KNOWN_PUBLIC_APIS = {
    # Android
    6: {
        "title": "Telegram for Android",
        "api_hash": "eb06d4abfb49dc3eeb1aeb98ae0f581e",
        "platform": "android",
        "description": "Официальный клиент Telegram для Android — самый безопасный, высокий траст.",
    },
    # iOS
    8: {
        "title": "Telegram for iOS",
        "api_hash": "7245de8e747a0d6fbe11f7cc14fcc0bb",
        "platform": "ios",
        "description": "Официальный клиент Telegram для iPhone/iPad.",
    },
    # Desktop (Windows/Linux)
    2040: {
        "title": "Telegram Desktop",
        "api_hash": "b18441a1ff607e10a989891a5462e627",
        "platform": "desktop",
        "description": "Официальный Telegram Desktop для Windows/Linux.",
    },
    # macOS
    2834: {
        "title": "Telegram for macOS",
        "api_hash": "68875f756c9b437a8b916ca3de215815",
        "platform": "macos",
        "description": "Официальный Telegram для macOS.",
    },
    # TelegramX (альтернативный Android клиент)
    21724: {
        "title": "Telegram X (Android)",
        "api_hash": "3e0cb5efcd52300aec5994fdfc5bdc16",
        "platform": "android",
        "description": "Экспериментальный Android клиент от Telegram.",
    },
}


def detect_platform_by_api_id(api_id: int) -> str:
    """Автоопределение платформы по api_id. По умолчанию android."""
    if api_id in KNOWN_PUBLIC_APIS:
        return KNOWN_PUBLIC_APIS[api_id]["platform"]
    return "android"


# ═══════════════════════════════════════════════════════════
# Pydantic схемы
# ═══════════════════════════════════════════════════════════

PlatformType = Literal["android", "ios", "desktop", "macos"]


class ApiAppCreate(BaseModel):
    api_id:       int
    api_hash:     str
    title:        str = ""
    platform:     Optional[PlatformType] = None   # None → автоопределение
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
    platform:     Optional[PlatformType] = None
    max_accounts: Optional[int] = None
    is_active:    Optional[bool] = None
    notes:        Optional[str] = None


class ApiAppOut(BaseModel):
    id:             int
    api_id:         int
    api_hash:       str
    title:          str
    platform:       str
    max_accounts:   int
    is_active:      bool
    notes:          str
    accounts_count: int = 0
    created_at:     datetime
    updated_at:     datetime

    model_config = {"from_attributes": True}