"""
GramGPT API — models/account_connection.py
Лог подключений аккаунтов к Telegram (для истории и UI-индикаторов).
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AccountConnection(Base):
    __tablename__ = "account_connections"

    id:           Mapped[int]             = mapped_column(Integer, primary_key=True)
    account_id:   Mapped[int]             = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    connected_at: Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Источник: warmup | commenting | ai_dialog | manual | parser | unknown
    source:       Mapped[str]             = mapped_column(String(32), default="unknown", index=True)
    proxy_id:     Mapped[Optional[int]]   = mapped_column(ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True)
    success:      Mapped[bool]            = mapped_column(Boolean, default=True)
    error:        Mapped[Optional[str]]   = mapped_column(String(500), nullable=True)

    def __repr__(self):
        return f"<AccountConnection acc={self.account_id} at={self.connected_at} source={self.source}>"
