"""
GramGPT API — models/api_app.py
Telegram API приложения (api_id + api_hash).
Позволяет распределять аккаунты по разным API-ключам,
чтобы избежать массового бана при масштабировании.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.user import User
    from models.account import TelegramAccount


class ApiApp(Base):
    __tablename__ = "api_apps"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id:       Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    api_id:        Mapped[int]      = mapped_column(Integer, nullable=False)
    api_hash:      Mapped[str]      = mapped_column(String(64), nullable=False)
    title:         Mapped[str]      = mapped_column(String(128), default="")
    max_accounts:  Mapped[int]      = mapped_column(Integer, default=100)
    is_active:     Mapped[bool]     = mapped_column(Boolean, default=True)
    notes:         Mapped[str]      = mapped_column(Text, default="")
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user:     Mapped[User]                    = relationship("User", backref="api_apps")
    accounts: Mapped[list[TelegramAccount]]   = relationship("TelegramAccount", back_populates="api_app")

    @property
    def accounts_count(self) -> int:
        return len(self.accounts) if self.accounts else 0

    @property
    def is_full(self) -> bool:
        return self.accounts_count >= self.max_accounts

    def __repr__(self):
        return f"<ApiApp #{self.id} '{self.title}' api_id={self.api_id} [{self.accounts_count}/{self.max_accounts}]>"
