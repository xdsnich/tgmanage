"""
GramGPT API — models/user.py
"""

from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from database import Base

if TYPE_CHECKING:
    from models.account import TelegramAccount
    from models.proxy import Proxy


class PlanEnum(str, enum.Enum):
    starter    = "starter"
    pro        = "pro"
    enterprise = "enterprise"


class User(Base):
    __tablename__ = "users"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True)
    email:         Mapped[str]      = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str]      = mapped_column(String(255), nullable=False)
    is_active:     Mapped[bool]     = mapped_column(Boolean, default=True)
    is_verified:   Mapped[bool]     = mapped_column(Boolean, default=False)
    plan:          Mapped[PlanEnum] = mapped_column(Enum(PlanEnum), default=PlanEnum.starter)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def account_limit(self) -> int:
        limits = {
            PlanEnum.starter:    10,
            PlanEnum.pro:        50,
            PlanEnum.enterprise: 99999,
        }
        return limits[self.plan]

    accounts:       Mapped[list[TelegramAccount]] = relationship("TelegramAccount", back_populates="user", cascade="all, delete-orphan")
    proxies:        Mapped[list[Proxy]]           = relationship("Proxy", back_populates="user", cascade="all, delete-orphan")
    refresh_tokens: Mapped[list[RefreshToken]]    = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email} [{self.plan}]>"


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id:    Mapped[int]      = mapped_column(ForeignKey("users.id"), nullable=False)
    token:      Mapped[str]      = mapped_column(String(512), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked:    Mapped[bool]     = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")