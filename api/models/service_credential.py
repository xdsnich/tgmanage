"""
GramGPT API — models/service_credential.py
API ключи для внешних сервисов (LLM, парсеры и т.д.)
"""

from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ServiceCredential(Base):
    __tablename__ = "service_credentials"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id:    Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Тип провайдера: claude | openai | gemini | groq | tgstat
    provider:   Mapped[str]      = mapped_column(String(32), nullable=False, index=True)
    api_key:    Mapped[str]      = mapped_column(Text, nullable=False)
    label:      Mapped[str]      = mapped_column(String(128), default="")
    is_active:  Mapped[bool]     = mapped_column(Boolean, default=True)
    # Если несколько ключей одного провайдера — default будет использоваться по умолчанию
    is_default: Mapped[bool]     = mapped_column(Boolean, default=False)
    notes:      Mapped[str]      = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ServiceCredential {self.provider} #{self.id}>"
