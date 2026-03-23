"""
GramGPT API — schemas/user.py
Pydantic схемы для пользователей и авторизации
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator

from models.user import PlanEnum


# ── Регистрация ──────────────────────────────────────────────
class UserRegister(BaseModel):
    email:    EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Пароль минимум 8 символов")
        return v


# ── Вход ────────────────────────────────────────────────────
class UserLogin(BaseModel):
    email:    EmailStr
    password: str


# ── Токены ──────────────────────────────────────────────────
class TokenPair(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str


# ── Ответ о пользователе ────────────────────────────────────
class UserOut(BaseModel):
    id:           int
    email:        str
    plan:         PlanEnum
    is_active:    bool
    is_verified:  bool
    account_limit:int
    created_at:   datetime

    model_config = {"from_attributes": True}


# ── Смена пароля ────────────────────────────────────────────
class PasswordChange(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Пароль минимум 8 символов")
        return v
