"""
GramGPT API — models/ai_dialog.py
Модель ИИ-диалогов.
По ТЗ раздел 3.3: ai_dialogs — id, account_id, contact_id, system_prompt, last_message, is_active
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, BigInteger, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.account import TelegramAccount


class AIDialog(Base):
    __tablename__ = "ai_dialogs"

    id:            Mapped[int]             = mapped_column(Integer, primary_key=True)
    account_id:    Mapped[int]             = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    contact_id:    Mapped[int]             = mapped_column(BigInteger, nullable=False, index=True)
    contact_name:  Mapped[str]             = mapped_column(String(128), default="")
    system_prompt: Mapped[str]             = mapped_column(Text, default="")
    last_message:  Mapped[str]             = mapped_column(Text, default="")
    last_msg_id:   Mapped[int]             = mapped_column(Integer, default=0)
    is_active:     Mapped[bool]            = mapped_column(Boolean, default=False)
    llm_provider:  Mapped[str]             = mapped_column(String(32), default="claude")
    messages_count:Mapped[int]             = mapped_column(Integer, default=0)
    created_at:    Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:    Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account: Mapped[TelegramAccount] = relationship("TelegramAccount", backref=None)

    def __repr__(self):
        return f"<AIDialog account={self.account_id} contact={self.contact_id} active={self.is_active}>"